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

### As a global CLI tool (recommended)

```bash
git clone https://github.com/JumpBearCode/CCFlow.git
cd CCFlow
cp .env.example .env   # edit .env with your tokens
uv tool install --editable .
```

After installation, `ccflow` is available globally from any directory. The `--editable` flag is required — CCFlow loads `.env` relative to its source tree, so the installed command must point back to the cloned repo.

To upgrade after pulling new changes, no reinstall needed — editable mode picks up changes automatically.

### As a project dependency (library only)

Use this when you want to import `ClaudeOrchestrator` in your own project. The Telegram bot and CLI features require the global install above.

```bash
# Using uv
uv add git+https://github.com/JumpBearCode/CCFlow.git

# Using pip
pip install git+https://github.com/JumpBearCode/CCFlow.git
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

## Telegram Bot

CCFlow includes a Telegram bot that bridges messages to Claude. Each chat maintains its own session for multi-turn conversations.

### Setup

The `.env` file should already be configured during installation (see [Install](#install)). Then run:

```bash
ccflow bot --danger
```

If you need table image rendering, also install Chromium (one-time):

```bash
playwright install chromium

# Linux servers also need system dependencies:
playwright install-deps chromium
```

### Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | — | Bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_ALLOWED_USERS` | No | — | Comma-separated Telegram user IDs. If unset, anyone can use the bot |
| `OUTPUT_FORMAT` | No | `streaming` | `streaming` (real-time tool calls) or `batch` (wait for full response) |
| `CLAUDE_SUBPROCESS_TIMEOUT` | No | `300` | Max seconds per Claude invocation |
| `ENABLE_TABLE_IMAGE` | No | `false` | Render markdown tables as images (requires Playwright setup) |

### Bot Commands

- `/start` — Welcome message
- `/reset` — Clear session, start fresh
- `/model <name>` — Switch model (e.g. `sonnet`, `opus`)
- `/status` — Show current session info

### CLI Flags

```bash
uv run ccflow bot --danger                    # required: skip permission checks
uv run ccflow bot --danger -m sonnet          # specify model
uv run ccflow bot --danger --cwd /path/to/dir # working directory for Claude
uv run ccflow bot --danger --max-budget 1.0   # budget cap per invocation
uv run ccflow bot --danger --subprocess-timeout 600
```

### Table Image Rendering (Optional)

When `ENABLE_TABLE_IMAGE=true`, markdown tables in Claude's output are rendered as PNG images using headless Chromium, then sent as photos. This makes tables readable on mobile.

**Setup:**

```bash
# 1. Install with table-image extra
uv add "ccflow[table-image] @ git+https://github.com/JumpBearCode/CCFlow.git"
# or for local dev:
uv sync --extra table-image

# 2. Install Chromium (one-time, ~300MB, stored in user cache)
uv run playwright install chromium

# Linux servers also need system dependencies:
uv run playwright install-deps chromium
```

**Docker:**

```dockerfile
RUN uv sync --extra table-image \
    && uv run playwright install-deps chromium \
    && uv run playwright install chromium
```

Then set `ENABLE_TABLE_IMAGE=true` in your `.env`.

If Playwright is not installed or rendering fails, it falls back to sending the raw table as `<pre>` text.
