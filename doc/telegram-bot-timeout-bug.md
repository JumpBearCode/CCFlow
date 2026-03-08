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

## 修复方向

1. **超时时杀掉子进程**：需要在 orchestrator 层暴露 `proc` 引用或 cancel 机制，
   超时时 `proc.kill()` + `proc.wait()`
2. **finally 块里更新 `last_active`**：无论成功还是超时，都刷新 `last_active`，
   防止 reaper 误杀
3. **超时后保留 session_id**：即使超时，也把已有的 `session_id` 留在 session 里，
   用户继续对话时还能 resume
4. **`load_dotenv` 用绝对路径**：`load_dotenv(Path(__file__).resolve().parent.parent / ".env")`
   确保无论从哪个目录启动都能找到 `.env`
5. **session_timeout 应 >= subprocess_timeout**：或者至少让 reaper 不清理
   最近 subprocess_timeout 秒内活跃过的 session
