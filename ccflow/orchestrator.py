"""Claude Code orchestrator — wraps `claude -p` CLI as a reusable Python class."""

import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ccflow import printer
from ccflow.tracker import UsageTracker


@dataclass
class ClaudeResult:
    """Result from a Claude CLI invocation."""

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

    Supports two execution modes:
    - ``run(prompt)`` — batch mode, minimal output, returns final result
    - ``run_stream(prompt)`` — stream mode, real-time Claude Code style printing
    """

    def __init__(
        self,
        *,
        model: str = "sonnet",
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        tools: str | None = None,
        permission_mode: str | None = None,
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
        cwd: str | None = None,
        usage_tracker: UsageTracker | bool = True,
    ) -> None:
        self.model = model
        self.allowed_tools = allowed_tools
        self.disallowed_tools = disallowed_tools
        self.tools = tools
        self.permission_mode = permission_mode
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
        self.cwd = cwd

        # Usage tracker: True = default tracker, False = disabled, or pass your own
        if usage_tracker is True:
            self.tracker = UsageTracker()
        elif isinstance(usage_tracker, UsageTracker):
            self.tracker = usage_tracker
        else:
            self.tracker = None

    # ── private helpers ──────────────────────────────────────

    def _build_cmd(self) -> list[str]:
        """Convert configuration into a ``claude`` CLI argument list."""
        cmd = [
            "claude", "-p",
            "--output-format", "stream-json",
            "--model", self.model,
        ]

        if self.verbose:
            cmd.append("--verbose")

        if self.permission_mode:
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
        """Return an explicit log path, or auto-generate one from log_dir."""
        if self.log_path:
            return self.log_path
        if self.log_dir:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            return os.path.join(self.log_dir, f"ccflow-{ts}.log")
        return None

    def _execute(self, prompt: str, *, stream: bool) -> ClaudeResult:
        """Shared execution core for both batch and stream modes."""
        cmd = self._build_cmd()
        log_path = self._resolve_log_path()
        log_file = None

        if log_path:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            log_file = open(log_path, "w", encoding="utf-8")

        try:
            # Remove CLAUDECODE env vars to support nested invocations
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

            # Send prompt via stdin
            proc.stdin.write(prompt)
            proc.stdin.close()

            # State for result extraction
            result_output = None
            result_session_id = None
            result_duration_ms = 0
            result_duration_api_ms = 0
            result_cost_usd = None
            result_num_turns = None
            result_usage = None

            if not stream:
                ts = printer.timestamp()
                print(
                    f"{printer.DIM}[{ts}]{printer.RESET} "
                    f"{printer.BOLD}CCFlow{printer.RESET} "
                    f"Running ({self.model})...",
                    flush=True,
                )

            # Read stdout line by line
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue

                # Write raw line to log
                if log_file:
                    log_file.write(line + "\n")
                    log_file.flush()

                # Parse JSON event
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # Non-JSON line (stderr passthrough)
                    if stream:
                        ts = printer.timestamp()
                        print(
                            f"{printer.DIM}[{ts}] [stderr] {line}{printer.RESET}",
                            flush=True,
                        )
                    continue

                etype = event.get("type", "")

                # Stream mode: print every event EXCEPT result (we handle it below)
                if stream and etype != "result":
                    printer.print_event(event)

                # Extract session_id from init
                if etype == "system" and event.get("subtype") == "init":
                    result_session_id = event.get("session_id")

                # Extract metadata from result
                if etype == "result":
                    result_output = event.get("result")
                    result_duration_ms = event.get("duration_ms", 0)
                    result_duration_api_ms = event.get("duration_api_ms", 0)
                    result_session_id = event.get("session_id", result_session_id)
                    result_cost_usd = event.get("total_cost_usd")
                    result_num_turns = event.get("num_turns")
                    result_usage = event.get("usage")
                    result_model_usage = event.get("modelUsage")

            proc.wait()

            # Record usage and get rolling window stats
            tracker_lines: list[str] | None = None
            if self.tracker and result_model_usage:
                self.tracker.record(result_model_usage, session_id=result_session_id)
                tracker_lines = self.tracker.format_stats()

            # Print completion — stream gets full banner, batch gets one-liner
            if stream:
                printer.print_result_banner(
                    duration_ms=result_duration_ms,
                    cost_usd=result_cost_usd,
                    session_id=result_session_id,
                    num_turns=result_num_turns,
                    usage=result_usage,
                    tracker_lines=tracker_lines,
                )
            else:
                parts = [f"Done ({result_duration_ms / 1000:.1f}s)"]
                if result_cost_usd is not None:
                    parts.append(f"${result_cost_usd:.4f}")
                if result_num_turns is not None:
                    parts.append(f"{result_num_turns} turns")
                ts = printer.timestamp()
                print(
                    f"{printer.DIM}[{ts}]{printer.RESET} "
                    f"{printer.BOLD}CCFlow{printer.RESET} "
                    + "  │  ".join(parts),
                    flush=True,
                )
                if tracker_lines:
                    for line in tracker_lines:
                        ts = printer.timestamp()
                        print(
                            f"{printer.DIM}[{ts}]{printer.RESET} "
                            f"{printer.BOLD}CCFlow{printer.RESET} {line}",
                            flush=True,
                        )

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
        finally:
            if log_file:
                log_file.close()

    # ── public API ───────────────────────────────────────────

    def run(self, prompt: str) -> ClaudeResult:
        """Run in batch mode — minimal output, returns final result."""
        return self._execute(prompt, stream=False)

    def run_stream(self, prompt: str) -> ClaudeResult:
        """Run in stream mode — real-time Claude Code style printing."""
        return self._execute(prompt, stream=True)
