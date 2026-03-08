"""CCFlow sandbox — deploy/remove PreToolUse hooks for directory confinement.

When sandbox mode is enabled, this module injects a PreToolUse hook into
the target project's .claude/settings.json that blocks file access outside
the sandbox directory. The hook script is referenced directly from the
CCFlow package (not copied), and settings.json is restored on teardown.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SandboxState:
    """State captured during setup, used for teardown."""

    settings_path: str
    original_settings: str | None  # None if settings.json didn't exist before
    created_claude_dir: bool


def _guard_script_path() -> str:
    """Absolute path to the bundled sandbox_guard.py hook script."""
    return os.path.join(os.path.dirname(__file__), "hooks", "sandbox_guard.py")


def setup_sandbox(cwd: str) -> SandboxState:
    """Inject PreToolUse sandbox hook into {cwd}/.claude/settings.json.

    Merges with existing settings — only touches hooks.PreToolUse,
    leaving all other settings and hook events intact.
    Idempotent: skips injection if the hook is already present.
    """
    claude_dir = os.path.join(cwd, ".claude")
    settings_path = os.path.join(claude_dir, "settings.json")

    created_claude_dir = not os.path.isdir(claude_dir)
    os.makedirs(claude_dir, exist_ok=True)

    # Read existing settings
    original_settings = None
    settings = {}
    if os.path.isfile(settings_path):
        original_settings = Path(settings_path).read_text(encoding="utf-8")
        try:
            settings = json.loads(original_settings)
        except json.JSONDecodeError:
            settings = {}

    # Build our hook entry — references the script directly from CCFlow package
    guard_cmd = f"python3 {_guard_script_path()}"
    our_hook = {
        "matcher": "Bash|Read|Write|Edit|Glob|Grep",
        "hooks": [{"type": "command", "command": guard_cmd}],
    }

    # Merge into PreToolUse (preserve existing hooks)
    hooks = settings.setdefault("hooks", {})
    pre_hooks = hooks.setdefault("PreToolUse", [])

    # Idempotent check: skip if our hook is already present
    already = any(
        any("sandbox_guard.py" in h.get("command", "") for h in entry.get("hooks", []))
        for entry in pre_hooks
    )
    if not already:
        pre_hooks.append(our_hook)

    # Write merged settings
    Path(settings_path).write_text(
        json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return SandboxState(
        settings_path=settings_path,
        original_settings=original_settings,
        created_claude_dir=created_claude_dir,
    )


def teardown_sandbox(state: SandboxState) -> None:
    """Restore settings.json to its original state.

    If settings.json existed before setup, restores the original content.
    If it didn't exist, removes it. Removes .claude/ if we created it
    and it's now empty.
    """
    if state.original_settings is not None:
        # Restore original content
        Path(state.settings_path).write_text(state.original_settings, encoding="utf-8")
    else:
        # We created it — remove
        if os.path.isfile(state.settings_path):
            os.remove(state.settings_path)

    # Remove .claude/ only if we created it and it's now empty
    if state.created_claude_dir:
        claude_dir = os.path.dirname(state.settings_path)
        try:
            os.rmdir(claude_dir)
        except OSError:
            pass  # not empty — other files exist, leave it
