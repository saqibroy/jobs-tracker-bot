"""Discord notifier — sends job alerts as rich embeds via webhook."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from discord_webhook import AsyncDiscordWebhook, DiscordEmbed
from loguru import logger

import config
from filters.employment import employment_display_lines
from models.job import Job
from filters.match import match_score_bar
from notifiers.base import (
    BaseNotifier,
    DeliveryDestination,
    DeliverySuccess,
    GroupedJobPayload,
    resolve_discord_destination,
)

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
    "linkedin": "🔷",
    "linkedin_alert": "🔷",
    "indeed_alert": "🟦",
    "stepstone": "🟦",
    "greenhouse": "🟩",
    "ashby": "🟨",
    "personio": "🟧",
    "lever": "🟦",
    "workable": "🟪",
    "jsonld": "🏢",
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

    @property
    def general_configured(self) -> bool:
        return bool(self._webhook_url)

    @property
    def ngo_configured(self) -> bool:
        return bool(self._webhook_url_ngo)

    def has_destination(self, destination: DeliveryDestination) -> bool:
        if destination == "discord_general":
            return self.general_configured
        if destination == "discord_ngo":
            return self.ngo_configured
        return False

    # ── Public API ─────────────────────────────────────────────────────

    async def send_jobs(
        self,
        jobs: list[Job],
        *,
        include_batch_header: bool = True,
    ) -> list[DeliverySuccess]:
        """Send each job separately and return only exact successful jobs.

        ``include_batch_header`` defaults to the historical public behavior.
        Receipt-driven orchestration disables it because each logical destination
        is already processed as an independent bounded batch.
        """
        if not self._webhook_url and not self._webhook_url_ngo:
            logger.warning("Discord webhook URL not configured — skipping")
            return []

        ngo_jobs = [j for j in jobs if j.is_ngo]
        general_jobs = [j for j in jobs if not j.is_ngo]

        logger.info(
            "Discord: sending {} jobs ({} NGO, {} general)",
            len(jobs), len(ngo_jobs), len(general_jobs),
        )

        # Send a batch header when there are multiple jobs
        if include_batch_header and len(jobs) > 1 and self._webhook_url:
            try:
                await self._send_batch_header(jobs)
            except Exception:
                logger.exception("Discord: failed to send batch header")
            await asyncio.sleep(_DELAY_BETWEEN_EMBEDS)

        successes: list[DeliverySuccess] = []
        for index, job in enumerate(jobs):
            try:
                success = await self._send_single_job(job)
                if success is not None:
                    successes.append(success)
            except Exception:
                logger.exception("Discord: failed to send job '{}'", job.title)

            # Rate-limit courtesy delay (skip after last one)
            if index < len(jobs) - 1:
                await asyncio.sleep(_DELAY_BETWEEN_EMBEDS)

        logger.info(
            "Discord: {}/{} jobs sent successfully", len(successes), len(jobs)
        )
        return successes

    async def send_grouped_digest(
        self,
        payload: GroupedJobPayload,
        *,
        total_jobs: int,
    ) -> list[DeliverySuccess]:
        """Send one general-channel digest and return its exact membership."""

        if not self._webhook_url or not payload.jobs:
            return []
        try:
            webhook = AsyncDiscordWebhook(url=self._webhook_url, content="")
            embed = DiscordEmbed(
                title=payload.title[:256],
                description=payload.description,
                color=_DIGEST_COLOUR,
            )
            embed.add_embed_field(
                name="📊 Database",
                value=f"`{total_jobs}` total jobs tracked",
                inline=True,
            )
            footer = (
                "Job Tracker Bot · Explore Digest"
                if "Explore" in payload.title
                else "Job Tracker Bot · Periodic Digest"
            )
            embed.set_footer(text=footer)
            embed.set_timestamp(datetime.now(timezone.utc).isoformat())
            webhook.add_embed(embed)
            response = await webhook.execute()
            if _http_response_failed(response):
                logger.error(
                    "Discord: digest HTTP failure status {}",
                    getattr(response, "status_code", "unknown"),
                )
                return []
        except Exception:
            logger.exception("Discord: grouped digest delivery failed")
            return []
        return [
            DeliverySuccess(job.id, "discord_general") for job in payload.jobs
        ]

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

    async def _send_single_job(self, job: Job) -> DeliverySuccess | None:
        """Build and send one embed, returning its exact logical success."""
        is_ngo = job.is_ngo
        high_match = job.match_score >= 70

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

        destination = resolve_discord_destination(
            job,
            general_configured=self.general_configured,
            ngo_configured=self.ngo_configured,
        )
        if destination is None:
            logger.debug("Discord: no configured destination for '{}'", job.title)
            return None
        url = (
            self._webhook_url_ngo
            if destination == "discord_ngo"
            else self._webhook_url
        )

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
        if job.workplace_type == "remote":
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
            model_label = job.workplace_type.title()
            loc_text = f"📍  {job.location}  ·  *{model_label}*"
        desc_lines.append(loc_text)

        if job.eligibility_reasons:
            desc_lines.append(f"✅  {job.eligibility_reasons[0]}")

        desc_lines.extend(employment_display_lines(job))

        # Salary (if available)
        if job.salary:
            desc_lines.append(f"💰  {job.salary}")

        desc_lines.append("")  # blank line separator

        # Match score — visual bar
        if job.match_score is not None and job.match_score > 0:
            bar = match_score_bar(job.match_score)
            match_label = "🔥 Excellent" if job.match_score >= 80 else "⭐ Strong" if job.match_score >= 50 else "📊 Moderate" if job.match_score >= 20 else "📊 Low"
            desc_lines.append(f"{match_label} match — **{job.match_score}%**\n`{bar}`")
        elif job.match_score is not None:
            bar = match_score_bar(0)
            desc_lines.append(f"📊 Match — **0%**\n`{bar}`")
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

        if job.match_breakdown:
            breakdown = " · ".join(
                f"{name.replace('_', ' ').title()} **{score}**"
                for name, score in job.match_breakdown.items()
            )
            embed.add_embed_field(name="🎯 Match breakdown", value=breakdown, inline=False)
        if job.match_reasons:
            embed.add_embed_field(
                name="Why it matched",
                value="; ".join(job.match_reasons[:4])[:1024],
                inline=False,
            )

        # ── Footer: source + posted time ───────────────────────────────
        posted_str = _format_relative_time(job.posted_at) if job.posted_at else "Unknown date"
        footer_text = f"{source_icon} {job.source}  ·  📅 {posted_str}"
        embed.set_footer(text=footer_text)
        embed.set_timestamp(datetime.now(timezone.utc).isoformat())

        # ── Send ───────────────────────────────────────────────────────
        webhook = AsyncDiscordWebhook(url=url, content="")
        webhook.add_embed(embed)
        response = await webhook.execute()

        if _http_response_failed(response):
            logger.error(
                "Discord: HTTP {} sending '{}'", response.status_code, job.title
            )
            return None
        logger.debug("Discord: sent '{}'", job.title)
        return DeliverySuccess(job.id, destination)


def _http_response_failed(response: object) -> bool:
    status = getattr(response, "status_code", None)
    return isinstance(status, int) and status >= 400


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
