# Skill: Run CCFlow

CCFlow is a Claude Code CLI wrapper. Use it to launch `claude -p` subprocesses from Python or CLI. Handles stream-json parsing, formatted printing, and logging.

## When to Use

- Run Claude Code non-interactively from a script or automation
- Get real-time formatted output (stream mode) or just the final result (batch mode)
- Chain multiple Claude sessions programmatically
- Resume or continue a previous Claude session

## CLI Quick Reference

Run from the `CCFlow/` project directory with `uv`:

```bash
# Stream mode (default, opus model)
uv run main.py "<prompt>" --danger

# All flags
uv run main.py "<prompt>" \
  -m opus \                    # model: opus (default), sonnet, haiku
  --danger \                   # skip all permission checks
  --plan \                     # read-only plan mode
  --batch \                    # no streaming, shows token breakdown + session ID
  -r <SESSION_ID> \            # resume session by ID
  -c \                         # continue most recent session
  --allowed-tools Bash Read \  # restrict tools
  --max-budget 2.0 \           # USD budget cap
  --cwd /path/to/project \     # working directory
  --log-dir logs               # log directory (default: logs)

# Pipe prompt from file
cat instructions.md | uv run main.py --danger
```

On success, prints `result.output` to stdout. On failure, prints error to stderr and exits 1.

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
    # max_budget_usd=2.0,                  # budget cap
)

# Stream — real-time formatted output
result = orc.run_stream("your prompt here")

# Batch — no streaming, prints summary with token breakdown + session ID
result = orc.run("your prompt here")

# Use the result
if result.success:
    print(result.output)        # final text
    print(result.session_id)    # for resuming later
    print(result.usage)         # token breakdown dict
```

## Flags & Modes

| Flag / Param | Effect |
|---|---|
| `--danger` / `dangerously_skip_permissions=True` | Skip all permission prompts |
| `--plan` / `permission_mode="plan"` | Read-only exploration, no file modifications |
| `--batch` / `orc.run()` | No streaming; prints token breakdown + session ID summary |
| `-r ID` / `resume_session="ID"` | Resume a previous session by ID |
| `-c` / `continue_session="true"` | Continue the most recent session |

## Output

Stream mode prints Claude Code style formatted output with session banner, tool calls, and a result banner (duration, cost, turns, token breakdown, session ID).

Batch mode prints a one-liner summary: duration, cost, turns, token breakdown (in/out/cached/cache-write), and session ID.

Both modes: on success, `main.py` prints `result.output` to stdout.

Logs are saved to `logs/ccflow-YYYYMMDD-HHMMSS.log` (raw stream-json, one event per line).
