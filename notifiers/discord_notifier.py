"""Discord notifier — sends job alerts as rich embeds via webhook."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from discord_webhook import AsyncDiscordWebhook, DiscordEmbed
from loguru import logger

import config
from models.job import Job
from filters.match import match_score_bar
from notifiers.base import BaseNotifier

# ── Embed colours ──────────────────────────────────────────────────────────
_NGO_COLOUR = 0x10B981    # emerald green
_GENERAL_COLOUR = 0x6366F1  # indigo
_HIGH_MATCH_COLOUR = 0xF59E0B  # amber — for match ≥ 60%
_DIGEST_COLOUR = 0x8B5CF6     # violet

# ── Source icons (Unicode emojis for visual differentiation) ───────────────
_SOURCE_ICONS: dict[str, str] = {
    "remotive": "🟣",
    "arbeitnow": "🔴",
    "remoteok": "🟠",
    "weworkremotely": "⚪",
    "idealist": "🟡",
    "reliefweb": "🔵",
    "techjobsforgood": "🟢",
    "eurobrussels": "🔵",
    "hours80k": "⚫",
    "goodjobs": "🟢",
    "devex": "🔴",
}

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

        # Send a batch header when there are multiple jobs
        if len(jobs) > 1:
            await self._send_batch_header(jobs)
            await asyncio.sleep(_DELAY_BETWEEN_EMBEDS)

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
            title="✅ Job Tracker Bot — Connected",
            description=(
                "Webhook is configured and working!\n\n"
                "The bot will send job alerts here when new matches are found."
            ),
            color=_NGO_COLOUR,
        )
        embed.set_footer(text="Job Tracker Bot")
        embed.set_timestamp()
        webhook.add_embed(embed)

        response = await webhook.execute()
        if response and hasattr(response, "status_code"):
            logger.info("Discord test message sent (status {})", response.status_code)
        else:
            logger.info("Discord test message sent")

    # ── Internals ──────────────────────────────────────────────────────

    async def _send_batch_header(self, jobs: list[Job]) -> None:
        """Send a compact header summarizing the incoming batch."""
        ngo_count = sum(1 for j in jobs if j.is_ngo)
        gen_count = len(jobs) - ngo_count
        sources = set(j.source for j in jobs)

        parts = [f"**{len(jobs)}** new job{'s' if len(jobs) != 1 else ''} found"]
        if ngo_count:
            parts.append(f"🟢 {ngo_count} NGO")
        if gen_count:
            parts.append(f"🔵 {gen_count} General")

        description = " · ".join(parts)
        source_list = " ".join(
            f"`{s}`" for s in sorted(sources)
        )

        webhook = AsyncDiscordWebhook(
            url=self._webhook_url,
            content=f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n📡 {description}\n🌐 Sources: {source_list}\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        )
        await webhook.execute()

    async def _send_single_job(self, job: Job) -> None:
        """Build and send a modern, visually polished embed for a job."""
        is_ngo = job.is_ngo
        high_match = job.match_score >= 60

        # Colour priority: high match (amber) > NGO (emerald) > general (indigo)
        if high_match:
            colour = _HIGH_MATCH_COLOUR
        elif is_ngo:
            colour = _NGO_COLOUR
        else:
            colour = _GENERAL_COLOUR

        # Category badge
        category_badge = "🏛️ NGO / Nonprofit" if is_ngo else "💼 General"
        source_icon = _SOURCE_ICONS.get(job.source, "🌐")

        # Choose webhook
        if is_ngo and self._webhook_url_ngo:
            url = self._webhook_url_ngo
        else:
            url = self._webhook_url

        # ── Build the embed description ────────────────────────────────
        # Compact description block with key info
        desc_lines: list[str] = []

        # Company line
        company_display = f"**🏢  {job.company or 'Unknown'}**"
        address_parts: list[str] = []
        if job.company_postal_code:
            address_parts.append(job.company_postal_code)
        if job.company_city:
            address_parts.append(job.company_city)
        if job.company_country:
            address_parts.append(job.company_country)
        if address_parts:
            company_display += f"\n> 📫 {', '.join(address_parts)}"
        desc_lines.append(company_display)

        # Location line
        scope = job.remote_scope or "unknown"
        if job.is_remote:
            scope_emoji = {
                "worldwide": "🌍", "eu": "🇪🇺", "germany": "🇩🇪",
            }.get(scope, "📍")
            scope_label = {
                "worldwide": "Worldwide", "eu": "EU / Europe", "germany": "Germany",
            }.get(scope, scope.title())
            loc_text = f"{scope_emoji}  {job.location}"
            if scope_label.lower() not in job.location.lower():
                loc_text += f"  ·  *{scope_label} remote*"
        else:
            loc_text = f"📍  {job.location}"
        desc_lines.append(loc_text)

        # Salary (if available)
        if job.salary:
            desc_lines.append(f"💰  {job.salary}")

        desc_lines.append("")  # blank line separator

        # Match score — visual bar
        if job.match_score > 0:
            bar = match_score_bar(job.match_score)
            match_label = "🔥 Excellent" if job.match_score >= 80 else "⭐ Strong" if job.match_score >= 50 else "📊 Moderate" if job.match_score >= 20 else "📊 Low"
            desc_lines.append(f"{match_label} match — **{job.match_score}%**\n`{bar}`")
        else:
            desc_lines.append("📊 Match — *not scored*")

        description = "\n".join(desc_lines)

        # ── Create embed ───────────────────────────────────────────────
        embed = DiscordEmbed(
            title=f"{job.title}"[:256],
            url=job.url,
            description=description,
            color=colour,
        )

        # Author line — used as a subtle category header
        if high_match:
            embed.set_author(name=f"� High Match · {category_badge}")
        else:
            embed.set_author(name=category_badge)

        # ── Tags as a compact field ────────────────────────────────────
        if job.tags:
            tag_chips = "  ".join(f"`{t}`" for t in job.tags[:6])
            embed.add_embed_field(name="🏷️ Tags", value=tag_chips, inline=False)

        # ── Footer: source + posted time ───────────────────────────────
        posted_str = _format_relative_time(job.posted_at) if job.posted_at else "Unknown date"
        footer_text = f"{source_icon} {job.source}  ·  📅 {posted_str}"
        embed.set_footer(text=footer_text)
        embed.set_timestamp(datetime.now(timezone.utc).isoformat())

        # ── Send ───────────────────────────────────────────────────────
        webhook = AsyncDiscordWebhook(url=url, content="")
        webhook.add_embed(embed)
        response = await webhook.execute()

        if response and hasattr(response, "status_code") and response.status_code >= 400:
            logger.error(
                "Discord: HTTP {} sending '{}'", response.status_code, job.title
            )
        else:
            logger.debug("Discord: sent '{}'", job.title)


def _format_relative_time(dt: datetime) -> str:
    """Format a datetime as human-friendly relative time for recent posts,
    falling back to YYYY-MM-DD for older posts.

    < 1 hour   → "a few minutes ago"
    1-23 hours → "X hours ago"
    1-5 days   → "X day(s) ago"
    >= 6 days  → "YYYY-MM-DD"
    """
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    delta = now - dt
    total_seconds = delta.total_seconds()

    if total_seconds < 0:
        return dt.strftime("%Y-%m-%d")

    minutes = total_seconds / 60
    hours = total_seconds / 3600
    days = delta.days

    if minutes < 60:
        return "a few minutes ago"
    elif hours < 2:
        return "1 hour ago"
    elif hours < 24:
        return f"{int(hours)} hours ago"
    elif days == 1:
        return "1 day ago"
    elif days < 6:
        return f"{days} days ago"
    else:
        return dt.strftime("%Y-%m-%d")
