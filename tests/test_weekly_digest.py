"""Tests for the weekly NGO digest feature and techjobsforgood Cloudflare handling.

Covers:
  - Weekly digest: DB queries, Discord embed, empty state, CLI flag
  - TechJobsForGood: Cloudflare WAF detection, fallback URL, enhanced headers
  - Config: WEEKLY_DIGEST_ENABLED, WEEKLY_DIGEST_DAY, WEEKLY_DIGEST_HOUR
  - Scheduler: CronTrigger job registration
  - match_score: DB schema migration, persistence
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models.job import Job


# ═══════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _make_job(**overrides) -> Job:
    """Create a minimal Job for testing."""
    defaults = dict(
        title="Software Engineer",
        company="Acme Corp",
        location="Remote - Worldwide",
        url="https://example.com/job/1",
        source="remotive",
        is_remote=True,
    )
    defaults.update(overrides)
    return Job(**defaults)


@pytest_asyncio.fixture
async def tmp_db(tmp_path, monkeypatch):
    """Set up a temporary database for testing."""
    from storage.database import init_db

    db_path = str(tmp_path / "test_jobs.db")
    monkeypatch.setattr("storage.database.config.DATABASE_PATH", db_path)
    await init_db()
    return db_path


# ═══════════════════════════════════════════════════════════════════════════
#  Weekly digest — DB queries
# ═══════════════════════════════════════════════════════════════════════════

class TestWeeklyNgoJobsQuery:
    """Test get_weekly_ngo_jobs() returns correct NGO jobs."""

    @pytest.mark.asyncio
    async def test_returns_only_ngo_jobs(self, tmp_db):
        """Only NGO jobs are returned, not general."""
        from storage.database import save_jobs, get_weekly_ngo_jobs

        ngo_job = _make_job(title="NGO Dev", company="UNICEF", is_ngo=True, match_score=80)
        general_job = _make_job(title="General Dev", company="Corp Inc", url="https://example.com/2", is_ngo=False)

        await save_jobs([ngo_job, general_job])
        results = await get_weekly_ngo_jobs(days=7)

        titles = [r["title"] for r in results]
        assert "NGO Dev" in titles
        assert "General Dev" not in titles

    @pytest.mark.asyncio
    async def test_sorted_by_match_score(self, tmp_db):
        """Results are sorted by match_score descending."""
        from storage.database import save_jobs, get_weekly_ngo_jobs

        low = _make_job(title="Low Match", company="NGO A", url="https://a.com/1", is_ngo=True, match_score=30)
        high = _make_job(title="High Match", company="NGO B", url="https://b.com/2", is_ngo=True, match_score=90)
        mid = _make_job(title="Mid Match", company="NGO C", url="https://c.com/3", is_ngo=True, match_score=60)

        await save_jobs([low, high, mid])
        results = await get_weekly_ngo_jobs(days=7)

        scores = [r["match_score"] for r in results]
        assert scores == sorted(scores, reverse=True)
        assert results[0]["title"] == "High Match"

    @pytest.mark.asyncio
    async def test_respects_limit(self, tmp_db):
        """Only up to `limit` jobs are returned."""
        from storage.database import save_jobs, get_weekly_ngo_jobs

        jobs = [
            _make_job(
                title=f"Job {i}",
                company=f"NGO {i}",
                url=f"https://example.com/{i}",
                is_ngo=True,
                match_score=50,
            )
            for i in range(10)
        ]
        await save_jobs(jobs)
        results = await get_weekly_ngo_jobs(days=7, limit=3)
        assert len(results) <= 3

    @pytest.mark.asyncio
    async def test_empty_when_no_ngo_jobs(self, tmp_db):
        """Returns empty list when no NGO jobs exist."""
        from storage.database import get_weekly_ngo_jobs

        results = await get_weekly_ngo_jobs(days=7)
        assert results == []

    @pytest.mark.asyncio
    async def test_returns_dict_format(self, tmp_db):
        """Each result is a dict with expected keys."""
        from storage.database import save_jobs, get_weekly_ngo_jobs

        job = _make_job(title="Test Job", company="NGO X", is_ngo=True, match_score=75)
        await save_jobs([job])
        results = await get_weekly_ngo_jobs(days=7)

        assert len(results) == 1
        row = results[0]
        assert isinstance(row, dict)
        assert "title" in row
        assert "company" in row
        assert "match_score" in row
        assert "source" in row
        assert "url" in row


class TestWeeklyGeneralCount:
    """Test get_weekly_general_count() returns correct count."""

    @pytest.mark.asyncio
    async def test_counts_only_non_ngo(self, tmp_db):
        """Only non-NGO jobs are counted."""
        from storage.database import save_jobs, get_weekly_general_count

        ngo = _make_job(title="NGO Dev", is_ngo=True, url="https://a.com/1")
        gen1 = _make_job(title="Gen 1", is_ngo=False, url="https://b.com/2")
        gen2 = _make_job(title="Gen 2", is_ngo=False, url="https://c.com/3")

        await save_jobs([ngo, gen1, gen2])
        count = await get_weekly_general_count(days=7)
        assert count == 2

    @pytest.mark.asyncio
    async def test_zero_when_empty(self, tmp_db):
        """Returns 0 when no jobs exist."""
        from storage.database import get_weekly_general_count

        count = await get_weekly_general_count(days=7)
        assert count == 0


# ═══════════════════════════════════════════════════════════════════════════
#  Weekly digest — match_score persistence
# ═══════════════════════════════════════════════════════════════════════════

class TestMatchScorePersistence:
    """Test that match_score is saved to and retrieved from the DB."""

    @pytest.mark.asyncio
    async def test_match_score_saved(self, tmp_db):
        """match_score value survives save → query round-trip."""
        from storage.database import save_jobs, get_weekly_ngo_jobs

        job = _make_job(title="Scored Job", company="NGO Y", is_ngo=True, match_score=88)
        await save_jobs([job])

        results = await get_weekly_ngo_jobs(days=7)
        assert len(results) == 1
        assert results[0]["match_score"] == 88

    @pytest.mark.asyncio
    async def test_match_score_default_zero(self, tmp_db):
        """match_score defaults to 0 when not set."""
        from storage.database import save_jobs, get_weekly_ngo_jobs

        job = _make_job(title="Unscored Job", company="NGO Z", is_ngo=True)
        await save_jobs([job])

        results = await get_weekly_ngo_jobs(days=7)
        assert len(results) == 1
        assert results[0]["match_score"] == 0


# ═══════════════════════════════════════════════════════════════════════════
#  Weekly digest — send function
# ═══════════════════════════════════════════════════════════════════════════

class TestSendWeeklyNgoDigest:
    """Test send_weekly_ngo_digest() Discord embed creation."""

    @pytest.mark.asyncio
    async def test_sends_when_jobs_exist(self, tmp_db, monkeypatch):
        """Digest sends a Discord embed when NGO jobs are available."""
        from main import send_weekly_ngo_digest
        from storage.database import save_jobs

        jobs = [
            _make_job(title=f"NGO Job {i}", company=f"Org {i}",
                      url=f"https://example.com/{i}", is_ngo=True, match_score=70)
            for i in range(5)
        ]
        await save_jobs(jobs)

        with patch("main.config") as mock_config:
            mock_config.DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/test"
            mock_config.DISCORD_WEBHOOK_URL_NGO = ""

            with patch("discord_webhook.AsyncDiscordWebhook") as MockWebhook:
                mock_instance = AsyncMock()
                MockWebhook.return_value = mock_instance

                await send_weekly_ngo_digest()
                mock_instance.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_sends_empty_state(self, tmp_db, monkeypatch):
        """Digest sends a 'no jobs' embed when no NGO jobs exist."""
        from main import send_weekly_ngo_digest

        with patch("main.config") as mock_config:
            mock_config.DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/test"
            mock_config.DISCORD_WEBHOOK_URL_NGO = ""

            with patch("discord_webhook.AsyncDiscordWebhook") as MockWebhook:
                mock_instance = AsyncMock()
                MockWebhook.return_value = mock_instance

                await send_weekly_ngo_digest()
                mock_instance.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_prefers_ngo_webhook(self, tmp_db, monkeypatch):
        """When DISCORD_WEBHOOK_URL_NGO is set, it's used for the digest."""
        from main import send_weekly_ngo_digest

        with patch("main.config") as mock_config:
            mock_config.DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/main"
            mock_config.DISCORD_WEBHOOK_URL_NGO = "https://discord.com/api/webhooks/ngo"

            with patch("discord_webhook.AsyncDiscordWebhook") as MockWebhook:
                mock_instance = AsyncMock()
                MockWebhook.return_value = mock_instance

                await send_weekly_ngo_digest()
                # Verify the NGO webhook URL was used
                call_args = MockWebhook.call_args
                assert call_args is not None
                assert call_args[1]["url"] == "https://discord.com/api/webhooks/ngo"

    @pytest.mark.asyncio
    async def test_skips_without_webhook(self, tmp_db, monkeypatch):
        """No error when no webhook is configured."""
        from main import send_weekly_ngo_digest

        with patch("main.config") as mock_config:
            mock_config.DISCORD_WEBHOOK_URL = ""
            mock_config.DISCORD_WEBHOOK_URL_NGO = ""

            # Should not raise
            await send_weekly_ngo_digest()

    @pytest.mark.asyncio
    async def test_handles_exception_gracefully(self, tmp_db, monkeypatch):
        """Digest catches exceptions and logs instead of crashing."""
        from main import send_weekly_ngo_digest

        with patch("main.get_weekly_ngo_jobs", new_callable=AsyncMock, side_effect=Exception("DB error")):
            # Should not raise
            await send_weekly_ngo_digest()


# ═══════════════════════════════════════════════════════════════════════════
#  Weekly digest — CLI flag
# ═══════════════════════════════════════════════════════════════════════════

class TestWeeklyDigestCli:
    """Test --weekly-digest CLI argument."""

    def test_weekly_digest_arg_in_parser(self):
        """The --weekly-digest flag is registered in argparse."""
        import argparse
        from main import main as main_func

        # Extract the parser by checking the function's source
        with patch("argparse.ArgumentParser.parse_args") as mock_parse:
            mock_parse.return_value = argparse.Namespace(
                dry_run=False, source=None, max_age=None, verbose=False,
                stats=False, weekly_digest=True,
            )
            with patch("main.asyncio.run") as mock_run:
                with patch("main.logger"):
                    try:
                        main_func()
                    except SystemExit:
                        pass

            # asyncio.run should have been called (for _run_weekly_digest_cli)
            mock_run.assert_called_once()

    def test_run_weekly_digest_cli_calls_digest(self):
        """_run_weekly_digest_cli initialises DB and sends digest."""
        import asyncio
        from main import _run_weekly_digest_cli

        with patch("main.init_db", new_callable=AsyncMock) as mock_init:
            with patch("main.send_weekly_ngo_digest", new_callable=AsyncMock) as mock_digest:
                asyncio.get_event_loop().run_until_complete(_run_weekly_digest_cli())
                mock_init.assert_called_once()
                mock_digest.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
#  Weekly digest — config
# ═══════════════════════════════════════════════════════════════════════════

class TestWeeklyDigestConfig:
    """Test weekly digest configuration variables."""

    def test_config_defaults(self):
        """Default config values are sensible."""
        import config
        assert hasattr(config, "WEEKLY_DIGEST_ENABLED")
        assert hasattr(config, "WEEKLY_DIGEST_DAY")
        assert hasattr(config, "WEEKLY_DIGEST_HOUR")
        assert isinstance(config.WEEKLY_DIGEST_ENABLED, bool)
        assert isinstance(config.WEEKLY_DIGEST_DAY, str)
        assert isinstance(config.WEEKLY_DIGEST_HOUR, int)

    def test_default_day_is_monday(self):
        """Default digest day is Monday."""
        import config
        assert config.WEEKLY_DIGEST_DAY == "mon"

    def test_default_hour_is_8(self):
        """Default digest hour is 8 (AM UTC)."""
        import config
        assert config.WEEKLY_DIGEST_HOUR == 8

    def test_default_enabled(self):
        """Weekly digest is enabled by default."""
        import config
        assert config.WEEKLY_DIGEST_ENABLED is True


# ═══════════════════════════════════════════════════════════════════════════
#  Weekly digest — scheduler registration
# ═══════════════════════════════════════════════════════════════════════════

class TestWeeklyDigestScheduler:
    """Test that the CronTrigger job is configured correctly."""

    def test_cron_trigger_import(self):
        """CronTrigger is importable from APScheduler."""
        from apscheduler.triggers.cron import CronTrigger
        trigger = CronTrigger(day_of_week="mon", hour=8, minute=0)
        assert trigger is not None

    def test_send_weekly_ngo_digest_is_importable(self):
        """The send function is importable from main."""
        from main import send_weekly_ngo_digest
        assert callable(send_weekly_ngo_digest)

    def test_run_weekly_digest_cli_is_importable(self):
        """The CLI wrapper is importable from main."""
        from main import _run_weekly_digest_cli
        assert callable(_run_weekly_digest_cli)


# ═══════════════════════════════════════════════════════════════════════════
#  TechJobsForGood — Cloudflare detection
# ═══════════════════════════════════════════════════════════════════════════

class TestTechJobsForGoodCloudflare:
    """Test Cloudflare WAF detection and graceful degradation."""

    def setup_method(self):
        from sources.techjobsforgood import TechJobsForGoodSource
        self.source = TechJobsForGoodSource()

    @pytest.mark.asyncio
    async def test_detects_cloudflare_blocked(self):
        """Returns [] when 'you have been blocked' is in the response."""
        blocked_html = (
            "<html><body><h1>Attention Required!</h1>"
            "<p>You have been blocked by Cloudflare.</p>"
            "<p>Cloudflare Ray ID: abc123</p>"
            "</body></html>"
        )
        # Make it long enough to pass the 500-char check
        blocked_html += " " * 500
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, return_value=blocked_html):
            jobs = await self.source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_detects_cf_error_details(self):
        """Returns [] when cf-error-details class is in the response."""
        cf_html = (
            '<html><body><div class="cf-error-details">'
            "<h1>Access denied</h1>"
            "<p>This website is using a security service.</p>"
            "</div></body></html>"
        )
        cf_html += " " * 500
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, return_value=cf_html):
            jobs = await self.source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_detects_attention_required(self):
        """Returns [] when 'attention required' is in response."""
        html = (
            "<html><head><title>Attention Required! | Cloudflare</title></head>"
            "<body><h1>Attention Required!</h1>"
            "<p>Please enable cookies and reload.</p>"
            "</body></html>"
        )
        html += " " * 500
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, return_value=html):
            jobs = await self.source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_detects_cloudflare_ray_id(self):
        """Returns [] when 'cloudflare ray id' is in the response."""
        html = (
            "<html><body><h1>Error 1020</h1>"
            "<p>Access denied.</p>"
            "<p>Cloudflare Ray ID: 8x9y10z</p>"
            "</body></html>"
        )
        html += " " * 500
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, return_value=html):
            jobs = await self.source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_short_response_returns_empty(self):
        """Very short response (< 500 chars) returns []."""
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, return_value="ok"):
            jobs = await self.source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_empty_response_returns_empty(self):
        """Empty string response returns []."""
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, return_value=""):
            jobs = await self.source.fetch()
        assert jobs == []


class TestTechJobsForGoodFetchHtml:
    """Test the _fetch_html method with fallback URL logic."""

    def setup_method(self):
        from sources.techjobsforgood import TechJobsForGoodSource
        self.source = TechJobsForGoodSource()

    @pytest.mark.asyncio
    async def test_tries_fallback_url_on_403(self):
        """When main URL returns 403, fallback URL is tried."""
        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            if call_count == 1:
                resp.status_code = 403
                resp.text = ""
            else:
                resp.status_code = 200
                resp.text = "<html><body>OK</body></html>"
            return resp

        with patch.object(self.source, "_get", side_effect=mock_get):
            html = await self.source._fetch_html()

        assert call_count == 2
        assert "OK" in html

    @pytest.mark.asyncio
    async def test_returns_empty_when_all_fail(self):
        """Returns empty string when all URLs return 403."""
        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 403
            resp.text = ""
            return resp

        with patch.object(self.source, "_get", side_effect=mock_get):
            html = await self.source._fetch_html()

        assert html == ""

    @pytest.mark.asyncio
    async def test_returns_first_success(self):
        """Returns HTML from first successful response."""
        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.text = "<html>First URL</html>"
            return resp

        with patch.object(self.source, "_get", side_effect=mock_get):
            html = await self.source._fetch_html()

        assert "First URL" in html

    @pytest.mark.asyncio
    async def test_handles_429_rate_limit(self):
        """Returns empty string on 429 rate limit."""
        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 429
            resp.text = ""
            return resp

        with patch.object(self.source, "_get", side_effect=mock_get):
            html = await self.source._fetch_html()

        assert html == ""

    @pytest.mark.asyncio
    async def test_handles_network_exception(self):
        """Returns empty string when network exception occurs."""
        async def mock_get(url, **kwargs):
            raise ConnectionError("Network unreachable")

        with patch.object(self.source, "_get", side_effect=mock_get):
            html = await self.source._fetch_html()

        assert html == ""


class TestTechJobsForGoodHeaders:
    """Test that enhanced headers are configured correctly."""

    def test_headers_include_sec_fetch(self):
        """Headers include Sec-Fetch-* headers for Cloudflare bypass."""
        from sources.techjobsforgood import _HEADERS

        assert "Sec-Fetch-Dest" in _HEADERS
        assert "Sec-Fetch-Mode" in _HEADERS
        assert "Sec-Fetch-Site" in _HEADERS
        assert _HEADERS["Sec-Fetch-Dest"] == "document"

    def test_headers_include_user_agent(self):
        """Headers include a Chrome-like User-Agent."""
        from sources.techjobsforgood import _HEADERS

        assert "User-Agent" in _HEADERS
        assert "Chrome" in _HEADERS["User-Agent"]

    def test_headers_include_referer(self):
        """Headers include a Referer from the same domain."""
        from sources.techjobsforgood import _HEADERS

        assert "Referer" in _HEADERS
        assert "techjobsforgood" in _HEADERS["Referer"]

    def test_headers_include_dnt(self):
        """Headers include DNT (Do Not Track)."""
        from sources.techjobsforgood import _HEADERS

        assert "DNT" in _HEADERS

    def test_headers_include_accept_encoding(self):
        """Headers include Accept-Encoding with br (Brotli)."""
        from sources.techjobsforgood import _HEADERS

        assert "Accept-Encoding" in _HEADERS
        assert "br" in _HEADERS["Accept-Encoding"]


# ═══════════════════════════════════════════════════════════════════════════
#  DB schema migration — match_score column
# ═══════════════════════════════════════════════════════════════════════════

class TestMatchScoreMigration:
    """Test that match_score column exists after init_db."""

    @pytest.mark.asyncio
    async def test_schema_has_match_score_column(self, tmp_db):
        """The jobs table has a match_score column after init."""
        import aiosqlite

        async with aiosqlite.connect(tmp_db) as db:
            cursor = await db.execute("PRAGMA table_info(jobs)")
            columns = await cursor.fetchall()
            col_names = [c[1] for c in columns]
            assert "match_score" in col_names

    @pytest.mark.asyncio
    async def test_migration_idempotent(self, tmp_db):
        """Running init_db again doesn't error (migration is idempotent)."""
        from storage.database import init_db

        # Second init should not crash
        await init_db()
        await init_db()
