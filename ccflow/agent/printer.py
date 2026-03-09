"""Claude Code CLI style printer for stream-json events.

Reproduces the visual style of Claude Code's terminal output,
with [HH:MM:SS] timestamps on every line.
"""

import sys
from datetime import datetime

from ccflow.utils import format_tool_input, shorten

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


def print_banner(model: str, session_id: str | None = None) -> None:
    """Print session start banner."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _ts_print(f"{DIM}╭─{RESET} {BOLD}CCFlow Session{RESET} {DIM}{'─' * (BANNER_WIDTH - 17)}{RESET}")
    _ts_print(f"{DIM}│{RESET}  Model: {model}")
    _ts_print(f"{DIM}│{RESET}  Started: {now}")
    if session_id:
        _ts_print(f"{DIM}│{RESET}  Session: {session_id}")
    _ts_print(f"{DIM}╰{'─' * BANNER_WIDTH}{RESET}")
    print(flush=True)


def print_result_banner(
    duration_ms: int,
    cost_usd: float | None,
    session_id: str | None,
    num_turns: int | None,
    usage: dict | None = None,
    *,
    title: str = "Session Complete",
    extra_lines: list[str] | None = None,
) -> None:
    """Print session complete banner with optional token usage."""
    parts: list[str] = []
    parts.append(f"Duration: {duration_ms / 1000:.1f}s")
    if cost_usd is not None:
        parts.append(f"Cost: ${cost_usd:.4f}")
    if num_turns is not None:
        parts.append(f"Turns: {num_turns}")
    stats = "  │  ".join(parts)

    padding = BANNER_WIDTH - len(title) - 4
    print(flush=True)
    _ts_print(f"{DIM}╭─{RESET} {BOLD}{title}{RESET} {DIM}{'─' * padding}{RESET}")
    _ts_print(f"{DIM}│{RESET}  {stats}")

    # Per-run token breakdown
    if usage:
        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_write = usage.get("cache_creation_input_tokens", 0)
        token_parts = [f"{_fmt_tokens(inp)} in", f"{_fmt_tokens(out)} out"]
        if cache_read:
            token_parts.append(f"{_fmt_tokens(cache_read)} cached")
        if cache_write:
            token_parts.append(f"{_fmt_tokens(cache_write)} cache-write")
        _ts_print(f"{DIM}│{RESET}  Tokens: {' + '.join(token_parts)}")

    if extra_lines:
        for line in extra_lines:
            _ts_print(f"{DIM}│{RESET}  {line}")

    if session_id:
        _ts_print(f"{DIM}│{RESET}  Session: {session_id}")
    _ts_print(f"{DIM}╰{'─' * BANNER_WIDTH}{RESET}")


def _fmt_tokens(n: int) -> str:
    """Human-readable token count."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def print_event(event: dict) -> None:
    """Dispatch a stream-json event to the appropriate printer."""
    etype = event.get("type", "")

    # ── system init ──
    if etype == "system" and event.get("subtype") == "init":
        model = event.get("model", "unknown")
        session_id = event.get("session_id")
        print_banner(model, session_id)
        return

    # ── assistant message ──
    if etype == "assistant":
        message = event.get("message", {})
        for block in message.get("content", []):
            block_type = block.get("type", "")

            if block_type == "text":
                text = block.get("text", "").strip()
                if text:
                    for line in text.split("\n"):
                        _ts_print(line)

            elif block_type == "thinking":
                _ts_print(f"{DIM_ITALIC}(thinking...){RESET}")

            elif block_type == "tool_use":
                name = block.get("name", "?")
                tool_input = block.get("input", {})
                params = format_tool_input(tool_input)
                if params:
                    _ts_print(f"{BOLD}⏺ {name}{RESET}  {DIM}{params}{RESET}")
                else:
                    _ts_print(f"{BOLD}⏺ {name}{RESET}")
        return

    # ── user message (tool results) ──
    if etype == "user":
        message = event.get("message", {})
        for block in message.get("content", []):
            if block.get("type") == "tool_result":
                is_error = block.get("is_error", False)
                if is_error:
                    content = block.get("content", "")
                    err_text = shorten(str(content), 150)
                    _ts_print(f"  {RED}⎿  Error: {err_text}{RESET}")
                else:
                    _ts_print(f"  {DIM}⎿  Done{RESET}")
        return

    # ── result ──
    if etype == "result":
        print_result_banner(
            duration_ms=event.get("duration_ms", 0),
            cost_usd=event.get("total_cost_usd"),
            session_id=event.get("session_id"),
            num_turns=event.get("num_turns"),
        )
        return

    # ── rate limit ──
    if etype == "rate_limit_event":
        info = event.get("rate_limit_info", {})
        if info.get("status") != "allowed":
            resets_at = info.get("resetsAt", "?")
            _ts_print(f"{RED}⚠ Rate limited — resets at {resets_at}{RESET}")
        return


def print_batch_summary(
    duration_ms: int,
    cost_usd: float | None,
    num_turns: int | None,
    usage: dict | None,
    session_id: str | None,
) -> None:
    """Print a one-liner batch summary (duration, cost, turns, tokens, session)."""
    parts = [f"Done ({duration_ms / 1000:.1f}s)"]
    if cost_usd is not None:
        parts.append(f"${cost_usd:.4f}")
    if num_turns is not None:
        parts.append(f"{num_turns} turns")
    if usage:
        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_write = usage.get("cache_creation_input_tokens", 0)
        token_parts = [f"{_fmt_tokens(inp)} in", f"{_fmt_tokens(out)} out"]
        if cache_read:
            token_parts.append(f"{_fmt_tokens(cache_read)} cached")
        if cache_write:
            token_parts.append(f"{_fmt_tokens(cache_write)} cache-write")
        parts.append(" + ".join(token_parts))
    ts = timestamp()
    print(
        f"{DIM}[{ts}]{RESET} "
        f"{BOLD}CCFlow{RESET} "
        + "  │  ".join(parts),
        flush=True,
    )
    if session_id:
        ts = timestamp()
        print(
            f"{DIM}[{ts}]{RESET} "
            f"{BOLD}CCFlow{RESET} "
            f"Session: {session_id}",
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
