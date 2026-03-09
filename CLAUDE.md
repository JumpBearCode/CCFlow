# CCFlow — Claude Code CLI Wrapper

CCFlow is a Claude Code CLI wrapper -- a Python library + CLI that wraps `claude -p` (Claude Code's non-interactive mode) into a reusable interface. It parses `stream-json` output in real-time, prints Claude Code style formatted output, and logs every session.

## Install & Integration

### Other uv projects — add as Git dependency

In the other project's `pyproject.toml`:

```toml
[project]
dependencies = [
    "ccflow @ git+https://github.com/JumpBearCode/CCFlow.git",
]
```

Then `uv sync`. After that:

```python
from ccflow import ClaudeOrchestrator
```

And the `ccflow` CLI command is available via `uv run ccflow "prompt" --danger`.

### Other install methods

```bash
# uv add (from GitHub)
uv add git+https://github.com/JumpBearCode/CCFlow.git

# uv add (from local path)
uv add /path/to/CCFlow

# pip
pip install git+https://github.com/JumpBearCode/CCFlow.git
```

## Project Structure

```
CCFlow/
  main.py                       # convenience wrapper → ccflow.cli.main()
  pyproject.toml                 # zero dependencies, Python >=3.11, [project.scripts] ccflow
  ccflow/
    __init__.py                  # re-exports: ClaudeOrchestrator, ClaudeResult, format_event
    cli.py                       # CLI entry point — installed as `ccflow` command
    utils.py                     # shared utilities: format_tool_input, shorten (leaf, no ccflow imports)
    agent/
      __init__.py
      orchestrator.py            # core class — builds CLI args, runs subprocess, parses events
      printer.py                 # Claude Code style terminal printer (╭╰│⏺⎿ + timestamps)
      sandbox.py                 # sandbox setup/teardown
      hooks/
        __init__.py
        sandbox_guard.py         # PreToolUse hook script
    telegram/
      __init__.py
      telegram_bot.py            # TelegramBot class + ChatSession + bot_main CLI entry
      event_formatter.py         # format_event + Telegram-specific formatting
```

## Architecture

```
ccflow/utils.py                  ← stdlib only (leaf)

agent/printer.py                 ← utils
agent/sandbox.py                 ← stdlib only (leaf)
agent/orchestrator.py            ← agent/printer, agent/sandbox

telegram/event_formatter.py      ← utils (no agent dependency)
telegram/telegram_bot.py         ← agent/orchestrator, telegram/event_formatter

ccflow/__init__.py               ← agent/orchestrator, telegram/event_formatter
ccflow/cli.py                    ← ccflow/__init__, telegram/telegram_bot (lazy)
```

```
ClaudeOrchestrator
├── _call(prompt, log_file, print_events, print_banner) → ClaudeResult   # core: subprocess + parse
├── run(prompt)              → ClaudeResult        # batch: summary + print output
├── run_stream(prompt)       → ClaudeResult        # stream: real-time events + result banner
└── run_conversation(prompt) → list[ClaudeResult]  # chat: multi-round, one log, accumulated summary
```

All three public methods are fully decoupled — each independently composes `_call()` + shared helpers.
agent/ and telegram/ have zero cross-dependencies (telegram only imports agent/orchestrator).

## How to Run

All commands assume you're in the `CCFlow/` directory.

### CLI

After install, the `ccflow` command is available (or use `uv run ccflow` / `uv run main.py`):

```bash
# Stream mode (default, opus model)
ccflow "analyze this codebase" --danger

# Batch mode — no streaming, prints summary + result output
ccflow "summarize" --batch --danger

# Chat mode — interactive multi-round conversation
ccflow -i --danger
ccflow -i "start with this" --danger

# Plan mode — read-only exploration
ccflow "design a new feature" --plan

# Resume / continue
ccflow "keep going" -r <SESSION_ID> --danger
ccflow "continue" -c --danger

# Specify model, budget, tools
ccflow "fix the bug" -m sonnet --max-budget 1.0 --allowed-tools Bash Read Glob Grep

# Pipe prompt from stdin
cat SKILL.md | ccflow --danger

# Specify working directory
ccflow "analyze" --cwd /path/to/project --danger
```

### CLI Flags

| Flag | Description |
|---|---|
| `<prompt>` | Prompt text (positional, optional if piping stdin) |
| `-m, --model MODEL` | Model name (default: `opus`) |
| `--batch` | Batch mode — no streaming, prints summary + result output |
| `-i, --chat` | Interactive multi-round conversation |
| `--plan` | Plan mode — read-only, no file modifications |
| `--danger` | Skip all permission checks (`--dangerously-skip-permissions`) |
| `-r, --resume SESSION_ID` | Resume a previous session by ID |
| `-c, --continue` | Continue the most recent session |
| `--allowed-tools TOOL [...]` | Restrict to specific tools |
| `--max-budget USD` | Budget cap in USD |
| `--cwd PATH` | Working directory for the claude subprocess |
| `--log-dir DIR` | Log directory (default: `logs`) |
| `--output-dir DIR` | Save result output to this directory as `.md` files |

### Python API

```python
from ccflow import ClaudeOrchestrator

# Stream mode — real-time formatted output
orc = ClaudeOrchestrator(
    model="opus",                          # default
    dangerously_skip_permissions=True,
    allowed_tools=["Bash", "Read", "Glob", "Grep"],
)
result = orc.run_stream("analyze this codebase")

# Batch mode — prints summary + result output
result = orc.run("summarize this project")

# Chat mode — multi-round interactive conversation
results = orc.run_conversation("start with this")
# or let it prompt interactively:
results = orc.run_conversation()

# Resume a session
orc = ClaudeOrchestrator(
    resume_session="<SESSION_ID>",
    dangerously_skip_permissions=True,
)
result = orc.run_stream("continue the task")
```

### ClaudeOrchestrator constructor params

| Param | Type | Default | Description |
|---|---|---|---|
| `model` | `str` | `"opus"` | Model name |
| `allowed_tools` | `list[str]` | `None` | Tool whitelist |
| `disallowed_tools` | `list[str]` | `None` | Tool blacklist |
| `tools` | `str` | `None` | Tools flag passed to CLI |
| `permission_mode` | `str` | `None` | e.g. `"plan"` for read-only |
| `dangerously_skip_permissions` | `bool` | `False` | Skip all permission checks |
| `system_prompt` | `str` | `None` | Override system prompt |
| `append_system_prompt` | `str` | `None` | Append to system prompt |
| `mcp_config` | `list[str]` | `None` | MCP server config file paths |
| `strict_mcp_config` | `bool` | `False` | Strict MCP config mode |
| `verbose` | `bool` | `True` | Pass `--verbose` to CLI |
| `max_budget_usd` | `float` | `None` | Budget cap in USD |
| `effort` | `str` | `None` | Effort level |
| `session_id` | `str` | `None` | Explicit session ID |
| `continue_session` | `str` | `None` | Continue most recent session |
| `resume_session` | `str` | `None` | Resume a specific session by ID |
| `log_dir` | `str` | `None` | Auto-generate log path in this dir |
| `log_path` | `str` | `None` | Explicit log file path |
| `output_dir` | `str` | `None` | Save result output to this dir as `.md` files |
| `cwd` | `str` | `None` | Working directory for subprocess |

### ClaudeResult fields

```
result.success          # bool
result.output           # final text output (str | None)
result.session_id       # for resume/continue
result.duration_ms      # wall clock time
result.duration_api_ms  # API-side duration
result.num_turns        # conversation turns
result.cost_usd         # equivalent API cost (informational for Max Plan)
result.usage            # dict with input_tokens, output_tokens, cache stats
result.error            # error message if success=False
```

## Key Implementation Details

- Always uses `--output-format stream-json` internally
- Removes `CLAUDECODE*` env vars to support nested Claude invocations
- Prompt is sent via stdin (`proc.stdin.write`), not as a CLI argument
- Logs every raw JSON line to `logs/ccflow-YYYYMMDD-HHMMSS.log`
- `run()` (batch): prints summary line + `result.output`
- `run_stream()`: prints real-time events + result banner (no separate output print — already streamed)
- `run_conversation()`: one log file for all rounds, session banner on first round only, accumulated summary at the end with round count
