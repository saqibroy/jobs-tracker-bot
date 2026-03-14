"""Telegram notifier — sends job alerts as HTML-formatted messages via Bot API."""

from __future__ import annotations

import asyncio

from loguru import logger
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError

import config
from models.job import Job
from filters.match import match_score_bar
from notifiers.base import BaseNotifier

# Telegram rate limit: ~30 messages per second to the same chat.
# We use a small delay to be safe.
_DELAY_BETWEEN_MESSAGES = 1.0  # seconds


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
