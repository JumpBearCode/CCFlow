"""Codex CLI orchestrator — wraps `codex exec` CLI as a reusable Python class."""

import json
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO

from ccflow.agent import codex_printer as printer


@dataclass
class CodexResult:
    """Structured result from a single ``codex exec`` invocation.

    Populated by parsing JSONL events from ``codex exec --json``.
    On failure, ``success`` is False and ``error`` contains the reason;
    all other fields may be None/zero.

    Attributes:
        success: Whether the invocation completed without error.
        output: Final text output — the last ``agent_message`` text.
        duration_ms: Client-side wall-clock duration in milliseconds.
        thread_id: Thread ID from ``thread.started`` event.
        num_turns: Number of ``turn.completed`` events.
        usage: Accumulated token usage dict containing
            ``input_tokens``, ``output_tokens``, ``cached_input_tokens``.
        error: Error message when ``success`` is False, None otherwise.
    """

    success: bool
    output: str | None = None
    duration_ms: int = 0
    thread_id: str | None = None
    num_turns: int = 0
    usage: dict | None = None
    error: str | None = None

    @property
    def session_id(self) -> str | None:
        """Alias for API compatibility with ClaudeResult."""
        return self.thread_id


class CodexOrchestrator:
    """High-level wrapper around ``codex exec`` with JSONL parsing.

    Supports three execution modes:
    - ``run(prompt)`` — batch mode, minimal output, returns final result
    - ``run_stream(prompt)`` — stream mode, real-time Codex style printing
    - ``run_conversation(initial_prompt)`` — multi-round interactive conversation
    """

    def __init__(
        self,
        *,
        model: str = "gpt-5.4",
        sandbox: str | None = None,
        full_auto: bool = False,
        dangerously_skip_permissions: bool = False,
        ephemeral: bool = False,
        images: list[str] | None = None,
        config_overrides: list[str] | None = None,
        add_dirs: list[str] | None = None,
        output_schema: str | None = None,
        profile: str | None = None,
        skip_git_repo_check: bool = False,
        resume_session: str | None = None,
        continue_session: bool = False,
        log_dir: str | None = None,
        log_path: str | None = None,
        output_dir: str | None = None,
        cwd: str | None = None,
    ) -> None:
        """Initialize the orchestrator with Codex CLI configuration.

        All parameters are keyword-only and map to ``codex exec`` CLI flags.

        Args:
            model: Model name (e.g. ``"gpt-5.4"``, ``"o4-mini"``). Default: ``"gpt-5.4"``.
            sandbox: Sandbox mode — ``"read-only"``, ``"workspace-write"``,
                or ``"danger-full-access"``.
            full_auto: If True, passes ``-a on-request`` and
                ``--sandbox workspace-write`` for autonomous operation.
            dangerously_skip_permissions: If True, passes
                ``--sandbox danger-full-access`` to bypass all sandbox restrictions.
                Overrides ``sandbox`` and ``full_auto``.
            ephemeral: If True, the thread is not stored after completion.
            images: List of image file paths to include with the prompt.
            config_overrides: List of ``key=value`` config overrides (``-c`` flags).
            add_dirs: List of additional directories to include.
            output_schema: JSON schema file path for structured output.
            profile: Configuration profile name.
            skip_git_repo_check: If True, skip the git repository check.
            resume_session: Thread ID to resume a previous session.
            continue_session: If True, continue the most recent session (``--last``).
            log_dir: Directory for auto-generated log files.
            log_path: Explicit path for the log file. Takes priority over ``log_dir``.
            output_dir: Directory to write ``result.output`` after each run.
            cwd: Working directory for the ``codex`` subprocess.
        """
        self.model = model
        self.sandbox = sandbox
        self.full_auto = full_auto
        self.dangerously_skip_permissions = dangerously_skip_permissions
        self.ephemeral = ephemeral
        self.images = images
        self.config_overrides = config_overrides
        self.add_dirs = add_dirs
        self.output_schema = output_schema
        self.profile = profile
        self.skip_git_repo_check = skip_git_repo_check
        self.resume_session = resume_session
        self.continue_session = continue_session
        self.log_dir = log_dir
        self.log_path = log_path
        self.output_dir = output_dir
        self.cwd = cwd

    # ── private helpers ──────────────────────────────────────

    def _build_cmd(self) -> list[str]:
        """Convert configuration into a ``codex exec`` CLI argument list.

        Normal:   ["codex", "exec", "--json", "-m", model, ...flags..., "-"]
        Resume:   ["codex", "exec", "resume", "--json", "-m", model, thread_id, "-"]
        Continue: ["codex", "exec", "resume", "--json", "-m", model, "--last", "-"]

        Note: ``codex exec resume`` only accepts ``--json``, ``-m/--model``,
        ``--last``, ``[SESSION_ID]``, and ``[PROMPT]``.  All other flags
        (``--sandbox``, ``--ephemeral``, ``--image``, etc.) are ignored when
        resuming because the CLI rejects them.

        Returns:
            List of strings suitable for ``subprocess.Popen()``.
        """
        is_resume = bool(self.resume_session) or self.continue_session

        if is_resume:
            cmd = ["codex", "exec", "resume", "--json"]
            cmd += ["-m", self.model]

            if self.continue_session:
                cmd.append("--last")
            elif self.resume_session:
                cmd.append(self.resume_session)

            cmd.append("-")
            return cmd

        cmd = ["codex", "exec", "--json"]
        cmd += ["-m", self.model]

        # Sandbox resolution: dangerously_skip_permissions > full_auto > sandbox
        if self.dangerously_skip_permissions:
            cmd += ["--sandbox", "danger-full-access"]
        elif self.full_auto:
            cmd += ["-a", "on-request", "--sandbox", "workspace-write"]
        elif self.sandbox:
            cmd += ["--sandbox", self.sandbox]

        if self.ephemeral:
            cmd.append("--ephemeral")

        if self.images:
            for img in self.images:
                cmd += ["--image", img]

        if self.config_overrides:
            for override in self.config_overrides:
                cmd += ["-c", override]

        if self.add_dirs:
            for d in self.add_dirs:
                cmd += ["--add-dir", d]

        if self.output_schema:
            cmd += ["--output-schema", self.output_schema]

        if self.profile:
            cmd += ["--profile", self.profile]

        if self.skip_git_repo_check:
            cmd.append("--skip-git-repo-check")

        # Prompt from stdin
        cmd.append("-")

        return cmd

    def _resolve_log_path(self) -> str | None:
        """Determine the log file path for this run.

        Priority: ``log_path`` (explicit) > ``log_dir`` (auto-generate) > None.
        """
        if self.log_path:
            return self.log_path
        if self.log_dir:
            log_dir = self.log_dir
            if not Path(log_dir).is_absolute() and self.cwd:
                log_dir = os.path.join(self.cwd, log_dir)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            return os.path.join(log_dir, f"ccflow-codex-{ts}.log")
        return None

    def _write_output(self, result: CodexResult) -> str | None:
        """Write ``result.output`` to a file in ``output_dir`` if configured."""
        if not self.output_dir or not result.success or not result.output:
            return None
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        output_path = os.path.join(self.output_dir, f"ccflow-codex-{ts}.md")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(result.output)
        return output_path

    def _open_log(self) -> tuple[str | None, IO[str] | None]:
        """Resolve log path and open the log file."""
        log_path = self._resolve_log_path()
        if not log_path:
            return None, None
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if self.log_path else "w"
        return log_path, open(log_path, mode, encoding="utf-8")

    def _call(
        self,
        prompt: str,
        *,
        log_file: IO[str] | None = None,
        print_events: bool = False,
        print_banner: bool = False,
        on_event: Callable[[dict], None] | None = None,
    ) -> CodexResult:
        """Core execution: spawn ``codex exec``, parse JSONL events, return result.

        Args:
            prompt: The prompt text to send via stdin.
            log_file: An already-open file handle for logging raw JSON lines.
            print_events: If True, print streaming events via printer.
            print_banner: If True, print session banner on thread.started.
            on_event: Optional callback invoked with each parsed JSON event.

        Returns:
            A ``CodexResult`` with all extracted metadata.
        """
        try:
            cmd = self._build_cmd()

            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=self.cwd,
            )
            self._proc = proc

            start_time = time.monotonic()

            proc.stdin.write(prompt)
            proc.stdin.close()

            thread_id = None
            last_agent_message = None
            num_turns = 0
            accumulated_usage: dict = {}
            error_message = None

            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue

                if log_file:
                    log_file.write(line + "\n")
                    log_file.flush()

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    if print_events:
                        ts = printer.timestamp()
                        print(
                            f"{printer.DIM}[{ts}] [stderr] {line}{printer.RESET}",
                            flush=True,
                        )
                    continue

                if on_event is not None:
                    try:
                        on_event(event)
                    except Exception:
                        pass  # never let callback crash the parse loop

                etype = event.get("type", "")

                if etype == "thread.started":
                    thread_id = event.get("thread_id")
                    if print_events and print_banner:
                        printer.print_banner(self.model, thread_id)

                elif etype == "item.completed":
                    item = event.get("item", {})
                    if item.get("type") == "agent_message":
                        last_agent_message = item.get("text")
                    if print_events:
                        printer.print_event(event)

                elif etype == "item.started":
                    if print_events:
                        printer.print_event(event)

                elif etype == "turn.completed":
                    num_turns += 1
                    round_usage = event.get("usage")
                    accumulated_usage = self._accumulate_usage(
                        accumulated_usage, round_usage,
                    )

                elif etype == "turn.failed":
                    error_message = event.get("message", "Turn failed")
                    if print_events:
                        printer.print_event(event)

                elif etype == "error":
                    error_message = event.get("message", "Unknown error")
                    if print_events:
                        printer.print_event(event)

                # turn.started → no-op

            proc.wait()

            elapsed_ms = int((time.monotonic() - start_time) * 1000)

            if error_message and last_agent_message is None:
                return CodexResult(
                    success=False,
                    error=error_message,
                    duration_ms=elapsed_ms,
                    thread_id=thread_id,
                    num_turns=num_turns,
                    usage=accumulated_usage or None,
                )

            if proc.returncode != 0 and last_agent_message is None:
                return CodexResult(
                    success=False,
                    error=f"codex exited with code {proc.returncode}",
                    duration_ms=elapsed_ms,
                    thread_id=thread_id,
                )

            return CodexResult(
                success=True,
                output=last_agent_message,
                duration_ms=elapsed_ms,
                thread_id=thread_id,
                num_turns=num_turns,
                usage=accumulated_usage or None,
            )

        except FileNotFoundError:
            return CodexResult(
                success=False,
                error="'codex' CLI not found. Install Codex CLI and ensure it is on PATH.",
            )
        except Exception as e:
            return CodexResult(success=False, error=str(e))

    @staticmethod
    def _accumulate_usage(totals: dict, round_usage: dict | None) -> dict:
        """Merge a round's usage dict into a running totals dict."""
        if not round_usage:
            return totals
        for key in ("input_tokens", "output_tokens", "cached_input_tokens"):
            totals[key] = totals.get(key, 0) + round_usage.get(key, 0)
        return totals

    # ── public API ───────────────────────────────────────────

    def run(
        self,
        prompt: str,
        *,
        on_event: Callable[[dict], None] | None = None,
    ) -> CodexResult:
        """Run Codex in batch mode.

        Prints a "Running..." line at the start and a summary at the end.

        Args:
            prompt: The prompt text to send to Codex.
            on_event: Optional callback invoked with each parsed event dict.

        Returns:
            A ``CodexResult`` containing the final output and session metadata.
        """
        _, log_file = self._open_log()
        try:
            ts = printer.timestamp()
            print(
                f"{printer.DIM}[{ts}]{printer.RESET} "
                f"{printer.BOLD}CCFlow{printer.RESET} "
                f"Running ({self.model})...",
                flush=True,
            )

            result = self._call(
                prompt, log_file=log_file,
                print_events=False, print_banner=False,
                on_event=on_event,
            )
            printer.print_batch_summary(
                result.duration_ms, result.num_turns,
                result.usage, result.thread_id,
            )

            if result.success and result.output:
                print(f"\n{result.output}", flush=True)

            output_path = self._write_output(result)
            if output_path:
                printer.print_output_saved(output_path)

            return result
        finally:
            if log_file:
                log_file.close()

    def run_stream(
        self,
        prompt: str,
        *,
        on_event: Callable[[dict], None] | None = None,
    ) -> CodexResult:
        """Run Codex in stream mode with real-time formatted output.

        Prints events as they arrive: session banner, agent messages,
        command executions, and a result banner at the end.

        Args:
            prompt: The prompt text to send to Codex.
            on_event: Optional callback invoked with each parsed event dict.

        Returns:
            A ``CodexResult`` containing the final output and session metadata.
        """
        _, log_file = self._open_log()
        try:
            result = self._call(
                prompt, log_file=log_file,
                print_events=True, print_banner=True,
                on_event=on_event,
            )

            if result.success:
                printer.print_result_banner(
                    duration_ms=result.duration_ms,
                    thread_id=result.thread_id,
                    num_turns=result.num_turns,
                    usage=result.usage,
                )

            output_path = self._write_output(result)
            if output_path:
                printer.print_output_saved(output_path)

            return result
        finally:
            if log_file:
                log_file.close()

    def run_conversation(
        self,
        initial_prompt: str | None = None,
        *,
        on_event: Callable[[dict], None] | None = None,
    ) -> list[CodexResult]:
        """Run an interactive multi-round conversation session.

        Opens one log file for the entire conversation. Prints the session
        banner on the first round only. Accumulates duration, turns,
        and token usage across all rounds. Prints one aggregated summary
        at the end.

        The loop ends when the user types ``exit``, ``quit``, an empty line,
        or sends EOF (Ctrl-D).

        Args:
            initial_prompt: First prompt to send. If None, prompts interactively.
            on_event: Optional callback invoked with each parsed event dict.

        Returns:
            List of ``CodexResult`` objects, one per round.
        """
        results: list[CodexResult] = []
        _, log_file = self._open_log()

        try:
            # Accumulation state
            total_duration_ms = 0
            total_num_turns = 0
            total_usage: dict = {}

            # ── First round ──
            prompt = initial_prompt
            if not prompt:
                try:
                    prompt = input(f"{printer.BOLD}You:{printer.RESET} ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return results
                if not prompt:
                    return results

            result = self._call(
                prompt, log_file=log_file,
                print_events=True, print_banner=True,
                on_event=on_event,
            )
            results.append(result)

            total_duration_ms += result.duration_ms
            total_num_turns += result.num_turns
            total_usage = self._accumulate_usage(total_usage, result.usage)

            if not result.success or not result.thread_id:
                printer.print_result_banner(
                    duration_ms=total_duration_ms,
                    thread_id=result.thread_id,
                    num_turns=total_num_turns or None,
                    usage=total_usage or None,
                    title="Conversation Complete",
                    extra_lines=[f"Rounds: {len(results)}"],
                )
                return results

            orig_resume = self.resume_session

            # ── Subsequent rounds ──
            while True:
                print()
                try:
                    user_input = input(f"{printer.BOLD}You:{printer.RESET} ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not user_input or user_input.lower() in ("exit", "quit"):
                    break

                self.resume_session = result.thread_id
                result = self._call(
                    user_input, log_file=log_file,
                    print_events=True, print_banner=False,
                    on_event=on_event,
                )
                results.append(result)

                total_duration_ms += result.duration_ms
                total_num_turns += result.num_turns
                total_usage = self._accumulate_usage(total_usage, result.usage)

                if not result.success:
                    break

            self.resume_session = orig_resume

            # One aggregated summary
            final_thread_id = results[-1].thread_id if results else None
            printer.print_result_banner(
                duration_ms=total_duration_ms,
                thread_id=final_thread_id,
                num_turns=total_num_turns or None,
                usage=total_usage or None,
                title="Conversation Complete",
                extra_lines=[f"Rounds: {len(results)}"],
            )

            # Write output from last successful round
            if results:
                output_path = self._write_output(results[-1])
                if output_path:
                    printer.print_output_saved(output_path)

            return results

        finally:
            if log_file:
                log_file.close()
