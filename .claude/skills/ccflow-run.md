# Skill: Run CCFlow

CCFlow is a Claude Code CLI wrapper. Use it to launch `claude -p` subprocesses from Python or CLI. Handles stream-json parsing, formatted printing, and logging.

## When to Use

- Run Claude Code non-interactively from a script or automation
- Get real-time formatted output (stream mode) or just the final result (batch mode)
- Run multi-round interactive conversations (chat mode)
- Chain multiple Claude sessions programmatically
- Resume or continue a previous Claude session

## CLI Quick Reference

Run from the `CCFlow/` project directory with `uv`:

```bash
# Stream mode (default, opus model)
uv run ccflow "<prompt>" --danger

# Batch mode — prints summary + result output
uv run ccflow "<prompt>" --batch --danger

# Chat mode — multi-round interactive conversation
uv run ccflow -i --danger
uv run ccflow -i "<initial prompt>" --danger

# All flags
uv run ccflow "<prompt>" \
  -m opus \                    # model: opus (default), sonnet, haiku
  --danger \                   # skip all permission checks
  --plan \                     # read-only plan mode
  --batch \                    # no streaming, prints summary + result output
  -i \                         # interactive multi-round conversation
  -r <SESSION_ID> \            # resume session by ID
  -c \                         # continue most recent session
  --allowed-tools Bash Read \  # restrict tools
  --max-budget 2.0 \           # USD budget cap
  --cwd /path/to/project \     # working directory
  --log-dir logs \             # log directory (default: logs)
  --output-dir outputs         # save result output as .md files

# Pipe prompt from file
cat instructions.md | uv run ccflow --danger
```

## Python Quick Reference

```python
from ccflow import ClaudeOrchestrator

orc = ClaudeOrchestrator(
    model="opus",                          # default
    dangerously_skip_permissions=True,      # --danger
    # permission_mode="plan",              # read-only mode
    # allowed_tools=["Bash", "Read"],      # tool whitelist
    # disallowed_tools=["Write"],          # tool blacklist
    # system_prompt="...",                 # override system prompt
    # append_system_prompt="...",          # append to system prompt
    # mcp_config=["./mcp.json"],           # MCP servers
    # resume_session="<SESSION_ID>",       # resume by ID
    # continue_session="true",            # continue most recent
    # cwd="/path/to/project",             # working directory
    # log_dir="logs",                      # auto-generate log path
    # output_dir="outputs",               # save result output as .md files
    # max_budget_usd=2.0,                  # budget cap
)

# Stream — real-time formatted output
result = orc.run_stream("your prompt here")

# Batch — prints summary + result output
result = orc.run("your prompt here")

# Chat — multi-round interactive conversation
results = orc.run_conversation("start with this")
results = orc.run_conversation()  # prompts interactively

# Use the result
if result.success:
    print(result.output)        # final text
    print(result.session_id)    # for resuming later
    print(result.usage)         # token breakdown dict
```

## Modes

| Mode | CLI | Python | Behavior |
|---|---|---|---|
| Stream | `ccflow "prompt" --danger` | `orc.run_stream(prompt)` | Real-time events + result banner |
| Batch | `ccflow "prompt" --batch --danger` | `orc.run(prompt)` | Summary line + result output printed |
| Chat | `ccflow -i --danger` | `orc.run_conversation()` | Multi-round, one log, accumulated summary |
| Plan | `ccflow "prompt" --plan` | `permission_mode="plan"` | Read-only, no file modifications |

## Flags & Params

| Flag / Param | Effect |
|---|---|
| `--danger` / `dangerously_skip_permissions=True` | Skip all permission prompts |
| `--plan` / `permission_mode="plan"` | Read-only exploration, no file modifications |
| `--batch` / `orc.run()` | No streaming; prints summary + result output |
| `-i` / `orc.run_conversation()` | Multi-round interactive conversation |
| `-r ID` / `resume_session="ID"` | Resume a previous session by ID |
| `-c` / `continue_session="true"` | Continue the most recent session |
| `--output-dir` / `output_dir="dir"` | Save result output to disk as `.md` files |

## Output

- **Stream mode**: Claude Code style formatted output with session banner, tool calls, and result banner (duration, cost, turns, token breakdown, session ID).
- **Batch mode**: One-liner summary (duration, cost, turns, tokens, session ID) followed by `result.output`.
- **Chat mode**: Session banner on first round, streaming events per round (no repeated banners), one "Conversation Complete" summary at the end with accumulated totals and round count.

Logs are saved to `logs/ccflow-YYYYMMDD-HHMMSS.log` (raw stream-json, one event per line). Chat mode writes all rounds to a single log file.
