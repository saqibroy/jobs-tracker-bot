"""Telegram notifier — sends job alerts as HTML-formatted messages via Bot API.

Also supports registering /commands with BotFather-style command handling:
  /scan    — trigger immediate scan
  /stats   — show statistics
  /help    — list commands
  /pause   — pause scanning
  /resume  — resume scanning
"""

from __future__ import annotations

import asyncio

from loguru import logger
from telegram import Bot, BotCommand, Update
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

import config
from models.job import Job
from filters.match import match_score_bar
from notifiers.base import BaseNotifier

# Telegram rate limit: ~30 messages per second to the same chat.
# We use a small delay to be safe.
_DELAY_BETWEEN_MESSAGES = 1.0  # seconds

# Commands to register with BotFather
_BOT_COMMANDS = [
    BotCommand("scan", "Trigger an immediate scan"),
    BotCommand("stats", "Show job tracking statistics"),
    BotCommand("help", "List available commands"),
    BotCommand("pause", "Pause scanning"),
    BotCommand("resume", "Resume scanning"),
]


class TelegramNotifier(BaseNotifier):
    """Send job notifications to a Telegram chat via Bot API."""

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
    ) -> None:
        self._bot_token = bot_token or config.TELEGRAM_BOT_TOKEN
        self._chat_id = chat_id or config.TELEGRAM_CHAT_ID

    @property
    def name(self) -> str:
        return "telegram"

    # ── Public API ─────────────────────────────────────────────────────

    async def send_jobs(self, jobs: list[Job]) -> None:
        """Send each job as a separate HTML message."""
        if not self._bot_token or not self._chat_id:
            logger.warning("Telegram bot token or chat ID not configured — skipping")
            return

        ngo_jobs = [j for j in jobs if j.is_ngo]
        general_jobs = [j for j in jobs if not j.is_ngo]

        logger.info(
            "Telegram: sending {} jobs ({} NGO, {} general)",
            len(jobs), len(ngo_jobs), len(general_jobs),
        )

        bot = Bot(token=self._bot_token)
        sent = 0

        for job in jobs:
            try:
                message = self._format_job(job)
                await bot.send_message(
                    chat_id=self._chat_id,
                    text=message,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                sent += 1
            except RetryAfter as exc:
                logger.warning(
                    "Telegram rate limited — waiting {}s", exc.retry_after
                )
                await asyncio.sleep(exc.retry_after)
                # Retry once after waiting
                try:
                    message = self._format_job(job)
                    await bot.send_message(
                        chat_id=self._chat_id,
                        text=message,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                    sent += 1
                except TelegramError:
                    logger.exception("Telegram: retry failed for '{}'", job.title)
            except TelegramError:
                logger.exception("Telegram: failed to send job '{}'", job.title)

            # Rate-limit courtesy delay
            if sent < len(jobs):
                await asyncio.sleep(_DELAY_BETWEEN_MESSAGES)

        logger.info("Telegram: {}/{} jobs sent successfully", sent, len(jobs))

    async def send_test_message(self) -> None:
        """Send a simple test message to verify the bot configuration."""
        if not self._bot_token or not self._chat_id:
            logger.error("Telegram bot token or chat ID not configured")
            return

        bot = Bot(token=self._bot_token)
        await bot.send_message(
            chat_id=self._chat_id,
            text="🤖 <b>Job Tracker Bot — Test</b>\n\nTelegram notifications are working!",
            parse_mode=ParseMode.HTML,
        )
        logger.info("Telegram test message sent")

    async def register_commands(self) -> None:
        """Register /commands with the Telegram Bot API (BotFather style)."""
        if not self._bot_token:
            logger.warning("Telegram bot token not configured — skipping command registration")
            return

        try:
            bot = Bot(token=self._bot_token)
            await bot.set_my_commands(_BOT_COMMANDS)
            logger.info("Telegram bot commands registered: {}", [c.command for c in _BOT_COMMANDS])
        except TelegramError:
            logger.exception("Failed to register Telegram commands")

    def build_application(
        self,
        scan_callback=None,
        stats_callback=None,
    ) -> Application:
        """Build a python-telegram-bot Application with command handlers.

        Args:
            scan_callback: async callable that triggers a scan and returns list[Job]
            stats_callback: async callable that returns stats dict

        Returns an Application that can be started with ``app.run_polling()``
        or integrated into an existing event loop.
        """
        self._scan_callback = scan_callback
        self._stats_callback = stats_callback

        app = Application.builder().token(self._bot_token).build()
        app.add_handler(CommandHandler("scan", self._cmd_scan))
        app.add_handler(CommandHandler("stats", self._cmd_stats))
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CommandHandler("pause", self._cmd_pause))
        app.add_handler(CommandHandler("resume", self._cmd_resume))

        return app

    # ── Command handlers ───────────────────────────────────────────────

    async def _cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /scan command."""
        if not self._scan_callback:
            await update.message.reply_text("⚠️ Scan not available.")
            return

        await update.message.reply_text("🔄 Scanning now...")
        try:
            new_jobs = await self._scan_callback()
            count = len(new_jobs) if new_jobs else 0
            if count > 0:
                await update.message.reply_text(f"✅ Scan complete — {count} new job{'s' if count != 1 else ''} found")
            else:
                await update.message.reply_text("✅ Scan complete — no new jobs")
        except Exception as exc:
            logger.exception("Telegram /scan failed")
            await update.message.reply_text(f"❌ Scan failed: {exc}")

    async def _cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /stats command."""
        if not self._stats_callback:
            await update.message.reply_text("⚠️ Stats not available.")
            return

        try:
            stats = await self._stats_callback()
            text = (
                f"📊 <b>Job Tracker Stats</b>\n\n"
                f"📦 Total tracked: <b>{stats['total']:,}</b>\n"
                f"🆕 Last 24 hours: <b>{stats['new_24h']}</b>\n"
                f"🟢 NGO jobs: <b>{stats['ngo_count']}</b>\n"
                f"🔵 General: <b>{stats['total'] - stats['ngo_count']}</b>"
            )
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        except Exception as exc:
            logger.exception("Telegram /stats failed")
            await update.message.reply_text(f"❌ Stats failed: {exc}")

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command."""
        text = (
            "🤖 <b>Job Tracker Bot — Commands</b>\n\n"
            "/scan — Trigger an immediate scan\n"
            "/stats — Show job tracking statistics\n"
            "/help — Show this help message\n"
            "/pause — Pause scanning\n"
            "/resume — Resume scanning"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /pause command."""
        try:
            from health import set_paused, is_paused
            if is_paused():
                await update.message.reply_text("⏸️ Scanning is already paused.")
                return
            set_paused(True)
            await update.message.reply_text("⏸️ Scanning paused. Use /resume to continue.")
            logger.info("Scanning paused via Telegram /pause command")
        except Exception:
            await update.message.reply_text("⏸️ Pause state updated.")

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /resume command."""
        try:
            from health import set_paused, is_paused
            if not is_paused():
                await update.message.reply_text("▶️ Scanning is already running.")
                return
            set_paused(False)
            await update.message.reply_text("▶️ Scanning resumed!")
            logger.info("Scanning resumed via Telegram /resume command")
        except Exception:
            await update.message.reply_text("▶️ Resume state updated.")

    # ── Internals ──────────────────────────────────────────────────────

    @staticmethod
    def _format_job(job: Job) -> str:
        """Format a single job as an HTML message for Telegram."""
        icon = "🟢" if job.is_ngo else "🔵"
        category = "NGO/Nonprofit" if job.is_ngo else "General"

        lines = [
            f'{icon} <b>{_escape_html(job.title)}</b> — {_escape_html(job.company)}',
            f'📍 {_escape_html(job.location)} ({job.remote_scope or "unknown"}) · {category}',
        ]

        if job.salary:
            lines.append(f"💰 {_escape_html(job.salary)}")

        bar = match_score_bar(job.match_score)
        lines.append(f"📊 {job.match_score}% match {bar}")

        if job.tags:
            tags_str = ", ".join(job.tags[:5])
            lines.append(f"🏷️ {_escape_html(tags_str)}")

        lines.append(f"🌍 Source: {_escape_html(job.source)}")

        if job.posted_at:
            lines.append(f"📅 Posted: {job.posted_at.strftime('%Y-%m-%d')}")

        lines.append(f'🔗 <a href="{job.url}">Apply here</a>')

        return "\n".join(lines)


def _escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram HTML parse mode."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
