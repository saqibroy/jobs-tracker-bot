"""Discord notifier — sends job alerts as rich embeds via webhook."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from discord_webhook import AsyncDiscordWebhook, DiscordEmbed
from loguru import logger

import config
from models.job import Job
from filters.match import match_score_bar
from notifiers.base import BaseNotifier

# Embed colours
_NGO_COLOUR = 0x2ECC71    # green
_GENERAL_COLOUR = 0x3498DB  # blue

# Discord webhook rate-limit: ~30 requests per 60 seconds per webhook.
# We add a small delay between embeds to stay safe.
_DELAY_BETWEEN_EMBEDS = 1.0  # seconds


class DiscordNotifier(BaseNotifier):
    """Send job notifications to a Discord channel via webhook.

    If DISCORD_WEBHOOK_URL_NGO is configured, NGO jobs go to that webhook
    and general jobs go to the main DISCORD_WEBHOOK_URL.
    Otherwise, all jobs go to the main webhook.
    """

    def __init__(
        self,
        webhook_url: str | None = None,
        webhook_url_ngo: str | None = None,
    ) -> None:
        self._webhook_url = webhook_url or config.DISCORD_WEBHOOK_URL
        self._webhook_url_ngo = webhook_url_ngo or config.DISCORD_WEBHOOK_URL_NGO

    @property
    def name(self) -> str:
        return "discord"

    # ── Public API ─────────────────────────────────────────────────────

    async def send_jobs(self, jobs: list[Job]) -> None:
        """Send each job as a separate embed message."""
        if not self._webhook_url:
            logger.warning("Discord webhook URL not configured — skipping")
            return

        ngo_jobs = [j for j in jobs if j.is_ngo]
        general_jobs = [j for j in jobs if not j.is_ngo]

        logger.info(
            "Discord: sending {} jobs ({} NGO, {} general)",
            len(jobs), len(ngo_jobs), len(general_jobs),
        )

        sent = 0
        for job in jobs:
            try:
                await self._send_single_job(job)
                sent += 1
            except Exception:
                logger.exception("Discord: failed to send job '{}'", job.title)

            # Rate-limit courtesy delay (skip after last one)
            if sent < len(jobs):
                await asyncio.sleep(_DELAY_BETWEEN_EMBEDS)

        logger.info("Discord: {}/{} jobs sent successfully", sent, len(jobs))

    async def send_test_message(self) -> None:
        """Send a simple test embed to verify the webhook works."""
        if not self._webhook_url:
            logger.error("Discord webhook URL not configured")
            return

        webhook = AsyncDiscordWebhook(url=self._webhook_url, content="")
        embed = DiscordEmbed(
            title="🤖 Job Tracker Bot — Test",
            description="Webhook is configured and working!",
            color=_NGO_COLOUR,
        )
        embed.set_timestamp()
        webhook.add_embed(embed)

        response = await webhook.execute()
        if response and hasattr(response, "status_code"):
            logger.info("Discord test message sent (status {})", response.status_code)
        else:
            logger.info("Discord test message sent")

    # ── Internals ──────────────────────────────────────────────────────

    async def _send_single_job(self, job: Job) -> None:
        """Build and send a single rich embed for a job."""
        is_ngo = job.is_ngo
        colour = _NGO_COLOUR if is_ngo else _GENERAL_COLOUR
        icon = "🟢" if is_ngo else "🔵"
        category = "NGO/Nonprofit" if is_ngo else "General"

        # Choose webhook: NGO jobs go to dedicated channel if configured
        if is_ngo and self._webhook_url_ngo:
            url = self._webhook_url_ngo
        else:
            url = self._webhook_url

        # Title links to the job URL
        title = f"{icon} {job.title}"
        embed = DiscordEmbed(
            title=title[:256],  # Discord limit
            url=job.url,
            color=colour,
        )

        # Fields
        embed.add_embed_field(name="🏢 Company", value=job.company, inline=True)

        # Build location display with city/postal details if available
        location_parts = []
        if job.company_postal_code:
            location_parts.append(job.company_postal_code)
        if job.company_city:
            location_parts.append(job.company_city)
        if job.company_country:
            location_parts.append(job.company_country)

        if location_parts:
            detailed_loc = " ".join(location_parts[:2])  # postal + city
            if job.company_country:
                detailed_loc = f"{detailed_loc}, {job.company_country}" if job.company_city else job.company_country
            remote_tag = " · Remote" if job.is_remote else ""
            location_display = f"{detailed_loc}{remote_tag}"
        else:
            location_display = f"{job.location} ({job.remote_scope or 'unknown'})"

        embed.add_embed_field(
            name="📍 Location",
            value=location_display,
            inline=True,
        )

        if job.salary:
            embed.add_embed_field(name="💰 Salary", value=job.salary, inline=True)

        bar = match_score_bar(job.match_score)
        embed.add_embed_field(
            name="📊 Match",
            value=f"{bar}  {job.match_score}%",
            inline=True,
        )

        if job.tags:
            tags_str = ", ".join(job.tags[:8])  # cap tag display
            embed.add_embed_field(name="🏷️ Tags", value=tags_str, inline=False)

        embed.add_embed_field(name="📂 Category", value=category, inline=True)
        embed.add_embed_field(name="🌍 Source", value=job.source, inline=True)

        if job.posted_at:
            embed.add_embed_field(
                name="📅 Posted",
                value=job.posted_at.strftime("%Y-%m-%d"),
                inline=True,
            )

        # Footer with timestamp
        embed.set_footer(text=f"Job Tracker Bot • {job.source}")
        embed.set_timestamp(datetime.now(timezone.utc).isoformat())

        # Send
        webhook = AsyncDiscordWebhook(url=url, content="")
        webhook.add_embed(embed)
        response = await webhook.execute()

        if response and hasattr(response, "status_code") and response.status_code >= 400:
            logger.error(
                "Discord: HTTP {} sending '{}'", response.status_code, job.title
            )
        else:
            logger.debug("Discord: sent '{}'", job.title)
