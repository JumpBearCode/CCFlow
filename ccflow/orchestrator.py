"""Claude Code orchestrator — wraps `claude -p` CLI as a reusable Python class."""

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO

from ccflow import printer


@dataclass
class ClaudeResult:
    """Structured result from a single ``claude -p`` invocation.

    Populated from the ``result`` event in the stream-json output.
    On failure, ``success`` is False and ``error`` contains the reason;
    all other fields may be None/zero.

    Attributes:
        success: Whether the invocation completed without error.
        output: Final text output from the ``result`` event's ``result`` field.
        duration_ms: Wall-clock duration of the entire session in milliseconds.
        duration_api_ms: Server-side API processing time in milliseconds.
        session_id: UUID of the session — pass to ``resume_session`` to continue later.
        cost_usd: Equivalent API cost in USD. Informational only for Max Plan users.
        num_turns: Number of assistant/user conversation turns.
        usage: Raw token usage dict from the ``result`` event, containing
            ``input_tokens``, ``output_tokens``, ``cache_read_input_tokens``,
            ``cache_creation_input_tokens``, etc.
        error: Error message when ``success`` is False, None otherwise.
    """

    success: bool
    output: str | None = None
    duration_ms: int = 0
    duration_api_ms: int = 0
    session_id: str | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    usage: dict | None = None
    error: str | None = None


class ClaudeOrchestrator:
    """High-level wrapper around ``claude -p`` with stream-json parsing.

    Supports three execution modes:
    - ``run(prompt)`` — batch mode, minimal output, returns final result
    - ``run_stream(prompt)`` — stream mode, real-time Claude Code style printing
    - ``run_conversation(initial_prompt)`` — multi-round interactive conversation
    """

    def __init__(
        self,
        *,
        model: str = "opus",
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        tools: str | None = None,
        permission_mode: str | None = None,
        dangerously_skip_permissions: bool = False,
        system_prompt: str | None = None,
        append_system_prompt: str | None = None,
        mcp_config: list[str] | None = None,
        strict_mcp_config: bool = False,
        verbose: bool = True,
        max_budget_usd: float | None = None,
        effort: str | None = None,
        session_id: str | None = None,
        continue_session: str | None = None,
        resume_session: str | None = None,
        log_dir: str | None = None,
        log_path: str | None = None,
        output_dir: str | None = None,
        cwd: str | None = None,
    ) -> None:
        """Initialize the orchestrator with Claude CLI configuration.

        All parameters are keyword-only and map directly to ``claude`` CLI flags.

        Args:
            model: Model name or alias (e.g. ``"opus"``, ``"sonnet"``, ``"haiku"``,
                or a full model ID like ``"claude-opus-4-6"``). Default: ``"opus"``.
            allowed_tools: Whitelist of tool names. Each becomes a separate
                ``--allowedTools`` flag (e.g. ``["Bash(git:*)", "Read", "Glob"]``).
            disallowed_tools: Blacklist of tool names. Each becomes a separate
                ``--disallowedTools`` flag.
            tools: Raw ``--tools`` value passed directly to CLI (e.g. ``"default"``
                or ``""`` to disable all).
            permission_mode: One of ``"plan"``, ``"default"``, ``"acceptEdits"``,
                ``"bypassPermissions"``, ``"dontAsk"``. Ignored if
                ``dangerously_skip_permissions`` is True.
            dangerously_skip_permissions: If True, passes
                ``--dangerously-skip-permissions`` to bypass all permission checks.
                Overrides ``permission_mode``.
            system_prompt: Override the default system prompt entirely.
            append_system_prompt: Append text to the default system prompt.
            mcp_config: List of MCP server config file paths. Each becomes a
                separate ``--mcp-config`` flag.
            strict_mcp_config: If True, only use MCP servers from ``mcp_config``,
                ignoring all other MCP configurations.
            verbose: Pass ``--verbose`` to the CLI. Default: True.
            max_budget_usd: Maximum dollar amount to spend on API calls.
            effort: Effort level — ``"low"``, ``"medium"``, or ``"high"``.
            session_id: Use a specific UUID as the session ID.
            continue_session: Continue the most recent session. Pass ``"true"``
                or the session ID string.
            resume_session: Resume a specific previous session by its UUID.
            log_dir: Directory for auto-generated log files. Log files are named
                ``ccflow-YYYYMMDD-HHMMSS.log``. Ignored if ``log_path`` is set.
            log_path: Explicit path for the log file. Takes priority over ``log_dir``.
            output_dir: Directory to write ``result.output`` after each run. Output
                files are named ``ccflow-YYYYMMDD-HHMMSS.md``. Only writes when the
                run succeeds and produces output.
            cwd: Working directory for the ``claude`` subprocess. Defaults to the
                current working directory of the parent process.
        """
        self.model = model
        self.allowed_tools = allowed_tools
        self.disallowed_tools = disallowed_tools
        self.tools = tools
        self.permission_mode = permission_mode
        self.dangerously_skip_permissions = dangerously_skip_permissions
        self.system_prompt = system_prompt
        self.append_system_prompt = append_system_prompt
        self.mcp_config = mcp_config
        self.strict_mcp_config = strict_mcp_config
        self.verbose = verbose
        self.max_budget_usd = max_budget_usd
        self.effort = effort
        self.session_id = session_id
        self.continue_session = continue_session
        self.resume_session = resume_session
        self.log_dir = log_dir
        self.log_path = log_path
        self.output_dir = output_dir
        self.cwd = cwd

    # ── private helpers ──────────────────────────────────────

    def _build_cmd(self) -> list[str]:
        """Convert the orchestrator's configuration into a ``claude`` CLI argument list.

        Always includes ``-p``, ``--output-format stream-json``, and ``--model``.
        Conditionally appends flags based on which attributes are set.
        Tools in ``allowed_tools`` / ``disallowed_tools`` are each repeated as
        separate ``--allowedTools`` / ``--disallowedTools`` flags (the CLI expects
        one tool name per flag).

        Returns:
            List of strings suitable for ``subprocess.Popen()``.
        """
        cmd = [
            "claude", "-p",
            "--output-format", "stream-json",
            "--model", self.model,
        ]

        if self.verbose:
            cmd.append("--verbose")

        if self.dangerously_skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        elif self.permission_mode:
            cmd += ["--permission-mode", self.permission_mode]

        if self.tools:
            cmd += ["--tools", self.tools]

        if self.allowed_tools:
            for tool in self.allowed_tools:
                cmd += ["--allowedTools", tool]

        if self.disallowed_tools:
            for tool in self.disallowed_tools:
                cmd += ["--disallowedTools", tool]

        if self.system_prompt:
            cmd += ["--system-prompt", self.system_prompt]

        if self.append_system_prompt:
            cmd += ["--append-system-prompt", self.append_system_prompt]

        if self.mcp_config:
            for cfg in self.mcp_config:
                cmd += ["--mcp-config", cfg]

        if self.strict_mcp_config:
            cmd.append("--strict-mcp-config")

        if self.max_budget_usd is not None:
            cmd += ["--max-turns-budget", str(self.max_budget_usd)]

        if self.effort:
            cmd += ["--effort", self.effort]

        if self.session_id:
            cmd += ["--session-id", self.session_id]

        if self.continue_session:
            cmd += ["--continue", self.continue_session]

        if self.resume_session:
            cmd += ["--resume", self.resume_session]

        return cmd

    def _resolve_log_path(self) -> str | None:
        """Determine the log file path for this run.

        Priority: ``log_path`` (explicit) > ``log_dir`` (auto-generate) > None.
        When using ``log_dir``, generates a filename like
        ``ccflow-20260303-093015.log`` based on the current timestamp.

        Returns:
            Absolute or relative path string, or None if logging is disabled.
        """
        if self.log_path:
            return self.log_path
        if self.log_dir:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            return os.path.join(self.log_dir, f"ccflow-{ts}.log")
        return None

    def _write_output(self, result: ClaudeResult) -> str | None:
        """Write ``result.output`` to a file in ``output_dir`` if configured.

        Creates the directory if it doesn't exist. The filename is
        ``ccflow-YYYYMMDD-HHMMSS.md``. Only writes when the result has output.

        Args:
            result: The ``ClaudeResult`` from a completed run.

        Returns:
            The path to the written file, or None if nothing was written.
        """
        if not self.output_dir or not result.success or not result.output:
            return None
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        output_path = os.path.join(self.output_dir, f"ccflow-{ts}.md")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(result.output)
        return output_path

    def _open_log(self) -> tuple[str | None, IO[str] | None]:
        """Resolve log path and open the log file.

        Returns:
            Tuple of (log_path, log_file). Both are None if logging is disabled.
        """
        log_path = self._resolve_log_path()
        if not log_path:
            return None, None
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        return log_path, open(log_path, "w", encoding="utf-8")

    def _call(
        self,
        prompt: str,
        *,
        log_file: IO[str] | None = None,
        print_events: bool = False,
        print_banner: bool = False,
    ) -> ClaudeResult:
        """Core execution: spawn ``claude -p``, parse events, return result.

        This is the pure subprocess + parsing layer. It does NOT manage log file
        lifecycle, print summaries/banners (except inline streaming events), or
        write output files. Callers handle all decoration.

        Args:
            prompt: The prompt text to send to Claude via stdin.
            log_file: An already-open file handle for logging raw JSON lines.
                Caller owns the lifecycle. If None, no logging.
            print_events: If True, print streaming events via ``printer.print_event()``.
            print_banner: If True (and ``print_events`` is True), allow the
                ``system:init`` event to trigger the session banner.

        Returns:
            A ``ClaudeResult`` with all extracted metadata.
        """
        try:
            cmd = self._build_cmd()
            env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDECODE")}

            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                cwd=self.cwd,
            )

            proc.stdin.write(prompt)
            proc.stdin.close()

            result_output = None
            result_session_id = None
            result_duration_ms = 0
            result_duration_api_ms = 0
            result_cost_usd = None
            result_num_turns = None
            result_usage = None

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

                etype = event.get("type", "")

                if etype == "system" and event.get("subtype") == "init":
                    result_session_id = event.get("session_id")
                    if print_events and print_banner:
                        printer.print_event(event)
                elif etype == "result":
                    result_output = event.get("result")
                    result_duration_ms = event.get("duration_ms", 0)
                    result_duration_api_ms = event.get("duration_api_ms", 0)
                    result_session_id = event.get("session_id", result_session_id)
                    result_cost_usd = event.get("total_cost_usd")
                    result_num_turns = event.get("num_turns")
                    result_usage = event.get("usage")
                else:
                    if print_events:
                        printer.print_event(event)

            proc.wait()

            if proc.returncode != 0 and result_output is None:
                return ClaudeResult(
                    success=False,
                    error=f"claude exited with code {proc.returncode}",
                    session_id=result_session_id,
                )

            return ClaudeResult(
                success=True,
                output=result_output,
                duration_ms=result_duration_ms,
                duration_api_ms=result_duration_api_ms,
                session_id=result_session_id,
                cost_usd=result_cost_usd,
                num_turns=result_num_turns,
                usage=result_usage,
            )

        except FileNotFoundError:
            return ClaudeResult(
                success=False,
                error="'claude' CLI not found. Install Claude Code and ensure it is on PATH.",
            )
        except Exception as e:
            return ClaudeResult(success=False, error=str(e))

    def _print_batch_summary(self, result: ClaudeResult) -> None:
        """Print a one-liner batch summary (duration, cost, turns, tokens, session)."""
        parts = [f"Done ({result.duration_ms / 1000:.1f}s)"]
        if result.cost_usd is not None:
            parts.append(f"${result.cost_usd:.4f}")
        if result.num_turns is not None:
            parts.append(f"{result.num_turns} turns")
        if result.usage:
            inp = result.usage.get("input_tokens", 0)
            out = result.usage.get("output_tokens", 0)
            cache_read = result.usage.get("cache_read_input_tokens", 0)
            cache_write = result.usage.get("cache_creation_input_tokens", 0)
            token_parts = [f"{printer._fmt_tokens(inp)} in", f"{printer._fmt_tokens(out)} out"]
            if cache_read:
                token_parts.append(f"{printer._fmt_tokens(cache_read)} cached")
            if cache_write:
                token_parts.append(f"{printer._fmt_tokens(cache_write)} cache-write")
            parts.append(" + ".join(token_parts))
        ts = printer.timestamp()
        print(
            f"{printer.DIM}[{ts}]{printer.RESET} "
            f"{printer.BOLD}CCFlow{printer.RESET} "
            + "  │  ".join(parts),
            flush=True,
        )
        if result.session_id:
            ts = printer.timestamp()
            print(
                f"{printer.DIM}[{ts}]{printer.RESET} "
                f"{printer.BOLD}CCFlow{printer.RESET} "
                f"Session: {result.session_id}",
                flush=True,
            )

    def _print_output_saved(self, output_path: str) -> None:
        """Print notification that output was saved to disk."""
        ts = printer.timestamp()
        print(
            f"{printer.DIM}[{ts}]{printer.RESET} "
            f"{printer.BOLD}CCFlow{printer.RESET} "
            f"Output saved: {output_path}",
            flush=True,
        )

    @staticmethod
    def _accumulate_usage(totals: dict, round_usage: dict | None) -> dict:
        """Merge a round's usage dict into a running totals dict."""
        if not round_usage:
            return totals
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens"):
            totals[key] = totals.get(key, 0) + round_usage.get(key, 0)
        return totals

    # ── public API ───────────────────────────────────────────

    def run(self, prompt: str) -> ClaudeResult:
        """Run Claude in batch mode.

        Prints a single "Running..." line at the start and a summary line at
        the end (duration, cost, turns, token breakdown, session ID).
        No intermediate events are printed.

        Args:
            prompt: The prompt text to send to Claude.

        Returns:
            A ``ClaudeResult`` containing the final output and session metadata.
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

            result = self._call(prompt, log_file=log_file, print_events=False, print_banner=False)
            self._print_batch_summary(result)

            output_path = self._write_output(result)
            if output_path:
                self._print_output_saved(output_path)

            return result
        finally:
            if log_file:
                log_file.close()

    def run_stream(self, prompt: str) -> ClaudeResult:
        """Run Claude in stream mode with real-time formatted output.

        Prints events as they arrive in Claude Code CLI style:
        session banner, assistant text, tool calls, tool results,
        thinking indicators, and a result banner at the end.

        Args:
            prompt: The prompt text to send to Claude.

        Returns:
            A ``ClaudeResult`` containing the final output and session metadata.
        """
        _, log_file = self._open_log()
        try:
            result = self._call(prompt, log_file=log_file, print_events=True, print_banner=True)

            if result.success:
                printer.print_result_banner(
                    duration_ms=result.duration_ms,
                    cost_usd=result.cost_usd,
                    session_id=result.session_id,
                    num_turns=result.num_turns,
                    usage=result.usage,
                )

            output_path = self._write_output(result)
            if output_path:
                self._print_output_saved(output_path)

            return result
        finally:
            if log_file:
                log_file.close()

    def run_conversation(self, initial_prompt: str | None = None) -> list[ClaudeResult]:
        """Run an interactive multi-round conversation session.

        Opens one log file for the entire conversation. Prints the session
        banner on the first round only. Accumulates duration, cost, turns,
        and token usage across all rounds. Prints one aggregated summary
        at the end.

        The loop ends when the user types ``exit``, ``quit``, an empty line,
        or sends EOF (Ctrl-D).

        Args:
            initial_prompt: First prompt to send. If None, prompts interactively.

        Returns:
            List of ``ClaudeResult`` objects, one per round.
        """
        results: list[ClaudeResult] = []
        _, log_file = self._open_log()

        try:
            # Accumulation state
            total_duration_ms = 0
            total_cost_usd = 0.0
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
                prompt, log_file=log_file, print_events=True, print_banner=True,
            )
            results.append(result)

            total_duration_ms += result.duration_ms
            if result.cost_usd is not None:
                total_cost_usd += result.cost_usd
            if result.num_turns is not None:
                total_num_turns += result.num_turns
            total_usage = self._accumulate_usage(total_usage, result.usage)

            if not result.success or not result.session_id:
                printer.print_result_banner(
                    duration_ms=total_duration_ms,
                    cost_usd=total_cost_usd or None,
                    session_id=result.session_id,
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

                self.resume_session = result.session_id
                result = self._call(
                    user_input, log_file=log_file, print_events=True, print_banner=False,
                )
                results.append(result)

                total_duration_ms += result.duration_ms
                if result.cost_usd is not None:
                    total_cost_usd += result.cost_usd
                if result.num_turns is not None:
                    total_num_turns += result.num_turns
                total_usage = self._accumulate_usage(total_usage, result.usage)

                if not result.success:
                    break

            self.resume_session = orig_resume

            # One aggregated summary
            final_session_id = results[-1].session_id if results else None
            printer.print_result_banner(
                duration_ms=total_duration_ms,
                cost_usd=total_cost_usd or None,
                session_id=final_session_id,
                num_turns=total_num_turns or None,
                usage=total_usage or None,
                title="Conversation Complete",
                extra_lines=[f"Rounds: {len(results)}"],
            )

            # Write output from last successful round
            if results:
                output_path = self._write_output(results[-1])
                if output_path:
                    self._print_output_saved(output_path)

            return results

        finally:
            if log_file:
                log_file.close()
