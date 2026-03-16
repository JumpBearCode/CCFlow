"""Pure formatter that converts stream-json events into human-readable strings.

Contains both the generic format_event() and Telegram-specific formatting logic.
"""

import html
import io
import re

from ccflow.utils import format_tool_input, shorten


def format_event(event: dict) -> str | None:
    """Convert a raw stream-json event dict into a short human-readable string.

    Returns None for events that should be suppressed (e.g. allowed rate limits).

    Args:
        event: A parsed JSON event from Claude's stream-json output.

    Returns:
        A short descriptive string, or None to suppress the event.
    """
    etype = event.get("type", "")

    # system:init
    if etype == "system" and event.get("subtype") == "init":
        model = event.get("model", "unknown")
        return f"Session started (model: {model})"

    # assistant message blocks
    if etype == "assistant":
        message = event.get("message", {})
        parts: list[str] = []
        for block in message.get("content", []):
            block_type = block.get("type", "")

            if block_type == "tool_use":
                name = block.get("name", "?")
                tool_input = block.get("input", {})
                params = format_tool_input(tool_input)
                if params:
                    parts.append(f"Tool: {name}  {params}")
                else:
                    parts.append(f"Tool: {name}")

            elif block_type == "thinking":
                parts.append("Thinking...")

            elif block_type == "text":
                text = block.get("text", "").strip()
                if text:
                    parts.append(text)

        return "\n".join(parts) if parts else None

    # user message (tool results)
    if etype == "user":
        message = event.get("message", {})
        for block in message.get("content", []):
            if block.get("type") == "tool_result":
                if block.get("is_error", False):
                    content = block.get("content", "")
                    return f"Error: {shorten(str(content), 150)}"
                return "Done"
        return None

    # result
    if etype == "result":
        duration_s = event.get("duration_ms", 0) / 1000
        cost = event.get("total_cost_usd")
        parts = [f"Completed ({duration_s:.1f}s"]
        if cost is not None:
            parts[0] += f", ${cost:.4f})"
        else:
            parts[0] += ")"
        return parts[0]

    # rate limit
    if etype == "rate_limit_event":
        info = event.get("rate_limit_info", {})
        if info.get("status") != "allowed":
            resets_at = info.get("resetsAt", "?")
            return f"Rate limited - resets at {resets_at}"
        return None

    return None


# ── Telegram-specific formatting ─────────────────────────────

_TOOL_EMOJIS: dict[str, str] = {
    "Bash": "\u2328\ufe0f",        # ⌨️
    "Read": "\U0001F4C4",          # 📄
    "Write": "\u270f\ufe0f",       # ✏️
    "Edit": "\u270f\ufe0f",        # ✏️
    "Glob": "\U0001F50D",          # 🔍
    "Grep": "\U0001F50D",          # 🔍
    "Agent": "\U0001F916",         # 🤖
    "WebSearch": "\U0001F310",     # 🌐
    "WebFetch": "\U0001F310",      # 🌐
}
_DEFAULT_TOOL_EMOJI = "\U0001F6E0\ufe0f"   # 🛠️


def _tool_emoji(name: str) -> str:
    """Return an emoji for the given tool name."""
    return _TOOL_EMOJIS.get(name, _DEFAULT_TOOL_EMOJI)


def _event_to_telegram(event: dict) -> list[tuple[str, str]]:
    """Classify an event into a list of (category, formatted_text) pairs for Telegram.

    Categories: "tool", "tool_done", "tool_error", "text", "thinking", "result", "status".
    Returns an empty list if the event should be suppressed entirely.
    """
    etype = event.get("type", "")

    if etype == "assistant":
        items: list[tuple[str, str]] = []
        message = event.get("message", {})
        for block in message.get("content", []):
            block_type = block.get("type", "")

            if block_type == "tool_use":
                name = block.get("name", "?")
                emoji = _tool_emoji(name)
                tool_input = block.get("input", {})
                params = format_tool_input(tool_input)
                if params:
                    items.append(("tool", f"{emoji} Tool: {name}  {params}"))
                else:
                    items.append(("tool", f"{emoji} Tool: {name}"))

            elif block_type == "thinking":
                thinking_text = block.get("thinking", "").strip()
                if thinking_text:
                    items.append(("thinking", f"\U0001F4A1 {shorten(thinking_text, 300)}"))
                else:
                    items.append(("thinking", "\U0001F4A1 Thinking..."))

            elif block_type == "text":
                text = block.get("text", "").strip()
                if text:
                    items.append(("text", text))

        return items

    if etype == "user":
        message = event.get("message", {})
        for block in message.get("content", []):
            if block.get("type") == "tool_result":
                if block.get("is_error", False):
                    content = block.get("content", "")
                    return [("tool_error", f"\u274c Error: {shorten(str(content), 150)}")]
                return [("tool_done", "\u2705 Done")]
        return []

    if etype == "system" and event.get("subtype") == "init":
        return [("status", format_event(event) or "Session started")]

    if etype == "result":
        duration_s = event.get("duration_ms", 0) / 1000
        cost = event.get("total_cost_usd")
        num_turns = event.get("num_turns")
        usage = event.get("usage", {}) or {}
        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)

        parts = [f"{duration_s:.1f}s"]
        if cost is not None:
            parts.append(f"${cost:.4f}")
        if num_turns is not None:
            parts.append(f"{num_turns} turns")
        if inp or out:
            parts.append(f"{inp}in/{out}out")
        return [("result", f"\u2705 Completed ({' | '.join(parts)})")]

    if etype == "rate_limit_event":
        formatted = format_event(event)
        if formatted:
            return [("status", f"\u26a0\ufe0f {formatted}")]
        return []

    return []


def _codex_event_to_telegram(event: dict) -> list[tuple[str, str]]:
    """Classify a Codex JSONL event into (category, formatted_text) pairs for Telegram.

    Categories match ``_event_to_telegram``: "tool", "tool_done", "tool_error",
    "text", "result", "status".  Returns an empty list for suppressed events.
    """
    etype = event.get("type", "")

    if etype == "item.started":
        item = event.get("item", {})
        if item.get("type") == "command_execution":
            cmd = item.get("command", "")
            # Strip shell wrapper prefix
            for prefix in ("/bin/zsh -lc ", "/bin/bash -lc ", "/bin/sh -c "):
                if cmd.startswith(prefix):
                    cmd = cmd[len(prefix):]
                    if len(cmd) >= 2 and cmd[0] in ("'", '"') and cmd[-1] == cmd[0]:
                        cmd = cmd[1:-1]
                    break
            return [("tool", f"\u2328\ufe0f shell  command={shorten(cmd, 120)!r}")]
        return []

    if etype == "item.completed":
        item = event.get("item", {})
        item_type = item.get("type", "")

        if item_type == "agent_message":
            text = item.get("text", "").strip()
            if text:
                return [("text", text)]
            return []

        if item_type == "command_execution":
            exit_code = item.get("exit_code")
            if exit_code is not None and exit_code != 0:
                output = item.get("output", "")
                msg = f"Exit code: {exit_code}"
                if output:
                    msg += f" — {shorten(output, 150)}"
                return [("tool_error", f"\u274c {msg}")]
            return [("tool_done", "\u2705 Done")]

        return []

    if etype == "turn.completed":
        usage = event.get("usage") or {}
        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        num_turns = 1  # each turn.completed = 1 turn
        parts = [f"{num_turns} turn"]
        if inp or out:
            parts.append(f"{inp}in/{out}out")
        return [("result", f"\u2705 Turn completed ({' | '.join(parts)})")]

    if etype == "error":
        msg = event.get("message", "Unknown error")
        return [("tool_error", f"\u274c {msg}")]

    if etype == "turn.failed":
        msg = event.get("message", "Turn failed")
        return [("tool_error", f"\u274c {msg}")]

    if etype == "thread.started":
        thread_id = event.get("thread_id", "")
        return [("status", f"Codex session started (thread: {shorten(thread_id, 20)})")]

    return []


def _split_message(text: str, max_len: int = 4096) -> list[str]:
    """Split text into chunks that fit Telegram's message size limit."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # Try to split at last newline before limit
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def _markdown_to_telegram_html(text: str) -> str:
    """Convert standard Markdown to Telegram-compatible HTML.

    Handles fenced code blocks, inline code, bold, italic,
    strikethrough, headers, and links.
    """
    # 1. Stash fenced code blocks so inner content is not processed.
    code_blocks: list[str] = []

    def _stash_code_block(m: re.Match) -> str:
        lang = m.group(1) or ""
        code = html.escape(m.group(2).strip("\n"))
        if lang:
            code_blocks.append(
                f'<pre><code class="language-{lang}">{code}</code></pre>'
            )
        else:
            code_blocks.append(f"<pre>{code}</pre>")
        return f"\x00CB{len(code_blocks) - 1}\x00"

    text = re.sub(r"```(\w*)\n?(.*?)```", _stash_code_block, text, flags=re.DOTALL)

    # 2. Stash inline code spans.
    inline_codes: list[str] = []

    def _stash_inline(m: re.Match) -> str:
        inline_codes.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00IC{len(inline_codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", _stash_inline, text)

    # 3. Escape HTML entities in the remaining (non-code) text.
    text = html.escape(text)

    # 4. Bold: **text**
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    # 5. Italic: *text* (not adjacent to word characters)
    text = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"<i>\1</i>", text)
    # 6. Strikethrough: ~~text~~
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    # 7. Headers: # … → bold line
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    # 8. Links: [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)

    # 9. Restore stashed blocks.
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CB{i}\x00", block)
    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00IC{i}\x00", code)

    return text


# Regex: match a markdown table (header + separator + data rows)
_TABLE_RE = re.compile(
    r"(\|[^\n]+\|\s*\n"          # header row:    | col1 | col2 |
    r"\|[-\s:|]+\|\s*\n"         # separator row: |------|------|
    r"(?:\|[^\n]+\|\s*\n?)+)",   # data rows (one or more)
)


def _parse_table(table_md: str) -> tuple[list[str], list[list[str]]]:
    """Parse a markdown table string into (headers, rows)."""
    lines = [ln.strip() for ln in table_md.strip().splitlines() if ln.strip()]
    def _parse_row(line: str) -> list[str]:
        # strip leading/trailing pipes, split by |
        return [cell.strip() for cell in line.strip("|").split("|")]
    headers = _parse_row(lines[0])
    # lines[1] is the separator, skip it
    rows = [_parse_row(ln) for ln in lines[2:]]
    return headers, rows


def _cell_to_html(text: str) -> str:
    """Convert a markdown table cell to HTML, handling **bold** markers."""
    parts = re.split(r"(\*\*.+?\*\*)", text)
    result: list[str] = []
    for part in parts:
        if part.startswith("**") and part.endswith("**"):
            result.append(f"<b>{html.escape(part[2:-2])}</b>")
        else:
            result.append(html.escape(part))
    return "".join(result)


def _table_md_to_styled_html(table_md: str) -> str:
    """Convert a markdown table to a self-contained styled HTML page."""
    headers, rows = _parse_table(table_md)

    header_cells = "".join(f"<th>{_cell_to_html(h)}</th>" for h in headers)
    body_rows = []
    for row in rows:
        # Pad if shorter than headers
        while len(row) < len(headers):
            row.append("")
        cells = "".join(f"<td>{_cell_to_html(c)}</td>" for c in row)
        body_rows.append(f"<tr>{cells}</tr>")

    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
        "body{margin:0;padding:8px;background:white}"
        "table{border-collapse:collapse;"
        'font-family:-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,'
        '"Noto Sans SC","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;'
        "font-size:15px;white-space:nowrap}"
        "th,td{border:1px solid #d0d7de;padding:8px 14px;text-align:left}"
        "th{background:#4a90d9;color:white;font-weight:600}"
        "tr:nth-child(even){background:#f0f4fa}"
        "</style></head><body>"
        f"<table><thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table>"
        "</body></html>"
    )


def _render_table_image(table_md: str) -> io.BytesIO:
    """Render a markdown table as a PNG image using Playwright (headless Chromium)."""
    from playwright.sync_api import sync_playwright

    html_content = _table_md_to_styled_html(table_md)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html_content, wait_until="networkidle")
        screenshot_bytes = page.locator("table").screenshot(type="png")
        page.close()
        browser.close()

    return io.BytesIO(screenshot_bytes)


def _split_text_and_tables(text: str) -> list[tuple[str, str]]:
    """Split markdown text into alternating ("text", ...) and ("table", ...) segments.

    Code blocks are protected — tables inside fenced code blocks are not extracted.
    """
    # Stash fenced code blocks to avoid matching tables inside them
    code_stash: list[str] = []

    def _stash(m: re.Match) -> str:
        code_stash.append(m.group(0))
        return f"\x00CODE{len(code_stash) - 1}\x00"

    protected = re.sub(r"```.*?```", _stash, text, flags=re.DOTALL)

    segments: list[tuple[str, str]] = []
    last_end = 0

    for m in _TABLE_RE.finditer(protected):
        start = m.start()
        # If the match doesn't start at position 0, there may be a leading newline
        # that is part of the regex but not part of the table itself
        table_text = m.group(0).strip("\n")

        # Text before this table
        before = protected[last_end:start].strip("\n")
        if before:
            # Restore code blocks in the text segment
            for i, block in enumerate(code_stash):
                before = before.replace(f"\x00CODE{i}\x00", block)
            segments.append(("text", before))

        # Restore code blocks in table (shouldn't happen, but be safe)
        for i, block in enumerate(code_stash):
            table_text = table_text.replace(f"\x00CODE{i}\x00", block)
        segments.append(("table", table_text))

        last_end = m.end()

    # Remaining text after last table
    remaining = protected[last_end:].strip("\n")
    if remaining:
        for i, block in enumerate(code_stash):
            remaining = remaining.replace(f"\x00CODE{i}\x00", block)
        segments.append(("text", remaining))

    # If no tables found, return the whole thing as text
    if not segments:
        segments.append(("text", text))

    return segments
