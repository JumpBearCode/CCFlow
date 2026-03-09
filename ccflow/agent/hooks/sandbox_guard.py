#!/usr/bin/env python3
"""CCFlow sandbox guard — PreToolUse hook that blocks file access outside the sandbox.

Deployed automatically by CCFlow when sandbox mode is enabled.
Reads CCFLOW_SANDBOX_DIR from environment to determine the allowed directory.
Receives tool call JSON on stdin from Claude Code's hook system.

For structured tools (Read/Write/Edit/Glob/Grep), path checking is reliable.
For Bash, only heuristic string matching is possible — see doc/security-cwd-sandbox.md.
"""

import json
import os
import re
import sys


def main():
    allowed_dir = os.environ.get("CCFLOW_SANDBOX_DIR")
    if not allowed_dir:
        sys.exit(0)

    allowed_dir = os.path.realpath(allowed_dir)

    try:
        event = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {})

    def is_allowed(path):
        if not path:
            return True
        resolved = os.path.realpath(path)
        return resolved == allowed_dir or resolved.startswith(allowed_dir + os.sep)

    def deny(reason):
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            sys.stdout,
        )
        sys.exit(0)

    # ── Structured tools: reliable path checking ──

    if tool_name in ("Read", "Write", "Edit"):
        file_path = tool_input.get("file_path", "")
        if file_path and not is_allowed(file_path):
            deny(f"Blocked: '{file_path}' is outside sandbox '{allowed_dir}'")

    elif tool_name in ("Glob", "Grep"):
        search_path = tool_input.get("path", "")
        if search_path and not is_allowed(search_path):
            deny(f"Blocked: search path '{search_path}' is outside sandbox '{allowed_dir}'")

    # ── Bash: best-effort heuristic checks ──

    elif tool_name == "Bash":
        cmd = tool_input.get("command", "")

        # 1. cd to absolute path outside sandbox
        for m in re.finditer(r"cd\s+[\"']?(/[^\s;|&\"']+)", cmd):
            if not is_allowed(m.group(1)):
                deny(f"Blocked: 'cd {m.group(1)}' escapes sandbox '{allowed_dir}'")

        # 2. Absolute paths under user/home directories (main escape vector)
        for m in re.finditer(r"(/(?:Users|home|root)/[^\s;|&\"')\]>]+)", cmd):
            if not is_allowed(m.group(1)):
                deny(f"Blocked: path '{m.group(1)}' is outside sandbox '{allowed_dir}'")

        # 3. .. traversal that resolves outside sandbox
        for m in re.finditer(r"(?:^|[\s;|&(])(\.\.[^\s;|&\"']*)", cmd):
            rel = m.group(1)
            resolved = os.path.realpath(os.path.join(allowed_dir, rel))
            if not is_allowed(resolved):
                deny(f"Blocked: '{rel}' resolves outside sandbox '{allowed_dir}'")

    sys.exit(0)


if __name__ == "__main__":
    main()
