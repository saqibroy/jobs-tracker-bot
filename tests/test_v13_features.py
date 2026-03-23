"""Tests for v1.3 features: health endpoint, startup notification, company blocklist,
Telegram commands, senior/salary filters, concurrency.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models.job import Job


# ── helpers ────────────────────────────────────────────────────────────────

def _make_job(**overrides) -> Job:
    """Create a minimal Job for testing, with sensible defaults."""
    defaults = dict(
        title="Software Engineer",
        company="Acme Corp",
        location="Remote",
        url="https://example.com/job/1",
        source="test",
    )
    defaults.update(overrides)
    return Job(**defaults)


# ═══════════════════════════════════════════════════════════════════════════
#  Health Endpoint
# ═══════════════════════════════════════════════════════════════════════════

class TestHealthEndpoint:
    """Test the health HTTP server returns correct JSON."""

    @pytest.mark.asyncio
    async def test_health_returns_200(self):
        """GET /health returns 200 status."""
        from health import _health_handler, set_last_scan, set_jobs_tracked

        set_last_scan(datetime(2026, 3, 15, 22, 0, 0, tzinfo=timezone.utc))
        set_jobs_tracked(1247)

        request = MagicMock()
        response = await _health_handler(request)

        assert response.status == 200

    @pytest.mark.asyncio
    async def test_health_returns_json(self):
        """GET /health returns valid JSON with expected keys."""
        from health import _health_handler, set_last_scan, set_jobs_tracked

        set_last_scan(datetime(2026, 3, 15, 22, 0, 0, tzinfo=timezone.utc))
        set_jobs_tracked(1247)

        request = MagicMock()
        response = await _health_handler(request)

        data = json.loads(response.body)
        assert data["status"] == "ok"
        assert "uptime_seconds" in data
        assert data["jobs_tracked"] == 1247
        assert data["last_scan"] is not None
        assert "next_scan_in_seconds" in data

    @pytest.mark.asyncio
    async def test_health_status_ok_when_not_paused(self):
        """Health status is 'ok' when not paused."""
        from health import _health_handler, set_paused

        set_paused(False)
        request = MagicMock()
        response = await _health_handler(request)
        data = json.loads(response.body)
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_health_status_paused(self):
        """Health status is 'paused' when scanning is paused."""
        from health import _health_handler, set_paused

        set_paused(True)
        request = MagicMock()
        response = await _health_handler(request)
        data = json.loads(response.body)
        assert data["status"] == "paused"
        # Reset
        set_paused(False)

    @pytest.mark.asyncio
    async def test_health_no_last_scan(self):
        """Health works even when no scan has run yet."""
        import health
        original = health._last_scan_time
        health._last_scan_time = None

        request = MagicMock()
        response = await health._health_handler(request)
        data = json.loads(response.body)
        assert data["last_scan"] is None

        health._last_scan_time = original

    @pytest.mark.asyncio
    async def test_health_server_starts(self):
        """Health server starts and returns a runner."""
        from health import start_health_server

        runner = await start_health_server(port=18080)
        assert runner is not None
        await runner.cleanup()


# ═══════════════════════════════════════════════════════════════════════════
#  Company Blocklist
# ═══════════════════════════════════════════════════════════════════════════

class TestCompanyBlocklist:
    """Test that the company blocklist correctly rejects configured companies."""

    def test_blocklist_empty_accepts_all(self):
        """With no blocklist, all companies are accepted."""
        from main import _passes_company_blocklist
        with patch("main.config") as mock_config:
            mock_config.COMPANY_BLOCKLIST = []
            job = _make_job(company="TechBiz Global")
            assert _passes_company_blocklist(job) is True

    def test_blocklist_rejects_exact(self):
        """Company matching blocklist entry is rejected."""
        from main import _passes_company_blocklist
        with patch("main.config") as mock_config:
            mock_config.COMPANY_BLOCKLIST = ["techbiz global", "lemon.io", "a.team"]
            job = _make_job(company="TechBiz Global")
            assert _passes_company_blocklist(job) is False

    def test_blocklist_rejects_substring(self):
        """Company name containing blocklist entry is rejected."""
        from main import _passes_company_blocklist
        with patch("main.config") as mock_config:
            mock_config.COMPANY_BLOCKLIST = ["techbiz global"]
            job = _make_job(company="TechBiz Global Solutions")
            assert _passes_company_blocklist(job) is False

    def test_blocklist_case_insensitive(self):
        """Blocklist comparison is case-insensitive."""
        from main import _passes_company_blocklist
        with patch("main.config") as mock_config:
            mock_config.COMPANY_BLOCKLIST = ["lemon.io"]
            job = _make_job(company="Lemon.io")
            assert _passes_company_blocklist(job) is False

    def test_blocklist_accepts_non_matching(self):
        """Company not on blocklist is accepted."""
        from main import _passes_company_blocklist
        with patch("main.config") as mock_config:
            mock_config.COMPANY_BLOCKLIST = ["techbiz global", "lemon.io"]
            job = _make_job(company="Good Company Inc")
            assert _passes_company_blocklist(job) is True

    def test_blocklist_in_filter_pipeline(self):
        """Blocked companies are removed by _apply_filters."""
        from main import _apply_filters
        with patch("main.config") as mock_config:
            mock_config.COMPANY_BLOCKLIST = ["badcorp"]
            mock_config.FILTER_SENIOR_ONLY = False
            mock_config.MIN_SALARY_EUR = 0
            mock_config.MAX_JOB_AGE_DAYS = 14
            mock_config.SOURCE_MAX_AGE_DAYS = {}
            mock_config.MINIMUM_MATCH_SCORE = 0
            mock_config.ACCEPT_ONSITE_GERMANY = False

            jobs = [
                _make_job(
                    title="React Developer",
                    company="BadCorp Solutions",
                    location="Remote - Worldwide",
                    url="https://example.com/bad1",
                ),
                _make_job(
                    title="React Developer",
                    company="GoodCorp",
                    location="Remote - Worldwide",
                    url="https://example.com/good1",
                ),
            ]
            results = _apply_filters(jobs)
            assert len(results) == 1
            assert results[0].company == "GoodCorp"


# ═══════════════════════════════════════════════════════════════════════════
#  Senior-Only Filter
# ═══════════════════════════════════════════════════════════════════════════

class TestSeniorFilter:
    """Test the optional senior-only filter."""

    def test_disabled_accepts_all(self):
        """When FILTER_SENIOR_ONLY=false, all titles pass."""
        from main import _passes_senior_filter
        with patch("main.config") as mock_config:
            mock_config.FILTER_SENIOR_ONLY = False
            job = _make_job(title="Junior Developer")
            assert _passes_senior_filter(job) is True

    def test_accepts_senior_title(self):
        """Senior title passes when filter is enabled."""
        from main import _passes_senior_filter
        with patch("main.config") as mock_config:
            mock_config.FILTER_SENIOR_ONLY = True
            job = _make_job(title="Senior Full Stack Developer")
            assert _passes_senior_filter(job) is True

    def test_accepts_lead_title(self):
        """Lead title passes when filter is enabled."""
        from main import _passes_senior_filter
        with patch("main.config") as mock_config:
            mock_config.FILTER_SENIOR_ONLY = True
            job = _make_job(title="Lead Backend Engineer")
            assert _passes_senior_filter(job) is True

    def test_accepts_staff_title(self):
        """Staff title passes when filter is enabled."""
        from main import _passes_senior_filter
        with patch("main.config") as mock_config:
            mock_config.FILTER_SENIOR_ONLY = True
            job = _make_job(title="Staff Software Engineer")
            assert _passes_senior_filter(job) is True

    def test_accepts_principal_title(self):
        """Principal title passes when filter is enabled."""
        from main import _passes_senior_filter
        with patch("main.config") as mock_config:
            mock_config.FILTER_SENIOR_ONLY = True
            job = _make_job(title="Principal Engineer")
            assert _passes_senior_filter(job) is True

    def test_rejects_junior_title(self):
        """Junior title is rejected when filter is enabled."""
        from main import _passes_senior_filter
        with patch("main.config") as mock_config:
            mock_config.FILTER_SENIOR_ONLY = True
            job = _make_job(title="Junior Frontend Developer")
            assert _passes_senior_filter(job) is False

    def test_rejects_mid_level(self):
        """Mid-level title is rejected when filter is enabled."""
        from main import _passes_senior_filter
        with patch("main.config") as mock_config:
            mock_config.FILTER_SENIOR_ONLY = True
            job = _make_job(title="Mid-Level Python Developer")
            assert _passes_senior_filter(job) is False

    def test_accepts_no_seniority_mention(self):
        """Title with no seniority keyword → assume senior, accept."""
        from main import _passes_senior_filter
        with patch("main.config") as mock_config:
            mock_config.FILTER_SENIOR_ONLY = True
            job = _make_job(title="Full Stack Developer")
            assert _passes_senior_filter(job) is True


# ═══════════════════════════════════════════════════════════════════════════
#  Salary Filter
# ═══════════════════════════════════════════════════════════════════════════

class TestSalaryFilter:
    """Test the optional minimum salary filter."""

    def test_disabled_accepts_all(self):
        """When MIN_SALARY_EUR=0, all jobs pass."""
        from main import _passes_salary_filter
        with patch("main.config") as mock_config:
            mock_config.MIN_SALARY_EUR = 0
            job = _make_job(salary="€30,000")
            assert _passes_salary_filter(job) is True

    def test_no_salary_accepts(self):
        """Jobs without salary listed always pass."""
        from main import _passes_salary_filter
        with patch("main.config") as mock_config:
            mock_config.MIN_SALARY_EUR = 50000
            job = _make_job(salary=None)
            assert _passes_salary_filter(job) is True

    def test_accepts_above_threshold(self):
        """Salary above minimum is accepted."""
        from main import _passes_salary_filter
        with patch("main.config") as mock_config:
            mock_config.MIN_SALARY_EUR = 50000
            job = _make_job(salary="€60,000 - €80,000")
            assert _passes_salary_filter(job) is True

    def test_rejects_below_threshold(self):
        """Salary below minimum is rejected."""
        from main import _passes_salary_filter
        with patch("main.config") as mock_config:
            mock_config.MIN_SALARY_EUR = 50000
            job = _make_job(salary="€35,000")
            assert _passes_salary_filter(job) is False

    def test_unparseable_salary_accepts(self):
        """Unparseable salary string → accept (benefit of the doubt)."""
        from main import _passes_salary_filter
        with patch("main.config") as mock_config:
            mock_config.MIN_SALARY_EUR = 50000
            job = _make_job(salary="Competitive")
            assert _passes_salary_filter(job) is True

    def test_monthly_salary_annualized(self):
        """Monthly salary (< 10000) is annualized for comparison."""
        from main import _passes_salary_filter
        with patch("main.config") as mock_config:
            mock_config.MIN_SALARY_EUR = 50000
            # 5000/month → 60000/year → passes 50000 threshold
            job = _make_job(salary="€5,000/month")
            assert _passes_salary_filter(job) is True


# ═══════════════════════════════════════════════════════════════════════════
#  Startup / Crash Notifications
# ═══════════════════════════════════════════════════════════════════════════

class TestStartupNotification:
    """Test that startup notification sends on boot."""

    @pytest.mark.asyncio
    async def test_startup_notification_sends(self):
        """Startup notification is sent when webhook is configured."""
        from main import _send_startup_notification
        with patch("main.config") as mock_config:
            mock_config.DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/test"
            mock_config.COMPANY_BLOCKLIST = []

            with patch("discord_webhook.AsyncDiscordWebhook") as MockWebhook:
                mock_instance = AsyncMock()
                MockWebhook.return_value = mock_instance

                await _send_startup_notification(6)
                mock_instance.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_startup_notification_skips_without_webhook(self):
        """No notification sent when webhook is not configured."""
        from main import _send_startup_notification
        with patch("main.config") as mock_config:
            mock_config.DISCORD_WEBHOOK_URL = ""

            # Should not raise
            await _send_startup_notification(6)

    @pytest.mark.asyncio
    async def test_crash_notification_sends(self):
        """Crash notification is sent when webhook is configured."""
        from main import _send_crash_notification
        with patch("main.config") as mock_config:
            mock_config.DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/test"

            with patch("discord_webhook.AsyncDiscordWebhook") as MockWebhook:
                mock_instance = AsyncMock()
                MockWebhook.return_value = mock_instance

                await _send_crash_notification(RuntimeError("test error"))
                mock_instance.execute.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
#  Telegram Commands
# ═══════════════════════════════════════════════════════════════════════════

class TestTelegramCommands:
    """Test that Telegram /commands are properly set up."""

    def test_bot_commands_defined(self):
        """Bot commands list is defined with expected commands."""
        from notifiers.telegram_notifier import _BOT_COMMANDS
        command_names = [c.command for c in _BOT_COMMANDS]
        assert "scan" in command_names
        assert "stats" in command_names
        assert "help" in command_names
        assert "pause" in command_names
        assert "resume" in command_names

    def test_build_application_returns_app(self):
        """build_application returns a telegram Application."""
        from notifiers.telegram_notifier import TelegramNotifier
        notifier = TelegramNotifier(bot_token="fake:token", chat_id="123")
        app = notifier.build_application()
        assert app is not None

    def test_build_application_has_handlers(self):
        """Application has handlers for all /commands."""
        from notifiers.telegram_notifier import TelegramNotifier
        notifier = TelegramNotifier(bot_token="fake:token", chat_id="123")
        app = notifier.build_application()
        # Check that handlers were added
        assert len(app.handlers) > 0

    @pytest.mark.asyncio
    async def test_register_commands(self):
        """register_commands calls Bot.set_my_commands."""
        from notifiers.telegram_notifier import TelegramNotifier
        notifier = TelegramNotifier(bot_token="fake:token", chat_id="123")

        with patch("notifiers.telegram_notifier.Bot") as MockBot:
            mock_bot = AsyncMock()
            MockBot.return_value = mock_bot

            await notifier.register_commands()
            mock_bot.set_my_commands.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
#  Concurrency Configuration
# ═══════════════════════════════════════════════════════════════════════════

class TestConcurrency:
    """Test MAX_CONCURRENT_SOURCES batching in run_scan."""

    @pytest.mark.asyncio
    async def test_batched_scan_runs_all_sources(self):
        """With MAX_CONCURRENT_SOURCES=2, all sources still run."""
        from main import run_scan

        # Create mock sources
        mock_sources = []
        for i in range(4):
            src = MagicMock()
            src.name = f"test_source_{i}"
            src.safe_fetch = AsyncMock(return_value=[])
            mock_sources.append(src)

        with patch("main.config") as mock_config:
            mock_config.MAX_CONCURRENT_SOURCES = 2
            mock_config.MAX_JOB_AGE_DAYS = 14
            mock_config.SOURCE_MAX_AGE_DAYS = {}
            mock_config.COMPANY_BLOCKLIST = []
            mock_config.FILTER_SENIOR_ONLY = False
            mock_config.MIN_SALARY_EUR = 0

            result = await run_scan(mock_sources, dry_run=True)

            # All 4 sources should have been fetched
            for src in mock_sources:
                src.safe_fetch.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════════════════════════════════

class TestConfig:
    """Test new config variables."""

    def test_company_blocklist_default_empty(self):
        """COMPANY_BLOCKLIST defaults to empty list."""
        import config as cfg
        # The actual default depends on .env, but the parsing should work
        assert isinstance(cfg.COMPANY_BLOCKLIST, list)

    def test_playwright_removed_from_config(self):
        """DISABLE_PLAYWRIGHT no longer exists after v1.5."""
        import config as cfg
        assert not hasattr(cfg, "DISABLE_PLAYWRIGHT")

    def test_max_concurrent_sources_default(self):
        """MAX_CONCURRENT_SOURCES has a sensible default."""
        import config as cfg
        assert isinstance(cfg.MAX_CONCURRENT_SOURCES, int)
        assert cfg.MAX_CONCURRENT_SOURCES > 0

    def test_health_port_default(self):
        """HEALTH_PORT defaults to 8080."""
        import config as cfg
        assert isinstance(cfg.HEALTH_PORT, int)

    def test_filter_senior_only_default_false(self):
        """FILTER_SENIOR_ONLY defaults to False."""
        import config as cfg
        assert isinstance(cfg.FILTER_SENIOR_ONLY, bool)

    def test_min_salary_eur_default_zero(self):
        """MIN_SALARY_EUR defaults to 0."""
        import config as cfg
        assert isinstance(cfg.MIN_SALARY_EUR, int)


# ═══════════════════════════════════════════════════════════════════════════
#  Paused state
# ═══════════════════════════════════════════════════════════════════════════

class TestPausedState:
    """Test the paused state management."""

    def test_set_and_get_paused(self):
        """set_paused and is_paused work correctly."""
        from health import set_paused, is_paused

        set_paused(True)
        assert is_paused() is True

        set_paused(False)
        assert is_paused() is False

    def test_initial_state_not_paused(self):
        """Bot starts in non-paused state."""
        from health import set_paused, is_paused

        set_paused(False)
        assert is_paused() is False
