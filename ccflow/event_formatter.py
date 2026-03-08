"""Pure formatter that converts stream-json events into short human-readable strings."""

from ccflow.printer import format_tool_input, shorten


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
