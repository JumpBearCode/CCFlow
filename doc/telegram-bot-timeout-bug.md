# Telegram Bot Timeout Bug 分析

## 问题现象

1. Claude 跑到一半说 **timeout**
2. 用户在 Telegram 继续发消息，Claude **完全没有记忆**（开了新 session）
3. 终端最后又打印 "项目已经跑完了" —— 底层子进程一直在跑

## 两层超时机制

```
┌─────────────────────────────────────────────────┐
│  Claude Code CLI (claude -p)                    │
│  → 没有时间限制，会一直跑到任务完成              │
│  → CLAUDE_SUBPROCESS_TIMEOUT 对它完全无效        │
│    它不认这个环境变量                            │
└───────────────────┬─────────────────────────────┘
                    │ subprocess.Popen
┌───────────────────▼─────────────────────────────┐
│  asyncio.wait_for(timeout=subprocess_timeout)   │
│  → 消费 CLAUDE_SUBPROCESS_TIMEOUT 环境变量       │
│  → 默认 300s，ENV 可设为 600s                    │
│  → 超时后只取消 asyncio future，不杀子进程！      │
└───────────────────┬─────────────────────────────┘
                    │
┌───────────────────▼─────────────────────────────┐
│  session_reaper (每 60s 扫一次)                  │
│  → session_timeout 默认 180s                     │
│  → 只看 last_active + busy 状态                  │
│  → 不关心子进程是否还在跑                        │
└─────────────────────────────────────────────────┘
```

关键：`CLAUDE_SUBPROCESS_TIMEOUT` 不是 Claude Code CLI 的设置，是 telegram bot 代码
自己定义的变量（`telegram_bot.py` 的 `bot_main()` 读取）。

## 出事时间线

以一次实际 timeout 还原（2026-03-08）：

| 时间 | 事件 |
|------|------|
| **00:24:53** | 用户发消息，`last_active = 此时`，Claude 开始跑任务 |
| 00:24~00:29 | Claude 在工作（读文件、写代码、跑命令），流式事件持续发送 |
| **00:29:53** | 距开始 **300 秒** —— `asyncio.wait_for` 超时，流式事件停止 |
| | `TimeoutError` 被捕获，发送 "Timed out" 消息 |
| | `session.busy = False`（finally 块） |
| | **但 `last_active` 没有更新**（line 414 在 try 块里被跳过） |
| | **底层 claude 子进程继续在跑**（没被 kill） |
| **00:30:24** | Reaper 扫描：`now - last_active = 331s > 180s` → **删除 session** |
| | `session_id` 丢失 |
| **00:30:49** | 用户发 "继续"，`_get_or_create_session` 创建全新 session |
| | `session_id = None`，Claude 开新对话 → **完全没有记忆** |
| 00:31:11 | 新 session 完成（21.4s），Claude 说 "我没有之前的上下文" |
| **00:44:07** | 原始 claude 子进程终于跑完（322.4s），在终端打印完整结果 |
| | 但 Telegram 用户永远收不到这个结果 |

## 三个结构性 Bug

### Bug 1: 超时不杀子进程

`telegram_bot.py:405-408`:
```python
result = await asyncio.wait_for(
    asyncio.to_thread(orc.run, text, on_event=on_event),
    timeout=self.subprocess_timeout,
)
```

`asyncio.wait_for` 超时只取消了 asyncio future。`asyncio.to_thread` 里的线程
和底层的 `claude` 子进程 (`subprocess.Popen`) 都继续在跑。
结果：烧钱、占资源，但结果丢失。

### Bug 2: 超时后 `last_active` 不更新

`telegram_bot.py:414`:
```python
session.last_active = time.monotonic()  # 在 try 块里，超时异常跳过了这行
```

这行只在 `asyncio.wait_for` 成功返回后才执行。超时时 `TimeoutError` 直接跳到
except 块，`last_active` 保持为任务开始时的值（已经过了 300+ 秒）。

### Bug 3: Reaper 清掉 session 导致丢失记忆

超时后：
- `last_active` 停在 300+ 秒前
- `session.busy = False`
- Reaper 下次扫描时发现 `now - last_active > 180s` → 删除 session
- `session_id` 永久丢失
- 用户再发消息 → 全新 session → 无记忆

### 附加问题: ENV 600s 可能没生效

日志显示事件恰好在 300 秒（默认值）停止，而不是 600 秒。
`load_dotenv()` 依赖运行时工作目录来找 `.env`。如果启动 bot 时
不在 CCFlow 目录下，`.env` 不会被加载，fallback 到默认 300s。

## 已完成的修复

### Fix 1: `load_dotenv` 用 `__file__` 定位 `.env`

`telegram_bot.py` 顶部：
```python
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
```
无论从哪个目录启动 bot，都能找到项目根目录的 `.env`，确保
`CLAUDE_SUBPROCESS_TIMEOUT` 等 ENV 变量被正确加载。

### Fix 2: 超时时杀掉子进程（无 orphan process）

**orchestrator.py** — `_call()` 里把 `proc` 存到 `self._proc`：
```python
proc = subprocess.Popen(...)
self._proc = proc
```

**telegram_bot.py** — `except asyncio.TimeoutError` 里杀掉子进程：
```python
proc = getattr(orc, "_proc", None)
if proc is not None:
    proc.kill()
    proc.wait()
```

子进程被 kill → stdout EOF → `_call()` 的 for 循环退出 →
线程自然结束 → 无 orphan。

### Fix 3: `last_active` 移到 finally 块

```python
finally:
    stop_typing.set()
    await typing_task
    session.last_active = time.monotonic()  # 无论成功/超时/异常都更新
    session.busy = False
```

### Fix 4: 删除 session_reaper，session 只由 `/reset` 清除

- 删除 `_session_reaper` 方法和 `post_init` 里的 `create_task`
- 删除 `session_timeout` 参数（constructor、CLI arg、bot_main）
- Session 持久存在，直到用户发 `/reset` 或 bot 进程重启
- 不再有 reaper 误杀 session 导致丢失记忆的问题

### Fix 5: Resume 失败自动容错

```python
elif not result.success and session.session_id:
    logger.warning("Resume failed for session %s, clearing", session.session_id)
    session.session_id = None
```

如果 resume 一个过期很久的 session_id，Claude CLI 可能报错。
此时自动清掉旧 session_id，用户下次发消息自动开新对话，
不需要手动 `/reset`。
