# CCFlow

Claude Code CLI wrapper — a Python library + CLI that wraps `claude -p` into a reusable interface.

- Parses `stream-json` output in real-time
- Prints Claude Code style formatted output (banners, tool calls, timestamps)
- Token breakdown per run (input/output/cached/cache-write)
- Multi-round interactive conversation mode
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
    print(result.session_id)  # save for resuming
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

# Batch mode — prints summary + result output
ccflow "summarize" --batch --danger

# Chat mode — interactive multi-round conversation
ccflow -i --danger
ccflow -i "start with this" --danger

# Plan mode — read-only exploration
ccflow "design a new feature" --plan

# Resume / continue
ccflow "keep going" -r <SESSION_ID> --danger
ccflow "continue" -c --danger

# Specify model, budget, tools, working directory
ccflow "fix the bug" -m sonnet --max-budget 1.0 --allowed-tools Bash Read Glob Grep --cwd /path/to/project

# Pipe prompt from stdin
cat instructions.md | ccflow --danger

# Save result output to disk
ccflow "summarize" --danger --output-dir outputs
```

### Flags

| Flag | Description |
|---|---|
| `<prompt>` | Prompt text (positional, or pipe via stdin) |
| `-m, --model` | Model name (default: `opus`) |
| `--danger` | Skip all permission checks |
| `--batch` | No streaming, prints summary + result output |
| `-i, --chat` | Interactive multi-round conversation |
| `--plan` | Read-only plan mode |
| `-r, --resume ID` | Resume a previous session |
| `-c, --continue` | Continue most recent session |
| `--allowed-tools` | Tool whitelist |
| `--max-budget` | Budget cap in USD |
| `--cwd` | Working directory |
| `--log-dir` | Log directory (default: `logs`) |
| `--output-dir` | Save result output as `.md` files |

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

# Batch — prints summary + result output
result = orc.run("your prompt")

# Chat — multi-round interactive conversation
results = orc.run_conversation("start with this")
# or let it prompt interactively:
results = orc.run_conversation()

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

## Architecture

```
ClaudeOrchestrator
├── _call(prompt, ...)           → ClaudeResult        Core: subprocess + parse events
├── run(prompt)                  → ClaudeResult        Batch: summary + print output
├── run_stream(prompt)           → ClaudeResult        Stream: real-time events + result banner
└── run_conversation(prompt)     → list[ClaudeResult]  Chat: multi-round, accumulated summary
```

All three public methods are fully decoupled — each independently composes `_call()` + shared helpers.

## Output Examples

### Stream mode

```
[09:30:15] ╭─ CCFlow Session ─────────────────────────────────────
[09:30:15] │  Model: claude-opus-4-6
[09:30:15] │  Started: 2026-03-03 09:30:15
[09:30:15] ╰──────────────────────────────────────────────────────

[09:30:16] I'll analyze the codebase structure.
[09:30:16] ⏺ Bash  command='find . -type f | head -20'
[09:30:17]   ⎿  Done

[09:30:25] ╭─ Session Complete ────────────────────────────────────
[09:30:25] │  Duration: 10.2s  │  Turns: 3
[09:30:25] │  Tokens: 160k in + 966 out + 20k cache-write
[09:30:25] │  Session: abc-123-def
[09:30:25] ╰──────────────────────────────────────────────────────
```

### Batch mode

```
[09:30:15] CCFlow Running (opus)...
[09:30:25] CCFlow Done (10.2s)  │  3 turns  │  160k in + 966 out + 20k cache-write
[09:30:25] CCFlow Session: abc-123-def

<result output printed here>
```

### Chat mode

```
[09:30:15] ╭─ CCFlow Session ─────────────────────────────────────
[09:30:15] │  Model: claude-opus-4-6
[09:30:15] │  Started: 2026-03-03 09:30:15
[09:30:15] ╰──────────────────────────────────────────────────────

[09:30:16] I'll analyze the codebase structure.
[09:30:16] ⏺ Bash  command='find . -type f | head -20'
[09:30:17]   ⎿  Done

You: what about the tests?

[09:30:45] Let me look at the test files.
[09:30:45] ⏺ Glob  pattern="**/*test*"
[09:30:46]   ⎿  Done

You: exit

[09:31:00] ╭─ Conversation Complete ───────────────────────────────
[09:31:00] │  Duration: 45.2s  │  Cost: $0.1523  │  Turns: 6
[09:31:00] │  Tokens: 320k in + 2.1k out + 40k cached
[09:31:00] │  Rounds: 2
[09:31:00] │  Session: abc-123-def
[09:31:00] ╰──────────────────────────────────────────────────────
```
