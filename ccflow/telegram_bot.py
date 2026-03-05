"""Telegram bot that forwards messages to ClaudeOrchestrator and sends responses back."""

import argparse
import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

from ccflow import ClaudeOrchestrator

logger = logging.getLogger(__name__)


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
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")

        try:
            orc = self._make_orchestrator(session)
            result = await asyncio.wait_for(
                asyncio.to_thread(orc.run, text),
                timeout=self.subprocess_timeout,
            )

            session.last_active = time.monotonic()

            if result.success and result.session_id:
                session.session_id = result.session_id

            if result.success and result.output:
                for chunk in _split_message(result.output):
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
            session.busy = False

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
        subprocess_timeout=parsed.subprocess_timeout,
    )
    bot.start()
