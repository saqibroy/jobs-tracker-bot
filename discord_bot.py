"""Discord bot — listens for commands in a configured channel.

Commands:
  r / refresh / scan  → trigger immediate scan
  stats               → show database stats embed
  help                → show available commands

Runs alongside APScheduler in the same asyncio event loop.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import discord
from loguru import logger

import config
from storage.database import get_stats, get_total_count, init_db


class JobTrackerBot(discord.Client):
    """Minimal Discord bot for commands in a designated channel."""

    def __init__(
        self,
        command_channel_id: int,
        scan_callback=None,
        **kwargs,
    ):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, **kwargs)
        self._command_channel_id = command_channel_id
        self._scan_callback = scan_callback  # async callable that runs a scan
        self._last_scan_time: datetime | None = None
        self._next_scan_time: datetime | None = None

    def set_scan_times(
        self, last_scan: datetime | None, next_scan: datetime | None
    ) -> None:
        """Update the last/next scan timestamps for the stats display."""
        self._last_scan_time = last_scan
        self._next_scan_time = next_scan

    async def on_ready(self) -> None:
        logger.info("Discord bot logged in as {} (id={})", self.user, self.user.id)

    async def on_message(self, message: discord.Message) -> None:
        # Ignore own messages
        if message.author == self.user:
            return

        # Only respond in the configured command channel
        if message.channel.id != self._command_channel_id:
            return

        content = message.content.strip().lower()

        if content in ("r", "refresh", "scan"):
            await self._handle_scan(message)
        elif content == "stats":
            await self._handle_stats(message)
        elif content == "help":
            await self._handle_help(message)

    # ── Command handlers ───────────────────────────────────────────────

    async def _handle_scan(self, message: discord.Message) -> None:
        """Trigger an immediate scan cycle."""
        if self._scan_callback is None:
            await message.channel.send("⚠️ Scan not available — no callback configured.")
            return

        await message.channel.send("🔄 Scanning now...")

        try:
            new_jobs = await self._scan_callback()
            count = len(new_jobs) if new_jobs else 0
            if count > 0:
                await message.channel.send(f"✅ Scan complete — {count} new job{'s' if count != 1 else ''} found")
            else:
                await message.channel.send("✅ Scan complete — no new jobs")
        except Exception as exc:
            logger.exception("Manual scan failed")
            await message.channel.send(f"❌ Scan failed: {exc}")

    async def _handle_stats(self, message: discord.Message) -> None:
        """Send a stats embed to the channel."""
        try:
            await init_db()
            stats = await get_stats()

            total = stats["total"]
            ngo_count = stats["ngo_count"]
            new_24h = stats["new_24h"]
            sources = stats["sources"]
            top_companies = stats["top_companies"]

            # Build source bars
            source_lines = []
            if sources:
                max_count = max(sources.values()) if sources else 1
                for src, count in sorted(sources.items(), key=lambda x: -x[1]):
                    bar_len = max(1, int(count / max_count * 8))
                    bar = "█" * bar_len
                    source_lines.append(f"`{src:<16s}` {bar} {count}")

            # Build top companies
            company_lines = []
            if top_companies:
                for company, count in top_companies[:5]:
                    name = company if len(company) <= 25 else company[:22] + "..."
                    company_lines.append(f"`{name:<25s}` {count}")

            # Timing
            last_scan_str = self._format_time_ago(self._last_scan_time)
            next_scan_str = self._format_time_until(self._next_scan_time)

            # Build embed
            embed = discord.Embed(
                title="📊 Job Tracker Stats",
                color=0x3498DB,
            )
            embed.add_field(
                name="Overview",
                value=(
                    f"📦 Total tracked: **{total:,}**\n"
                    f"🆕 Last 24 hours: **{new_24h}**\n"
                    f"🟢 NGO jobs: **{ngo_count}**\n"
                    f"🔵 General: **{total - ngo_count}**"
                ),
                inline=False,
            )

            if source_lines:
                embed.add_field(
                    name="📡 Sources (all time)",
                    value="\n".join(source_lines),
                    inline=False,
                )

            if company_lines:
                embed.add_field(
                    name="🏆 Top companies",
                    value="\n".join(company_lines),
                    inline=False,
                )

            embed.add_field(
                name="Timing",
                value=f"🕐 Last scan: {last_scan_str}\n🔄 Next scan: {next_scan_str}",
                inline=False,
            )

            embed.set_footer(text="Job Tracker Bot")
            embed.timestamp = datetime.now(timezone.utc)

            await message.channel.send(embed=embed)

        except Exception as exc:
            logger.exception("Stats command failed")
            await message.channel.send(f"❌ Stats failed: {exc}")

    async def _handle_help(self, message: discord.Message) -> None:
        """Show available commands."""
        embed = discord.Embed(
            title="🤖 Job Tracker Bot — Commands",
            color=0x2ECC71,
        )
        embed.add_field(
            name="Available Commands",
            value=(
                "`r` / `refresh` / `scan` — Trigger an immediate scan\n"
                "`stats` — Show database statistics\n"
                "`help` — Show this help message"
            ),
            inline=False,
        )
        embed.set_footer(text="Job Tracker Bot")
        await message.channel.send(embed=embed)

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _format_time_ago(dt: datetime | None) -> str:
        if dt is None:
            return "never"
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        minutes = int(delta.total_seconds() // 60)
        if minutes < 1:
            return "just now"
        if minutes < 60:
            return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        days = hours // 24
        return f"{days} day{'s' if days != 1 else ''} ago"

    @staticmethod
    def _format_time_until(dt: datetime | None) -> str:
        if dt is None:
            return "unknown"
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = dt - datetime.now(timezone.utc)
        minutes = int(delta.total_seconds() // 60)
        if minutes <= 0:
            return "any moment"
        if minutes < 60:
            return f"in {minutes} minute{'s' if minutes != 1 else ''}"
        hours = minutes // 60
        return f"in {hours} hour{'s' if hours != 1 else ''}"
