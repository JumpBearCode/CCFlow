"""Shared utility functions — leaf module with no ccflow imports."""

import json


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

    # Edit: old_string -> new_string
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
