# CCFlow

Claude Code CLI wrapper — a Python library + CLI that wraps `claude -p` into a reusable interface.

- Parses `stream-json` output in real-time
- Prints Claude Code style formatted output (banners, tool calls, timestamps)
- Token breakdown per run (input/output/cached/cache-write)
- Session resume/continue support
- Zero external dependencies, Python >=3.11

## Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and on PATH (`claude` command available)
- Python >=3.11
- [uv](https://docs.astral.sh/uv/) (recommended)

## Install

### From GitHub (recommended for other projects)

```bash
# Using uv
uv add git+https://github.com/JumpBearCode/CCFlow.git

# Using pip
pip install git+https://github.com/JumpBearCode/CCFlow.git
```

### From local path

```bash
# Using uv
uv add /path/to/CCFlow

# Using pip
pip install /path/to/CCFlow
```

### For development (editable)

```bash
cd CCFlow
uv pip install -e .
```

## Using CCFlow in Other uv Projects

### Step 1: Add dependency

In your project's `pyproject.toml`:

```toml
[project]
dependencies = [
    "ccflow @ git+https://github.com/JumpBearCode/CCFlow.git",
]
```

Or pin to a specific commit:

```toml
dependencies = [
    "ccflow @ git+https://github.com/JumpBearCode/CCFlow.git@03b418d",
]
```

Then sync:

```bash
uv sync
```

### Step 2: Use in your code

```python
from ccflow import ClaudeOrchestrator

orc = ClaudeOrchestrator(
    dangerously_skip_permissions=True,
    allowed_tools=["Bash", "Read", "Glob", "Grep"],
)
result = orc.run_stream("analyze this codebase")

if result.success:
    print(result.output)
    print(f"Session: {result.session_id}")  # save for resuming
```

### Step 3: Use the CLI command

After installing, the `ccflow` command is available in your project's venv:

```bash
uv run ccflow "your prompt" --danger
```

## CLI Usage

```bash
# Stream mode (default, opus model)
ccflow "analyze this codebase" --danger

# Batch mode — no streaming, prints summary + result
ccflow "summarize" --batch --danger

# Plan mode — read-only exploration
ccflow "design a new feature" --plan

# Resume / continue
ccflow "keep going" -r <SESSION_ID> --danger
ccflow "continue" -c --danger

# Specify model, budget, tools, working directory
ccflow "fix the bug" -m sonnet --max-budget 1.0 --allowed-tools Bash Read Glob Grep --cwd /path/to/project

# Pipe prompt from stdin
cat instructions.md | ccflow --danger
```

### Flags

| Flag | Description |
|---|---|
| `<prompt>` | Prompt text (positional, or pipe via stdin) |
| `-m, --model` | Model name (default: `opus`) |
| `--danger` | Skip all permission checks |
| `--batch` | No streaming, prints token summary + session ID |
| `--plan` | Read-only plan mode |
| `-r, --resume ID` | Resume a previous session |
| `-c, --continue` | Continue most recent session |
| `--allowed-tools` | Tool whitelist |
| `--max-budget` | Budget cap in USD |
| `--cwd` | Working directory |
| `--log-dir` | Log directory (default: `logs`) |

## Python API

```python
from ccflow import ClaudeOrchestrator, ClaudeResult

# Stream — real-time formatted output
orc = ClaudeOrchestrator(
    model="opus",
    dangerously_skip_permissions=True,
    allowed_tools=["Bash", "Read", "Glob", "Grep"],
    log_dir="logs",
)
result = orc.run_stream("your prompt")

# Batch — quiet, returns result
result = orc.run("your prompt")

# Resume a previous session
orc = ClaudeOrchestrator(
    resume_session="<SESSION_ID>",
    dangerously_skip_permissions=True,
)
result = orc.run_stream("continue")

# With MCP servers
orc = ClaudeOrchestrator(
    mcp_config=["./mcp-servers.json"],
    strict_mcp_config=True,
    allowed_tools=["mcp__my-server__tool1", "Read"],
)
result = orc.run_stream("execute workflow")
```

### ClaudeResult

```python
result.success          # bool
result.output           # final text (str | None)
result.session_id       # for resume/continue
result.duration_ms      # wall clock ms
result.num_turns        # conversation turns
result.cost_usd         # equivalent API cost (informational for Max Plan users)
result.usage            # {"input_tokens": ..., "output_tokens": ..., ...}
result.error            # error message if success=False
```

## Output Examples

### Stream mode

```
[09:30:15] ╭─ CCFlow Session ─────────────────────────────────
[09:30:15] │  Model: claude-opus-4-6
[09:30:15] │  Started: 2026-03-03 09:30:15
[09:30:15] ╰──────────────────────────────────────────────────

[09:30:16] I'll analyze the codebase structure.
[09:30:16] ⏺ Bash  command='find . -type f | head -20'
[09:30:17]   ⎿  Done

[09:30:25] ╭─ Session Complete ────────────────────────────────
[09:30:25] │  Duration: 10.2s  │  Turns: 3
[09:30:25] │  Tokens: 160k in + 966 out + 20k cache-write
[09:30:25] │  Session: abc-123-def
[09:30:25] ╰──────────────────────────────────────────────────
```

### Batch mode

```
[09:30:15] CCFlow Running (opus)...
[09:30:25] CCFlow Done (10.2s)  │  3 turns  │  160k in + 966 out + 20k cache-write
[09:30:25] CCFlow Session: abc-123-def
```
