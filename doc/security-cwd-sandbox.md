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

## Solution: Dual-Layer Defense (implemented)

Both layers are now built into CCFlow via the `sandbox` parameter. Enable with `--sandbox --cwd /path` (CLI) or `sandbox=True, cwd="/path"` (Python API).

### Layer 1: Soft Constraint — `append_system_prompt` (auto-injected)

When `sandbox=True`, the orchestrator automatically prepends a constraint to `append_system_prompt`:

> CRITICAL CONSTRAINT: You MUST NOT access files or directories outside '/path/to/cwd'. Never use 'cd' with absolute paths or '..' to escape this directory.

~99% effective since models usually comply. No manual prompt configuration needed.

### Layer 2: Hard Constraint — PreToolUse Hook (auto-deployed)

When `sandbox=True`, the orchestrator automatically:

1. Creates `{cwd}/.claude/settings.json` (or merges into existing)
2. Injects a PreToolUse hook referencing `ccflow/hooks/sandbox_guard.py`
3. Sets `CCFLOW_SANDBOX_DIR` env var for the subprocess
4. On session end, restores the original `settings.json` (or removes it if newly created)

The hook script (`ccflow/hooks/sandbox_guard.py`) receives tool call JSON on stdin and checks:

| Tool | Check method | Reliability |
|---|---|---|
| Read, Write, Edit | `file_path` prefix match against sandbox dir | ~100% |
| Glob, Grep | `path` prefix match against sandbox dir | ~100% |
| Bash | Heuristic regex on command string | ~90-95% |

**Bash heuristics** catch three patterns:
1. `cd /absolute/path` — direct directory escape
2. `/Users/...` or `/home/...` paths — absolute path references outside sandbox
3. `..` traversal — relative paths that resolve outside sandbox

To deny a tool call, the hook outputs:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Blocked: '/path' is outside sandbox '/allowed/dir'"
  }
}
```

### Usage

```bash
# CLI
ccflow "analyze" --cwd /path/to/project --sandbox --danger

# .env (defaults, overridden by CLI flags)
CCFLOW_CWD="/path/to/project"
CCFLOW_SANDBOX="true"
```

```python
# Python API
orc = ClaudeOrchestrator(cwd="/path/to/project", sandbox=True)
result = orc.run_stream("analyze this project")
```

### Implementation files

| File | Role |
|---|---|
| `ccflow/sandbox.py` | `setup_sandbox()` / `teardown_sandbox()` — inject/restore `.claude/settings.json` |
| `ccflow/hooks/sandbox_guard.py` | PreToolUse hook script — path checking logic |
| `ccflow/orchestrator.py` | `_enter_sandbox()` / `_exit_sandbox()` — lifecycle in `run()` / `run_stream()` / `run_conversation()` |
| `ccflow/cli.py` | `--sandbox` flag + `.env` fallback (`CCFLOW_CWD`, `CCFLOW_SANDBOX`) |

### Settings.json merge strategy

The sandbox setup **preserves existing project-level settings**:
- Reads existing `{cwd}/.claude/settings.json` if present
- Only touches `hooks.PreToolUse` — all other keys and hook events are untouched
- Idempotent — skips injection if `sandbox_guard.py` hook already present
- On teardown, restores the exact original file content (byte-for-byte)
- If `.claude/` didn't exist before, removes it entirely on teardown

### Limitation

Bash command path parsing is inherently imperfect — variable expansion, subshells, symlinks can bypass string-based checks. For absolute security against malicious users, Docker/VM is the only option. But soft constraint + hooks is sufficient for normal use cases (e.g., Telegram bot).

## Docker Alternative (not needed yet)

Currently using Layer 1 + Layer 2 above. Docker is the escalation path if hooks are ever bypassed in practice.

### Why it was initially rejected

1. Existing CLI tools (az, gh) are already authenticated in the host environment
2. Re-authenticating inside containers for every session is too cumbersome

### Future Implementation

The auth problem is solvable by mounting host credentials read-only or injecting tokens via env vars.

**Dockerfile:**

```dockerfile
FROM node:22-slim
RUN npm i -g @anthropic-ai/claude-code
# Install CLI tools as needed
RUN apt-get update && apt-get install -y gh
```

**Run with mounted credentials:**

```bash
docker run --rm \
  -v ~/.config/gh:/root/.config/gh:ro \
  -v ~/.azure:/root/.azure:ro \
  -v /path/to/playground:/workspace \
  -e GH_TOKEN="$(gh auth token)" \
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  -w /workspace \
  my-ccflow-image \
  claude -p "do stuff" --dangerously-skip-permissions
```

**Key points:**
- `:ro` mounts credentials read-only — container cannot modify host auth state
- `gh` supports `GH_TOKEN` env var, so mounting `~/.config/gh` is optional
- `az` can use mounted `~/.azure` or service principal via env vars
- `/workspace` is the only writable volume — true filesystem sandbox
- Upgrade to this only if hook-based defense proves insufficient
