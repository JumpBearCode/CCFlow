"""Telegram bot that forwards messages to ClaudeOrchestrator and sends responses back."""

from dotenv import load_dotenv

load_dotenv()

import argparse
import asyncio
import html
import logging
import os
import re
import time
from dataclasses import dataclass, field

from ccflow import ClaudeOrchestrator
from ccflow.event_formatter import format_event
from ccflow.printer import format_tool_input, shorten

logger = logging.getLogger(__name__)

# Minimum interval between Telegram API calls (rate limit safety)
_TG_MIN_INTERVAL = 1.0

_TOOL_EMOJIS: dict[str, str] = {
    "Bash": "\u2328\ufe0f",        # ⌨️
    "Read": "\U0001F4C4",          # 📄
    "Write": "\u270f\ufe0f",       # ✏️
    "Edit": "\u270f\ufe0f",        # ✏️
    "Glob": "\U0001F50D",          # 🔍
    "Grep": "\U0001F50D",          # 🔍
    "Agent": "\U0001F916",         # 🤖
    "WebSearch": "\U0001F310",     # 🌐
    "WebFetch": "\U0001F310",      # 🌐
}
_DEFAULT_TOOL_EMOJI = "\U0001F6E0\ufe0f"   # 🛠️


def _tool_emoji(name: str) -> str:
    """Return an emoji for the given tool name."""
    return _TOOL_EMOJIS.get(name, _DEFAULT_TOOL_EMOJI)


def _event_to_telegram(event: dict) -> list[tuple[str, str]]:
    """Classify an event into a list of (category, formatted_text) pairs for Telegram.

    Categories: "tool", "tool_done", "tool_error", "text", "thinking", "result", "status".
    Returns an empty list if the event should be suppressed entirely.
    """
    etype = event.get("type", "")

    if etype == "assistant":
        items: list[tuple[str, str]] = []
        message = event.get("message", {})
        for block in message.get("content", []):
            block_type = block.get("type", "")

            if block_type == "tool_use":
                name = block.get("name", "?")
                emoji = _tool_emoji(name)
                tool_input = block.get("input", {})
                params = format_tool_input(tool_input)
                if params:
                    items.append(("tool", f"{emoji} Tool: {name}  {params}"))
                else:
                    items.append(("tool", f"{emoji} Tool: {name}"))

            elif block_type == "thinking":
                thinking_text = block.get("thinking", "").strip()
                if thinking_text:
                    items.append(("thinking", f"\U0001F4A1 {shorten(thinking_text, 300)}"))
                else:
                    items.append(("thinking", "\U0001F4A1 Thinking..."))

            elif block_type == "text":
                text = block.get("text", "").strip()
                if text:
                    items.append(("text", text))

        return items

    if etype == "user":
        message = event.get("message", {})
        for block in message.get("content", []):
            if block.get("type") == "tool_result":
                if block.get("is_error", False):
                    content = block.get("content", "")
                    return [("tool_error", f"\u274c Error: {shorten(str(content), 150)}")]
                return [("tool_done", "\u2705 Done")]
        return []

    if etype == "system" and event.get("subtype") == "init":
        return [("status", format_event(event) or "Session started")]

    if etype == "result":
        duration_s = event.get("duration_ms", 0) / 1000
        cost = event.get("total_cost_usd")
        num_turns = event.get("num_turns")
        usage = event.get("usage", {}) or {}
        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)

        parts = [f"{duration_s:.1f}s"]
        if cost is not None:
            parts.append(f"${cost:.4f}")
        if num_turns is not None:
            parts.append(f"{num_turns} turns")
        if inp or out:
            parts.append(f"{inp}in/{out}out")
        return [("result", f"\u2705 Completed ({' | '.join(parts)})")]

    if etype == "rate_limit_event":
        formatted = format_event(event)
        if formatted:
            return [("status", f"\u26a0\ufe0f {formatted}")]
        return []

    return []


@dataclass
class ChatSession:
    """Tracks a Telegram chat's Claude session state."""

    session_id: str | None = None
    model: str = "opus"
    last_active: float = field(default_factory=time.monotonic)
    busy: bool = False


def _split_message(text: str, max_len: int = 4096) -> list[str]:
    """Split text into chunks that fit Telegram's message size limit."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # Try to split at last newline before limit
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def _markdown_to_telegram_html(text: str) -> str:
    """Convert standard Markdown to Telegram-compatible HTML.

    Handles fenced code blocks, inline code, bold, italic,
    strikethrough, headers, and links.
    """
    # 1. Stash fenced code blocks so inner content is not processed.
    code_blocks: list[str] = []

    def _stash_code_block(m: re.Match) -> str:
        lang = m.group(1) or ""
        code = html.escape(m.group(2).strip("\n"))
        if lang:
            code_blocks.append(
                f'<pre><code class="language-{lang}">{code}</code></pre>'
            )
        else:
            code_blocks.append(f"<pre>{code}</pre>")
        return f"\x00CB{len(code_blocks) - 1}\x00"

    text = re.sub(r"```(\w*)\n?(.*?)```", _stash_code_block, text, flags=re.DOTALL)

    # 2. Stash inline code spans.
    inline_codes: list[str] = []

    def _stash_inline(m: re.Match) -> str:
        inline_codes.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00IC{len(inline_codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", _stash_inline, text)

    # 3. Escape HTML entities in the remaining (non-code) text.
    text = html.escape(text)

    # 4. Bold: **text**
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    # 5. Italic: *text* (not adjacent to word characters)
    text = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"<i>\1</i>", text)
    # 6. Strikethrough: ~~text~~
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    # 7. Headers: # … → bold line
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    # 8. Links: [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)

    # 9. Restore stashed blocks.
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CB{i}\x00", block)
    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00IC{i}\x00", code)

    return text


class TelegramBot:
    """Telegram bot that bridges messages to ClaudeOrchestrator.

    Each Telegram chat maintains its own Claude session for multi-turn
    conversations. Sessions are automatically reaped after idle timeout.
    """

    def __init__(
        self,
        token: str,
        *,
        allowed_users: set[int] | None = None,
        model: str = "opus",
        danger: bool = False,
        allowed_tools: list[str] | None = None,
        max_budget_usd: float | None = None,
        cwd: str | None = None,
        log_dir: str | None = None,
        session_timeout: int = 180,
        subprocess_timeout: int = 300,
        output_format: str = "streaming",
    ) -> None:
        self.token = token
        self.allowed_users = allowed_users
        self.model = model
        self.danger = danger
        self.allowed_tools = allowed_tools
        self.max_budget_usd = max_budget_usd
        self.cwd = cwd
        self.log_dir = log_dir
        self.session_timeout = session_timeout
        self.subprocess_timeout = subprocess_timeout
        self.output_format = output_format
        self.sessions: dict[int, ChatSession] = {}

    def _is_authorized(self, user_id: int) -> bool:
        return self.allowed_users is None or user_id in self.allowed_users

    def _get_or_create_session(self, chat_id: int) -> ChatSession:
        if chat_id not in self.sessions:
            self.sessions[chat_id] = ChatSession(model=self.model)
        session = self.sessions[chat_id]
        session.last_active = time.monotonic()
        return session

    def _make_orchestrator(self, session: ChatSession) -> ClaudeOrchestrator:
        return ClaudeOrchestrator(
            model=session.model,
            dangerously_skip_permissions=self.danger,
            allowed_tools=self.allowed_tools,
            max_budget_usd=self.max_budget_usd,
            resume_session=session.session_id,
            log_dir=self.log_dir,
            cwd=self.cwd,
        )

    async def _session_reaper(self) -> None:
        """Background task that removes idle sessions."""
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            expired = [
                cid
                for cid, s in self.sessions.items()
                if not s.busy and (now - s.last_active) > self.session_timeout
            ]
            for cid in expired:
                logger.info("Reaping idle session for chat %d", cid)
                del self.sessions[cid]

    async def _handle_start(self, update, context) -> None:
        await update.message.reply_text(
            "Welcome to CCFlow Bot!\n\n"
            "Send any message and I'll forward it to Claude.\n\n"
            "Commands:\n"
            "/reset — End current conversation\n"
            "/model <name> — Switch model (e.g. sonnet, opus)\n"
            "/status — Show session info"
        )

    async def _handle_reset(self, update, context) -> None:
        chat_id = update.effective_chat.id
        if chat_id in self.sessions:
            del self.sessions[chat_id]
            await update.message.reply_text("Session cleared. Next message starts a fresh conversation.")
        else:
            await update.message.reply_text("No active session.")

    async def _handle_model(self, update, context) -> None:
        chat_id = update.effective_chat.id
        if not context.args:
            session = self.sessions.get(chat_id)
            current = session.model if session else self.model
            await update.message.reply_text(f"Current model: {current}\nUsage: /model <name>")
            return
        new_model = context.args[0]
        session = self._get_or_create_session(chat_id)
        session.model = new_model
        await update.message.reply_text(f"Model switched to: {new_model}")

    async def _handle_status(self, update, context) -> None:
        chat_id = update.effective_chat.id
        session = self.sessions.get(chat_id)
        if not session:
            await update.message.reply_text(
                f"No active session.\nDefault model: {self.model}\n"
                f"Active sessions: {len(self.sessions)}"
            )
            return
        idle = int(time.monotonic() - session.last_active)
        await update.message.reply_text(
            f"Session ID: {session.session_id or '(new)'}\n"
            f"Model: {session.model}\n"
            f"Idle: {idle}s\n"
            f"Busy: {session.busy}\n"
            f"Active sessions: {len(self.sessions)}"
        )

    @staticmethod
    async def _keep_typing(chat_id: int, bot, stop_event: asyncio.Event) -> None:
        """Send 'typing' action every 4 seconds until stop_event is set."""
        while not stop_event.is_set():
            try:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=4.0)
                break
            except asyncio.TimeoutError:
                continue

    async def _handle_message(self, update, context) -> None:
        user_id = update.effective_user.id
        if not self._is_authorized(user_id):
            await update.message.reply_text("Unauthorized.")
            return

        chat_id = update.effective_chat.id
        text = update.message.text
        if not text:
            return

        session = self._get_or_create_session(chat_id)
        if session.busy:
            await update.message.reply_text("Still processing the previous message. Please wait.")
            return

        session.busy = True

        # Start continuous typing indicator (always active regardless of output_format)
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(self._keep_typing(chat_id, context.bot, stop_typing))

        try:
            orc = self._make_orchestrator(session)

            if self.output_format == "streaming":
                result = await self._run_streaming(orc, text, update)
            else:
                result = await self._run_batch(orc, text)

            session.last_active = time.monotonic()

            if result.success and result.session_id:
                session.session_id = result.session_id

            if result.success and result.output:
                for chunk in _split_message(result.output):
                    formatted = _markdown_to_telegram_html(chunk)
                    try:
                        await update.message.reply_text(formatted, parse_mode="HTML")
                    except Exception:
                        await update.message.reply_text(chunk)
            elif result.success:
                await update.message.reply_text("(Claude returned no output)")
            else:
                await update.message.reply_text(f"Error: {result.error}")

        except asyncio.TimeoutError:
            await update.message.reply_text(
                f"Timed out after {self.subprocess_timeout}s. Try a simpler prompt or increase the timeout."
            )
        except Exception as e:
            logger.exception("Error handling message")
            await update.message.reply_text(f"Error: {e}")
        finally:
            stop_typing.set()
            await typing_task
            session.busy = False

    async def _run_batch(self, orc: ClaudeOrchestrator, text: str):
        """Batch mode: block until done, return result. No intermediate messages."""
        return await asyncio.wait_for(
            asyncio.to_thread(orc.run, text),
            timeout=self.subprocess_timeout,
        )

    async def _run_streaming(self, orc: ClaudeOrchestrator, text: str, update):
        """Streaming mode: send tool calls, thinking, and result status in real-time."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict | None] = asyncio.Queue()

        def on_event(event: dict) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, event)

        async def send_events() -> None:
            last_send = 0.0
            thinking_sent = False

            while True:
                event = await queue.get()
                if event is None:
                    break

                items = _event_to_telegram(event)

                for category, formatted_text in items:
                    if category in ("tool_done", "text", "status"):
                        continue

                    if category == "thinking":
                        if thinking_sent:
                            continue
                        thinking_sent = True
                    else:
                        thinking_sent = False

                    now = time.monotonic()
                    elapsed = now - last_send
                    if elapsed < _TG_MIN_INTERVAL:
                        await asyncio.sleep(_TG_MIN_INTERVAL - elapsed)

                    try:
                        await update.message.reply_text(formatted_text)
                    except Exception:
                        logger.debug("Failed to send status message: %s", formatted_text)

                    last_send = time.monotonic()

        consumer_task = asyncio.create_task(send_events())

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(orc.run, text, on_event=on_event),
                timeout=self.subprocess_timeout,
            )
        finally:
            queue.put_nowait(None)
            await consumer_task

        return result

    def start(self) -> None:
        """Build the Telegram application and start polling."""
        from telegram import Update
        from telegram.ext import (
            ApplicationBuilder,
            CommandHandler,
            MessageHandler,
            filters,
        )

        app = ApplicationBuilder().token(self.token).build()

        app.add_handler(CommandHandler("start", self._handle_start))
        app.add_handler(CommandHandler("reset", self._handle_reset))
        app.add_handler(CommandHandler("model", self._handle_model))
        app.add_handler(CommandHandler("status", self._handle_status))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))

        async def post_init(application) -> None:
            application.create_task(self._session_reaper())

        app.post_init = post_init

        logger.info("Starting CCFlow Telegram bot (model=%s, danger=%s)", self.model, self.danger)
        print(f"CCFlow Telegram bot started (model={self.model})")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


def bot_main(args: list[str]) -> None:
    """CLI entry point for `ccflow bot ...`."""
    parser = argparse.ArgumentParser(description="CCFlow Telegram Bot")
    parser.add_argument("-m", "--model", default="opus", help="Default model (default: opus)")
    parser.add_argument("--danger", action="store_true", help="Skip all permission checks")
    parser.add_argument("--allowed-tools", nargs="*", help="Allowed tools list")
    parser.add_argument("--max-budget", type=float, help="Max budget in USD per invocation")
    parser.add_argument("--cwd", help="Working directory for claude subprocess")
    parser.add_argument("--log-dir", default="logs", help="Log directory (default: logs)")
    parser.add_argument("--session-timeout", type=int, default=180, help="Session idle timeout in seconds (default: 180)")
    parser.add_argument("--subprocess-timeout", type=int, default=300, help="Max time per Claude call in seconds (default: 300)")
    parsed = parser.parse_args(args)

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN environment variable is required.")
        raise SystemExit(1)

    allowed_users = None
    users_env = os.environ.get("TELEGRAM_ALLOWED_USERS")
    if users_env:
        allowed_users = {int(uid.strip()) for uid in users_env.split(",") if uid.strip()}

    subprocess_timeout = parsed.subprocess_timeout
    timeout_env = os.environ.get("CLAUDE_SUBPROCESS_TIMEOUT")
    if timeout_env and not any(a.startswith("--subprocess-timeout") for a in args):
        subprocess_timeout = int(timeout_env)

    output_format = os.environ.get("OUTPUT_FORMAT", "streaming").lower()
    if output_format not in ("streaming", "batch"):
        output_format = "streaming"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    bot = TelegramBot(
        token=token,
        allowed_users=allowed_users,
        model=parsed.model,
        danger=parsed.danger,
        allowed_tools=parsed.allowed_tools,
        max_budget_usd=parsed.max_budget,
        cwd=parsed.cwd,
        log_dir=parsed.log_dir,
        session_timeout=parsed.session_timeout,
        subprocess_timeout=subprocess_timeout,
        output_format=output_format,
    )
    bot.start()
