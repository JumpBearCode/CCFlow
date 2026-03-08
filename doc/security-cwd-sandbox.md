# Security: --cwd is NOT a Sandbox

## Problem (discovered 2026-03-08)

CCFlow's `--cwd` flag only sets `subprocess.Popen(cwd=...)` for the `claude` subprocess. It does NOT restrict Claude's file/bash access to that directory.

Claude's inner model can freely:
- `cd /anywhere` in Bash commands
- Use absolute paths to read/write files outside cwd
- Create projects in parent directories

### Evidence

Log `ccflow-20260308-002453.log`: Claude was given `--cwd playground` but executed:
```
cd /Users/wqeq/Desktop/project && npm create vite@latest portfolio
```
Creating files in CCFlow's **parent directory**, completely escaping the intended scope.

### Root Cause

- `--cwd` is a CCFlow parameter (cli.py:27), not a `claude` CLI parameter
- It only sets the initial working directory via `subprocess.Popen(cwd=self.cwd)` (orchestrator.py:302)
- `claude` CLI itself has no `--cwd` flag and no directory sandboxing mechanism
- Claude Code resets shell cwd after each Bash call, but does NOT block absolute paths

## Solution: Dual-Layer Defense

### Layer 1: Soft Constraint — `--append-system-prompt`

Tell the model not to leave the directory. ~99% effective since models usually comply.

```python
orc = ClaudeOrchestrator(
    append_system_prompt=f"You MUST NOT access files or directories outside {cwd}. Never use cd or absolute paths to leave this directory.",
    cwd=cwd,
)
```

### Layer 2: Hard Constraint — Claude Code PreToolUse Hooks

Intercept tool calls before execution. Configure in `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Read|Write|Edit|Glob|Grep",
        "hooks": [
          {
            "type": "command",
            "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/sandbox-guard.sh"
          }
        ]
      }
    ]
  }
}
```

Hook receives JSON on stdin with `tool_name` and `tool_input`. To deny:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Path outside allowed directory"
  }
}
```

### Limitation

Bash command path parsing is inherently imperfect — variable expansion, subshells, symlinks can bypass string-based checks. For absolute security against malicious users, Docker/VM is the only option. But soft constraint + hooks is sufficient for normal use cases (e.g., Telegram bot).

## Docker Alternative (rejected for now)

User prefers not to use Docker because:
1. Existing CLI tools (az, gh) are already authenticated in the host environment
2. Re-authenticating inside containers for every session is too cumbersome
