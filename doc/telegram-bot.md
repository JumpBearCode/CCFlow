# Telegram Bot 运行机制

## Commands

Bot 注册了 4 个 command handler（`telegram_bot.py` 第 280-284 行）：

| Command | Handler | 作用 |
|---------|---------|------|
| `/start` | `_handle_start` | 发送欢迎信息 |
| `/reset` | `_handle_reset` | 清除当前 chat 的 session，下次消息开新对话 |
| `/model <name>` | `_handle_model` | 切换模型（如 `sonnet`、`opus`） |
| `/status` | `_handle_status` | 显示当前 session 信息（session_id、model、idle 时间等） |

此外还有一个 catch-all：

```python
MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
```

所有非 command 的普通文本消息都走 `_handle_message`，转发给 Claude。

**调用方式**：用户在 Telegram 聊天框里输入 `/start`、`/reset` 等，Telegram 客户端会把它识别为 bot command 发给 bot。`python-telegram-bot` 库的 `CommandHandler` 负责匹配 `/xxx` 前缀并路由到对应 handler。

## Welcome Message

Welcome message 就是 `/start` handler 里硬编码的一段文字（第 168-175 行）。没有什么特殊设置——当用户第一次打开 bot 或手动发 `/start` 时，Telegram 客户端会自动发送 `/start` 消息，bot 收到后回复这段文本。

如果你想设置 Telegram 里那个 bot 描述页面的文字（点进 bot 之前看到的），那是通过 BotFather 配置的，不在代码里。

## 运行机制

### Polling 机制

`app.run_polling()`（第 293 行）底层是一个循环调用 Telegram Bot API 的 `getUpdates` 方法：

```
GET https://api.telegram.org/bot<TOKEN>/getUpdates?offset=<last_update_id+1>&timeout=30
```

这里用的是 **long polling**，不是每隔固定时间 ping 一次。流程是：

1. Bot 发一个 HTTP 请求到 Telegram server，带 `timeout=30`（默认值）
2. Telegram server **hold 住这个连接**，直到有新消息或超时
3. 有新消息 → 立即返回；超时 → 返回空结果
4. Bot 处理完后立即发下一个请求

所以消息到达几乎是**实时**的——Telegram server 一收到用户消息，就立刻通过已经挂着的 long poll 连接推回来。

### 延迟来源

Telegram polling 本身的延迟几乎可以忽略（毫秒级）。真正的延迟来自：

- **Claude 处理时间**：`orc.run()` 跑完可能要几秒到几十秒。在此期间 `session.busy = True`，新消息会被拒绝（"Still processing"）
- 但 polling 本身不会被阻塞——`_handle_message` 是 async 的，`asyncio.to_thread(orc.run, text)` 把 Claude 调用放到线程池里，event loop 还是自由的，能继续收新的 update

## 完整消息流转

```
用户在 Telegram 发 "hello"
        │
        ▼
Telegram Server 收到消息
        │
        ▼ (通过已挂着的 long poll 连接立即返回)
        │
python-telegram-bot 收到 Update
        │
        ▼ 匹配到 MessageHandler (TEXT & ~COMMAND)
        │
_handle_message()
        │
        ├─ 检查 authorized? → 否 → "Unauthorized."
        ├─ 检查 session.busy? → 是 → "Still processing..."
        │
        ▼ (通过)
  session.busy = True
  send_chat_action("typing")    ← Telegram 里显示 "Bot is typing..."
        │
        ▼
  new ClaudeOrchestrator(resume_session=session.session_id)
        │
        ▼
  asyncio.to_thread(orc.run, "hello")   ← 在线程池里跑 claude -p
        │                                    主 event loop 不阻塞
        ▼ (等 Claude 返回)
        │
  拿到 result
        ├─ 保存 session_id（下次 resume 用）
        ├─ result.output → 转 HTML → split chunks → reply_text()
        │
        ▼
  session.busy = False
```

## 多轮对话机制

虽然每条消息都 new 了一个新的 `ClaudeOrchestrator` 实例，但通过 `resume_session` 参数串联对话：

1. **第一条消息**：`session.session_id` 为 `None`，Claude 开一个全新 session
2. **Claude 返回后**：把 `result.session_id` 存到 `session.session_id`
3. **后续消息**：`ClaudeOrchestrator(resume_session="之前的session_id")`，底层 `claude -p -r <session_id>` 恢复对话历史

效果等价于 `run_conversation` 的多轮对话能力，但用 `run()` + `resume_session` 手动实现，适合 Telegram 这种事件驱动的异步场景。

### Session 生命周期

- 每个 `chat_id` 维护一个 `ChatSession`，记住 `session_id`
- 空闲超过 `session_timeout`（默认 180 秒）后被 `_session_reaper` 后台任务清理
- 用户发 `/reset` 也会清除 session，下次消息就是全新对话
