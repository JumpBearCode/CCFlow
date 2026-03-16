"""Codex CLI style printer for JSONL events.

Reproduces the visual style of CCFlow's terminal output,
adapted for Codex event format (thread/turn/item model).
"""

import sys
from datetime import datetime

from ccflow.utils import shorten

# ANSI codes matching Claude Code CLI
DIM = "\033[2m"
BOLD = "\033[1m"
RED = "\033[31m"
DIM_ITALIC = "\033[2;3m"
RESET = "\033[0m"

BANNER_WIDTH = 55


def timestamp() -> str:
    """Return current time as HH:MM:SS."""
    return datetime.now().strftime("%H:%M:%S")


def _ts_print(*args: str) -> None:
    """Print with [HH:MM:SS] prefix."""
    ts = f"{DIM}[{timestamp()}]{RESET}"
    print(ts, *args, flush=True)


def print_banner(model: str, thread_id: str | None = None) -> None:
    """Print session start banner."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _ts_print(f"{DIM}╭─{RESET} {BOLD}CCFlow/Codex Session{RESET} {DIM}{'─' * (BANNER_WIDTH - 23)}{RESET}")
    _ts_print(f"{DIM}│{RESET}  Model: {model}")
    _ts_print(f"{DIM}│{RESET}  Started: {now}")
    if thread_id:
        _ts_print(f"{DIM}│{RESET}  Thread: {thread_id}")
    _ts_print(f"{DIM}╰{'─' * BANNER_WIDTH}{RESET}")
    print(flush=True)


def _fmt_tokens(n: int) -> str:
    """Human-readable token count."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def print_result_banner(
    duration_ms: int,
    thread_id: str | None,
    num_turns: int | None,
    usage: dict | None = None,
    *,
    title: str = "Session Complete",
    extra_lines: list[str] | None = None,
) -> None:
    """Print session complete banner with optional token usage."""
    parts: list[str] = []
    parts.append(f"Duration: {duration_ms / 1000:.1f}s")
    if num_turns is not None:
        parts.append(f"Turns: {num_turns}")
    stats = "  │  ".join(parts)

    padding = BANNER_WIDTH - len(title) - 4
    print(flush=True)
    _ts_print(f"{DIM}╭─{RESET} {BOLD}{title}{RESET} {DIM}{'─' * padding}{RESET}")
    _ts_print(f"{DIM}│{RESET}  {stats}")

    if usage:
        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        cached = usage.get("cached_input_tokens", 0)
        token_parts = [f"{_fmt_tokens(inp)} in", f"{_fmt_tokens(out)} out"]
        if cached:
            token_parts.append(f"{_fmt_tokens(cached)} cached")
        _ts_print(f"{DIM}│{RESET}  Tokens: {' + '.join(token_parts)}")

    if extra_lines:
        for line in extra_lines:
            _ts_print(f"{DIM}│{RESET}  {line}")

    if thread_id:
        _ts_print(f"{DIM}│{RESET}  Thread: {thread_id}")
    _ts_print(f"{DIM}╰{'─' * BANNER_WIDTH}{RESET}")


def print_event(event: dict) -> None:
    """Dispatch a Codex JSONL event to the appropriate printer."""
    etype = event.get("type", "")

    if etype == "item.completed":
        print_item(event.get("item", {}))
    elif etype == "item.started":
        print_item_started(event.get("item", {}))
    elif etype == "error":
        print_error(event.get("message", "Unknown error"))
    elif etype == "turn.failed":
        print_error(event.get("message", "Turn failed"))


def print_item(item: dict) -> None:
    """Print a completed item: agent_message text, command result, or error."""
    item_type = item.get("type", "")

    if item_type == "agent_message":
        text = item.get("text", "").strip()
        if text:
            for line in text.split("\n"):
                _ts_print(line)

    elif item_type == "command_execution":
        cmd = item.get("command", "")
        exit_code = item.get("exit_code")
        output = item.get("output", "")

        # Show result
        if exit_code is not None and exit_code != 0:
            _ts_print(f"  {RED}⎿  Exit code: {exit_code}{RESET}")
            if output:
                _ts_print(f"  {RED}⎿  {shorten(output, 150)}{RESET}")
        else:
            if output:
                _ts_print(f"  {DIM}⎿  {shorten(output, 150)}{RESET}")
            else:
                _ts_print(f"  {DIM}⎿  Done{RESET}")

    elif item_type == "error":
        msg = item.get("message", "") or item.get("text", "")
        _ts_print(f"{RED}⚠ Error: {msg}{RESET}")


def print_item_started(item: dict) -> None:
    """Print when a command starts executing."""
    item_type = item.get("type", "")

    if item_type == "command_execution":
        cmd = item.get("command", "")
        # Strip shell wrapper prefix if present
        for prefix in ("/bin/zsh -lc ", "/bin/bash -lc ", "/bin/sh -c "):
            if cmd.startswith(prefix):
                cmd = cmd[len(prefix):]
                # Remove surrounding quotes if present
                if len(cmd) >= 2 and cmd[0] in ("'", '"') and cmd[-1] == cmd[0]:
                    cmd = cmd[1:-1]
                break
        _ts_print(f"{BOLD}⏺ shell{RESET}  {DIM}command={shorten(cmd, 120)!r}{RESET}")


def print_error(message: str) -> None:
    """Print a top-level error event."""
    _ts_print(f"{RED}⚠ {message}{RESET}")


def print_batch_summary(
    duration_ms: int,
    num_turns: int | None,
    usage: dict | None,
    thread_id: str | None,
) -> None:
    """Print a one-liner batch summary (duration, turns, tokens, thread)."""
    parts = [f"Done ({duration_ms / 1000:.1f}s)"]
    if num_turns is not None:
        parts.append(f"{num_turns} turns")
    if usage:
        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        cached = usage.get("cached_input_tokens", 0)
        token_parts = [f"{_fmt_tokens(inp)} in", f"{_fmt_tokens(out)} out"]
        if cached:
            token_parts.append(f"{_fmt_tokens(cached)} cached")
        parts.append(" + ".join(token_parts))
    ts = timestamp()
    print(
        f"{DIM}[{ts}]{RESET} "
        f"{BOLD}CCFlow{RESET} "
        + "  │  ".join(parts),
        flush=True,
    )
    if thread_id:
        ts = timestamp()
        print(
            f"{DIM}[{ts}]{RESET} "
            f"{BOLD}CCFlow{RESET} "
            f"Thread: {thread_id}",
            flush=True,
        )


def print_output_saved(output_path: str) -> None:
    """Print notification that output was saved to disk."""
    ts = timestamp()
    print(
        f"{DIM}[{ts}]{RESET} "
        f"{BOLD}CCFlow{RESET} "
        f"Output saved: {output_path}",
        flush=True,
    )
