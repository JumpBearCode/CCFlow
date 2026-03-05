"""Claude Code CLI style printer for stream-json events.

Reproduces the visual style of Claude Code's terminal output,
with [HH:MM:SS] timestamps on every line.
"""

import json
import sys
from datetime import datetime

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


def shorten(text: str, max_len: int = 200) -> str:
    """Collapse newlines and truncate text."""
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    return text[:max_len] + "..." if len(text) > max_len else text


def format_tool_input(tool_input: dict) -> str:
    """Intelligently extract the most useful parameters from tool input."""
    if not tool_input:
        return ""

    # Bash command
    if "command" in tool_input:
        return f'command={shorten(tool_input["command"], 120)!r}'

    # File path (Read, Write)
    if "file_path" in tool_input:
        s = f'file_path="{tool_input["file_path"]}"'
        if "pattern" in tool_input:
            s += f'  pattern="{tool_input["pattern"]}"'
        return s

    # Grep/Glob pattern + path
    if "pattern" in tool_input:
        s = f'pattern="{tool_input["pattern"]}"'
        if "path" in tool_input:
            s += f'  path="{tool_input["path"]}"'
        return s

    # Web search / fetch
    if "query" in tool_input:
        return f'query="{shorten(tool_input["query"], 120)}"'
    if "url" in tool_input:
        return f'url="{tool_input["url"]}"'

    # Skill
    if "skill" in tool_input:
        return f'skill="{tool_input["skill"]}"'

    # Agent prompt
    if "prompt" in tool_input:
        return f'prompt={shorten(tool_input["prompt"], 120)!r}'

    # Edit: old_string → new_string
    if "old_string" in tool_input:
        old = shorten(tool_input["old_string"], 50)
        new = shorten(tool_input.get("new_string", ""), 50)
        return f'"{old}" → "{new}"'

    # Selector-based tools (MCP browser etc.)
    if "selector" in tool_input:
        parts = [tool_input["selector"]]
        for key in ("value", "value_or_label"):
            if key in tool_input:
                parts.append(f'"{tool_input[key]}"')
        return " ".join(parts)

    # Fallback: JSON truncated
    return shorten(json.dumps(tool_input, ensure_ascii=False), 120)


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
