"""Telegram bot that forwards messages to ClaudeOrchestrator and sends responses back."""

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

import argparse
import asyncio
import html
import logging
import os
import time
from dataclasses import dataclass, field

from ccflow.agent.orchestrator import ClaudeOrchestrator
from ccflow.telegram.event_formatter import (
    _event_to_telegram,
    _markdown_to_telegram_html,
    _render_table_image,
    _split_message,
    _split_text_and_tables,
    format_event,
)

logger = logging.getLogger(__name__)

# Minimum interval between Telegram API calls (rate limit safety)
_TG_MIN_INTERVAL = 1.0


@dataclass
class ChatSession:
    """Tracks a Telegram chat's Claude session state."""

    session_id: str | None = None
    model: str = "opus"
    last_active: float = field(default_factory=time.monotonic)
    busy: bool = False
    cwd: str | None = None
    status_message_id: int | None = None


class TelegramBot:
    """Telegram bot that bridges messages to ClaudeOrchestrator.

    Each Telegram chat maintains its own Claude session for multi-turn
    conversations. Sessions persist until the user sends /reset.
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
        subprocess_timeout: int = 300,
        output_format: str = "streaming",
        enable_table_image: bool = False,
    ) -> None:
        self.token = token
        self.allowed_users = allowed_users
        self.model = model
        self.danger = danger
        self.allowed_tools = allowed_tools
        self.max_budget_usd = max_budget_usd
        self.project_root = Path(cwd).resolve() if cwd else Path.cwd()
        self.log_dir = log_dir
        self.subprocess_timeout = subprocess_timeout
        self.output_format = output_format
        self.enable_table_image = enable_table_image
        self.sessions: dict[int, ChatSession] = {}

    def _is_authorized(self, user_id: int) -> bool:
        return self.allowed_users is None or user_id in self.allowed_users

    def _get_or_create_session(self, chat_id: int) -> ChatSession:
        if chat_id not in self.sessions:
            self.sessions[chat_id] = ChatSession(
                model=self.model,
                cwd=str(self.project_root),
            )
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
            cwd=session.cwd,
        )

    async def _handle_start(self, update, context) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        subdirs = sorted(
            p.name for p in self.project_root.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )

        lines = [
            "Welcome to CCFlow Bot!\n",
            "Send any message and I'll forward it to Claude.\n",
            "Commands:",
            "/reset — End current conversation & return to project root",
            "/model <name> — Switch model (e.g. sonnet, opus)",
            "/mkdir <name> — Create a new project directory",
            "/status — Show session info",
        ]

        if subdirs:
            lines.append("\nSelect a project to work in, or just send a message to work in the project root:")
            keyboard = [
                [InlineKeyboardButton(name, callback_data=f"cd:{name}")]
                for name in subdirs
            ]
            await update.message.reply_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        else:
            lines.append("\nNo project directories found. Use /mkdir <name> to create one, or just send a message to start.")
            await update.message.reply_text("\n".join(lines))

    async def _handle_reset(self, update, context) -> None:
        chat_id = update.effective_chat.id
        session = self.sessions.get(chat_id)
        if session:
            if session.status_message_id:
                try:
                    await context.bot.unpin_chat_message(chat_id, session.status_message_id)
                except Exception:
                    pass
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
                f"CWD: {self.project_root}\n"
                f"Active sessions: {len(self.sessions)}"
            )
            return
        idle = int(time.monotonic() - session.last_active)
        await update.message.reply_text(
            f"Session ID: {session.session_id or '(new)'}\n"
            f"Model: {session.model}\n"
            f"CWD: {session.cwd}\n"
            f"Idle: {idle}s\n"
            f"Busy: {session.busy}\n"
            f"Active sessions: {len(self.sessions)}"
        )

    async def _handle_callback(self, update, context) -> None:
        query = update.callback_query
        data = query.data or ""

        if not data.startswith("cd:"):
            await query.answer("Unknown action.")
            return

        dirname = data[3:]
        target = (self.project_root / dirname).resolve()
        if not target.is_relative_to(self.project_root) or not target.is_dir():
            await query.answer("Invalid directory.")
            return

        chat_id = update.effective_chat.id
        session = self._get_or_create_session(chat_id)

        # Unpin previous status message if any
        if session.status_message_id:
            try:
                await context.bot.unpin_chat_message(chat_id, session.status_message_id)
            except Exception:
                pass

        session.cwd = str(target)

        # Send and pin new status message
        msg = await context.bot.send_message(chat_id, f"\U0001f4c2 {dirname}")
        session.status_message_id = msg.message_id
        try:
            await context.bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
        except Exception:
            pass

        await query.answer(f"Switched to {dirname}")

    async def _handle_mkdir(self, update, context) -> None:
        if not context.args:
            await update.message.reply_text("Usage: /mkdir <name>")
            return

        name = context.args[0]
        if not name or "/" in name or ".." in name:
            await update.message.reply_text("Invalid directory name.")
            return

        target = (self.project_root / name).resolve()
        if not target.is_relative_to(self.project_root):
            await update.message.reply_text("Invalid directory name.")
            return
        if target.exists():
            await update.message.reply_text(f"'{name}' already exists.")
            return

        target.mkdir()
        await update.message.reply_text(f"Created directory: {name}")

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

        orc: ClaudeOrchestrator | None = None
        try:
            orc = self._make_orchestrator(session)

            if self.output_format == "streaming":
                result = await self._run_streaming(orc, text, update)
            else:
                result = await self._run_batch(orc, text)

            if result.success and result.session_id:
                session.session_id = result.session_id
            elif not result.success and session.session_id:
                # Resume failed (e.g. stale session) — clear so next message starts fresh
                logger.warning("Resume failed for session %s, clearing", session.session_id)
                session.session_id = None

            if result.success and result.output:
                if self.enable_table_image:
                    await self._send_rich_output(update, result.output)
                else:
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
            # Kill the orphan claude subprocess
            proc = getattr(orc, "_proc", None)
            if proc is not None:
                proc.kill()
                proc.wait()
            await update.message.reply_text(
                f"Timed out after {self.subprocess_timeout}s. Try a simpler prompt or increase the timeout."
            )
        except Exception as e:
            logger.exception("Error handling message")
            await update.message.reply_text(f"Error: {e}")
        finally:
            stop_typing.set()
            await typing_task
            session.last_active = time.monotonic()
            session.busy = False

    async def _send_rich_output(self, update, output: str) -> None:
        """Send output with tables rendered as images and text as HTML messages."""
        segments = _split_text_and_tables(output)

        for seg_type, content in segments:
            if seg_type == "table":
                try:
                    buf = await asyncio.to_thread(_render_table_image, content)
                    await update.message.reply_photo(photo=buf)
                except Exception:
                    logger.debug("Failed to render table as image, sending as text")
                    await update.message.reply_text(f"<pre>{html.escape(content)}</pre>", parse_mode="HTML")
            else:
                for chunk in _split_message(content):
                    formatted = _markdown_to_telegram_html(chunk)
                    try:
                        await update.message.reply_text(formatted, parse_mode="HTML")
                    except Exception:
                        await update.message.reply_text(chunk)

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
            CallbackQueryHandler,
            CommandHandler,
            MessageHandler,
            filters,
        )

        app = ApplicationBuilder().token(self.token).build()

        app.add_handler(CommandHandler("start", self._handle_start))
        app.add_handler(CommandHandler("reset", self._handle_reset))
        app.add_handler(CommandHandler("model", self._handle_model))
        app.add_handler(CommandHandler("status", self._handle_status))
        app.add_handler(CommandHandler("mkdir", self._handle_mkdir))
        app.add_handler(CallbackQueryHandler(self._handle_callback))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))

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

    enable_table_image = os.environ.get("ENABLE_TABLE_IMAGE", "").lower() in ("1", "true", "yes")

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
        subprocess_timeout=subprocess_timeout,
        output_format=output_format,
        enable_table_image=enable_table_image,
    )
    bot.start()
