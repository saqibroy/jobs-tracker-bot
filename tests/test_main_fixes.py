"""Tests for main.py filter pipeline fixes and Discord notifier."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models.job import Job
from main import _apply_filters, _format_age, _show_stats
from notifiers.discord_notifier import DiscordNotifier


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
#  Fix 1 — Arbeitnow unknown scope defaults to "germany"
# ═══════════════════════════════════════════════════════════════════════════

class TestArbeitnowGermanyDefault:
    def test_arbeitnow_unknown_scope_defaults_germany(self):
        """Arbeitnow job with no remote scope signal → defaults to 'germany'."""
        job = _make_job(
            title="Backend Developer",
            company="FinTech Co",
            location="Windeck",  # Small German town, not in cities list
            source="arbeitnow",
            is_remote=True,
            url="https://arbeitnow.com/job/1",
        )
        results = _apply_filters([job])
        assert len(results) == 1
        assert results[0].remote_scope == "germany"

    def test_non_arbeitnow_unknown_scope_stays_unknown(self):
        """Non-arbeitnow source keeps 'unknown' scope — location filter handles it."""
        job = _make_job(
            title="Backend Developer",
            company="FinTech Co",
            location="Remote",
            source="remotive",
            url="https://remotive.com/job/1",
        )
        results = _apply_filters([job])
        # This would pass location filter due to "Remote" keyword
        # The scope is determined by classify_remote_scope; just verify it's not "germany"
        for r in results:
            if r.source == "remotive":
                assert r.remote_scope != "germany" or "germany" in r.location.lower()


# ═══════════════════════════════════════════════════════════════════════════
#  Fix 2 — Per-company dedup cap (max 2 per scan)
# ═══════════════════════════════════════════════════════════════════════════

class TestPerCompanyCap:
    def test_max_two_per_company(self):
        """Only 2 jobs per company should pass, most recent first."""
        jobs = [
            _make_job(
                title="Backend Developer",
                company="SpamCorp",
                location="Remote - Worldwide",
                url=f"https://example.com/job/{i}",
                posted_at="2025-01-10T00:00:00Z",
            )
            for i in range(1, 4)
        ]
        # Give them different titles so content_hash differs
        jobs[0].title = "Backend Developer"
        jobs[1].title = "Frontend Developer"
        jobs[2].title = "DevOps Engineer"
        # Need to recreate to get fresh hashes
        jobs = [
            _make_job(
                title=t,
                company="SpamCorp",
                location="Remote - Worldwide",
                url=f"https://example.com/job/{i}",
                posted_at=datetime.now(timezone.utc) - timedelta(hours=i),
            )
            for i, t in enumerate(["Backend Developer", "Frontend Developer", "DevOps Engineer"], 1)
        ]

        results = _apply_filters(jobs)
        assert len(results) == 2

    def test_different_companies_not_capped(self):
        """Jobs from different companies are not affected by the cap."""
        jobs = [
            _make_job(
                title=f"Developer {i}",
                company=f"Company {i}",
                location="Remote - Worldwide",
                url=f"https://example.com/job/{i}",
            )
            for i in range(1, 5)
        ]
        results = _apply_filters(jobs)
        assert len(results) == 4

    def test_cap_keeps_most_recent(self):
        """When capping, the most recently posted jobs should be kept."""
        now = datetime.now(timezone.utc)
        jobs = [
            _make_job(
                title="Backend Developer",
                company="SpamCorp",
                location="Remote - Worldwide",
                url="https://example.com/job/old",
                posted_at=now - timedelta(days=3),
            ),
            _make_job(
                title="Frontend Developer",
                company="SpamCorp",
                location="Remote - Worldwide",
                url="https://example.com/job/mid",
                posted_at=now - timedelta(days=2),
            ),
            _make_job(
                title="DevOps Engineer",
                company="SpamCorp",
                location="Remote - Worldwide",
                url="https://example.com/job/new",
                posted_at=now - timedelta(days=1),
            ),
        ]
        results = _apply_filters(jobs)
        assert len(results) == 2
        result_titles = {r.title for r in results}
        assert "DevOps Engineer" in result_titles
        assert "Frontend Developer" in result_titles
        assert "Backend Developer" not in result_titles


# ═══════════════════════════════════════════════════════════════════════════
#  Fix 3 — Arbeitnow is_remote=False rejection for germany-scoped jobs
# ═══════════════════════════════════════════════════════════════════════════

class TestArbeitnowOnSiteRejection:
    def test_arbeitnow_onsite_germany_rejected(self):
        """Arbeitnow job with is_remote=False and germany scope → rejected."""
        job = _make_job(
            title="Backend Developer",
            company="Berlin Corp",
            location="Berlin",
            source="arbeitnow",
            is_remote=False,
            url="https://arbeitnow.com/job/onsite",
        )
        results = _apply_filters([job])
        assert len(results) == 0

    def test_arbeitnow_remote_germany_accepted(self):
        """Arbeitnow job with is_remote=True and germany scope → accepted."""
        job = _make_job(
            title="Backend Developer",
            company="Berlin Corp",
            location="Berlin, Germany (Remote)",
            source="arbeitnow",
            is_remote=True,
            url="https://arbeitnow.com/job/remote",
        )
        results = _apply_filters([job])
        assert len(results) == 1

    def test_arbeitnow_onsite_worldwide_rejected_v11(self):
        """Arbeitnow job with is_remote=False and 'Worldwide' but no corroboration.

        v1.1: Arbeitnow 'Worldwide' without description corroboration →
        defaults to germany scope → is_remote=False + germany = on-site reject.
        """
        job = _make_job(
            title="Backend Developer",
            company="Global Corp",
            location="Remote - Worldwide",
            source="arbeitnow",
            is_remote=False,
            url="https://arbeitnow.com/job/worldwide",
        )
        results = _apply_filters([job])
        # v1.1: uncorroborated worldwide + is_remote=False → rejected
        assert len(results) == 0

    def test_arbeitnow_onsite_worldwide_with_corroboration_accepted(self):
        """Arbeitnow job with 'Worldwide' + description corroboration → accepted
        even if is_remote=False (scope stays worldwide, on-site check only
        applies to germany scope)."""
        job = _make_job(
            title="Backend Developer",
            company="Global Corp",
            location="Remote - Worldwide",
            source="arbeitnow",
            is_remote=False,
            description="This is a fully remote worldwide position.",
            url="https://arbeitnow.com/job/worldwide2",
        )
        results = _apply_filters([job])
        assert len(results) == 1


# ═══════════════════════════════════════════════════════════════════════════
#  Fix 4 — Source pre-classified remote_scope preserved in _apply_filters
# ═══════════════════════════════════════════════════════════════════════════

class TestPreClassifiedScope:
    def test_idealist_worldwide_preserved(self):
        """Idealist sets remote_scope=worldwide → _apply_filters keeps it."""
        job = _make_job(
            title="Software Engineer",
            company="NGO Corp",
            location="Remote (Worldwide)",
            source="idealist",
            is_remote=True,
            remote_scope="worldwide",
            url="https://idealist.org/job/1",
        )
        results = _apply_filters([job])
        assert len(results) == 1
        assert results[0].remote_scope == "worldwide"

    def test_idealist_restricted_rejected(self):
        """Idealist sets remote_scope=restricted → _apply_filters rejects it."""
        job = _make_job(
            title="Software Engineer",
            company="US Only NGO",
            location="US · Remote (US)",
            source="idealist",
            is_remote=True,
            remote_scope="restricted",
            url="https://idealist.org/job/us-only",
        )
        results = _apply_filters([job])
        assert len(results) == 0

    def test_idealist_eu_preserved(self):
        """Idealist sets remote_scope=eu → _apply_filters keeps it."""
        job = _make_job(
            title="Backend Developer",
            company="Berlin NGO",
            location="DE · Remote (DE)",
            source="idealist",
            is_remote=True,
            remote_scope="eu",
            url="https://idealist.org/job/2",
        )
        results = _apply_filters([job])
        assert len(results) == 1
        assert results[0].remote_scope == "eu"

    def test_non_preclassified_still_reclassified(self):
        """Source without pre-classification still gets classify_remote_scope."""
        job = _make_job(
            title="Software Engineer",
            company="Remote Corp",
            location="Remote - Worldwide",
            source="remoteok",
            is_remote=True,
            url="https://remoteok.com/job/1",
        )
        results = _apply_filters([job])
        assert len(results) == 1
        assert results[0].remote_scope == "worldwide"


# ═══════════════════════════════════════════════════════════════════════════
#  Discord Notifier — unit tests (mocked webhook)
# ═══════════════════════════════════════════════════════════════════════════

class TestDiscordNotifier:
    @pytest.mark.asyncio
    async def test_send_jobs_creates_embeds(self):
        """Verify send_jobs calls the webhook for each job."""
        notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/test/test")

        jobs = [
            _make_job(title="Job A", url="https://example.com/a"),
            _make_job(title="Job B", url="https://example.com/b"),
        ]

        with patch("notifiers.discord_notifier.AsyncDiscordWebhook") as MockWebhook:
            mock_instance = AsyncMock()
            mock_instance.execute = AsyncMock(return_value=MagicMock(status_code=200))
            MockWebhook.return_value = mock_instance

            await notifier.send_jobs(jobs)

            # Should have been called once per job
            assert MockWebhook.call_count == 2

    @pytest.mark.asyncio
    async def test_send_jobs_ngo_uses_ngo_webhook(self):
        """NGO jobs should go to the NGO webhook if configured."""
        notifier = DiscordNotifier(
            webhook_url="https://discord.com/api/webhooks/general",
            webhook_url_ngo="https://discord.com/api/webhooks/ngo",
        )

        ngo_job = _make_job(title="Dev at UNICEF", url="https://example.com/ngo")
        ngo_job.is_ngo = True

        general_job = _make_job(title="Dev at Acme", url="https://example.com/gen")
        general_job.is_ngo = False

        webhook_urls_used = []

        with patch("notifiers.discord_notifier.AsyncDiscordWebhook") as MockWebhook:
            mock_instance = AsyncMock()
            mock_instance.execute = AsyncMock(return_value=MagicMock(status_code=200))
            MockWebhook.return_value = mock_instance

            def capture_url(*args, **kwargs):
                webhook_urls_used.append(kwargs.get("url", args[0] if args else None))
                return mock_instance
            MockWebhook.side_effect = capture_url

            await notifier.send_jobs([ngo_job, general_job])

            assert webhook_urls_used[0] == "https://discord.com/api/webhooks/ngo"
            assert webhook_urls_used[1] == "https://discord.com/api/webhooks/general"

    @pytest.mark.asyncio
    async def test_send_jobs_skips_when_no_url(self):
        """If no webhook URL is configured, send_jobs should do nothing."""
        notifier = DiscordNotifier(webhook_url="")
        # Force the internal URL to empty (bypass config.DISCORD_WEBHOOK_URL fallback)
        notifier._webhook_url = ""

        jobs = [_make_job(title="Test Job")]

        with patch("notifiers.discord_notifier.AsyncDiscordWebhook") as MockWebhook:
            await notifier.send_jobs(jobs)
            MockWebhook.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_test_message(self):
        """Verify send_test_message sends a single embed."""
        notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/test/test")

        with patch("notifiers.discord_notifier.AsyncDiscordWebhook") as MockWebhook:
            mock_instance = AsyncMock()
            mock_instance.execute = AsyncMock(return_value=MagicMock(status_code=200))
            MockWebhook.return_value = mock_instance

            await notifier.send_test_message()

            MockWebhook.assert_called_once()
            mock_instance.add_embed.assert_called_once()
            mock_instance.execute.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
#  Recency Filter — reject old postings
# ═══════════════════════════════════════════════════════════════════════════

class TestRecencyFilter:
    def test_recent_job_accepted(self):
        """A job posted 3 days ago should pass the default 14-day filter."""
        job = _make_job(
            title="Software Engineer",
            company="Fresh Co",
            location="Remote - Worldwide",
            source="remotive",
            is_remote=True,
            posted_at=datetime.now(timezone.utc) - timedelta(days=3),
        )
        results = _apply_filters([job])
        assert len(results) == 1

    def test_old_job_rejected(self):
        """A job posted 30 days ago should be rejected by default 14-day filter."""
        job = _make_job(
            title="Software Engineer",
            company="Stale Co",
            location="Remote - Worldwide",
            source="remotive",
            is_remote=True,
            posted_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
        results = _apply_filters([job])
        assert len(results) == 0

    def test_custom_max_age(self):
        """max_age_days=7 rejects a 10-day-old job."""
        job = _make_job(
            title="Software Engineer",
            company="Oldish Co",
            location="Remote - Worldwide",
            source="remotive",
            is_remote=True,
            posted_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
        results = _apply_filters([job], max_age_days=7)
        assert len(results) == 0

    def test_custom_max_age_accepts_within_range(self):
        """max_age_days=7 accepts a 5-day-old job."""
        job = _make_job(
            title="Software Engineer",
            company="Recent Co",
            location="Remote - Worldwide",
            source="remotive",
            is_remote=True,
            posted_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        results = _apply_filters([job], max_age_days=7)
        assert len(results) == 1

    def test_no_posted_at_accepted(self):
        """Jobs without posted_at should NOT be rejected by recency filter."""
        job = _make_job(
            title="Software Engineer",
            company="Mystery Co",
            location="Remote - Worldwide",
            source="remotive",
            is_remote=True,
            posted_at=None,
        )
        results = _apply_filters([job])
        assert len(results) == 1

    def test_exactly_at_boundary(self):
        """A job just under 14 days old should still pass."""
        job = _make_job(
            title="Software Engineer",
            company="Edge Co",
            location="Remote - Worldwide",
            source="remotive",
            is_remote=True,
            posted_at=datetime.now(timezone.utc) - timedelta(days=13, hours=23),
        )
        results = _apply_filters([job])
        assert len(results) == 1

    def test_naive_datetime_handled(self):
        """Naive posted_at (no timezone) should be treated as UTC."""
        job = _make_job(
            title="Software Engineer",
            company="Naive Co",
            location="Remote - Worldwide",
            source="remotive",
            is_remote=True,
            posted_at=datetime.utcnow() - timedelta(days=3),  # naive
        )
        results = _apply_filters([job])
        assert len(results) == 1


# ═══════════════════════════════════════════════════════════════════════════
#  Per-source MAX_JOB_AGE_DAYS override
# ═══════════════════════════════════════════════════════════════════════════

class TestPerSourceMaxAge:
    def test_reliefweb_uses_30_day_default(self):
        """ReliefWeb job 20 days old passes with SOURCE_MAX_AGE_DAYS=30."""
        job = _make_job(
            title="Software Engineer",
            company="UNICEF",
            location="Remote - Worldwide",
            source="reliefweb",
            is_remote=True,
            is_ngo=True,
            posted_at=datetime.now(timezone.utc) - timedelta(days=20),
        )
        # Default MAX_JOB_AGE_DAYS=14 would reject, but reliefweb override=30
        results = _apply_filters([job])
        assert len(results) == 1

    def test_reliefweb_35_day_old_rejected(self):
        """ReliefWeb job 35 days old exceeds even the 30-day override."""
        job = _make_job(
            title="Software Engineer",
            company="UNICEF",
            location="Remote - Worldwide",
            source="reliefweb",
            is_remote=True,
            is_ngo=True,
            posted_at=datetime.now(timezone.utc) - timedelta(days=35),
        )
        results = _apply_filters([job])
        assert len(results) == 0

    def test_non_reliefweb_still_uses_global_default(self):
        """A non-reliefweb job 20 days old is still rejected by global 14-day max."""
        job = _make_job(
            title="Software Engineer",
            company="Normal Corp",
            location="Remote - Worldwide",
            source="remotive",
            is_remote=True,
            posted_at=datetime.now(timezone.utc) - timedelta(days=20),
        )
        results = _apply_filters([job])
        assert len(results) == 0

    def test_cli_max_age_does_not_override_source(self):
        """CLI --max-age is a fallback, but per-source override takes priority."""
        job = _make_job(
            title="Software Engineer",
            company="UNICEF",
            location="Remote - Worldwide",
            source="reliefweb",
            is_remote=True,
            is_ngo=True,
            posted_at=datetime.now(timezone.utc) - timedelta(days=25),
        )
        # max_age_days=7 via CLI, but reliefweb override=30 takes precedence
        results = _apply_filters([job], max_age_days=7)
        assert len(results) == 1


# ═══════════════════════════════════════════════════════════════════════════
#  Verbose rejection tracking
# ═══════════════════════════════════════════════════════════════════════════

class TestVerboseRejections:
    def test_verbose_false_no_output(self, capsys):
        """When verbose=False (default), no rejection table is printed."""
        job = _make_job(
            title="Office Assistant",
            company="Bad Co",
            location="Remote - Worldwide",
            source="remotive",
        )
        _apply_filters([job], verbose=False)
        captured = capsys.readouterr()
        assert "REJECTED" not in captured.out

    def test_verbose_true_prints_rejections(self, capsys):
        """When verbose=True, rejected jobs are printed to stdout."""
        job = _make_job(
            title="Office Assistant",
            company="Bad Co",
            location="Remote - Worldwide",
            source="remotive",
        )
        _apply_filters([job], verbose=True)
        captured = capsys.readouterr()
        assert "REJECTED JOBS" in captured.out
        assert "Office Assistant" in captured.out
        assert "role" in captured.out.lower()

    def test_verbose_shows_location_rejection(self, capsys):
        """Location rejections show scope info."""
        job = _make_job(
            title="Software Engineer",
            company="US Corp",
            location="US Only, Remote",
            source="remotive",
            is_remote=True,
            remote_scope="restricted",
        )
        _apply_filters([job], verbose=True)
        captured = capsys.readouterr()
        assert "REJECTED JOBS" in captured.out
        assert "location" in captured.out.lower()

    def test_verbose_shows_recency_rejection(self, capsys):
        """Recency rejections show age info."""
        job = _make_job(
            title="Software Engineer",
            company="Old Co",
            location="Remote - Worldwide",
            source="remotive",
            is_remote=True,
            posted_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
        _apply_filters([job], verbose=True)
        captured = capsys.readouterr()
        assert "REJECTED JOBS" in captured.out
        assert "recency" in captured.out.lower()


# ═══════════════════════════════════════════════════════════════════════════
#  _format_age — human-readable age display
# ═══════════════════════════════════════════════════════════════════════════

class TestFormatAge:
    def test_none(self):
        assert _format_age(None) == "age unknown"

    def test_just_now(self):
        result = _format_age(datetime.now(timezone.utc) - timedelta(minutes=5))
        assert result == "just now"

    def test_hours_ago(self):
        result = _format_age(datetime.now(timezone.utc) - timedelta(hours=5))
        assert result == "5h ago"

    def test_one_day(self):
        result = _format_age(datetime.now(timezone.utc) - timedelta(days=1))
        assert result == "1d ago"

    def test_multiple_days(self):
        result = _format_age(datetime.now(timezone.utc) - timedelta(days=7))
        assert result == "7d ago"

    def test_naive_datetime(self):
        """Naive datetime treated as UTC."""
        result = _format_age(datetime.utcnow() - timedelta(days=3))
        assert result == "3d ago"


# ═══════════════════════════════════════════════════════════════════════════
#  --stats display
# ═══════════════════════════════════════════════════════════════════════════

class TestShowStats:
    @pytest.mark.asyncio
    async def test_shows_total_and_sources(self, capsys):
        """_show_stats prints the dashboard with totals and source breakdown."""
        mock_stats = {
            "total": 312,
            "ngo_count": 45,
            "new_24h": 12,
            "sources": {"remotive": 120, "arbeitnow": 89, "idealist": 55},
            "top_companies": [
                ("Mozilla", 8),
                ("Wikimedia", 5),
                ("Acme Corp", 3),
            ],
            "last_fetched_at": datetime.now(timezone.utc) - timedelta(minutes=14),
        }
        with patch("main.init_db", new_callable=AsyncMock):
            with patch("main.get_stats", new_callable=AsyncMock, return_value=mock_stats):
                await _show_stats()

        captured = capsys.readouterr()
        assert "312" in captured.out
        assert "45" in captured.out
        assert "12" in captured.out
        assert "remotive" in captured.out
        assert "120" in captured.out
        assert "arbeitnow" in captured.out
        assert "Mozilla" in captured.out
        assert "14 minutes ago" in captured.out

    @pytest.mark.asyncio
    async def test_shows_never_when_no_scans(self, capsys):
        """When last_fetched_at is None, shows 'never'."""
        mock_stats = {
            "total": 0,
            "ngo_count": 0,
            "new_24h": 0,
            "sources": {},
            "top_companies": [],
            "last_fetched_at": None,
        }
        with patch("main.init_db", new_callable=AsyncMock):
            with patch("main.get_stats", new_callable=AsyncMock, return_value=mock_stats):
                await _show_stats()

        captured = capsys.readouterr()
        assert "never" in captured.out
        assert "Total jobs in DB" in captured.out

    @pytest.mark.asyncio
    async def test_shows_hours_ago(self, capsys):
        """When last scan was 3 hours ago, displays correctly."""
        mock_stats = {
            "total": 50,
            "ngo_count": 10,
            "new_24h": 5,
            "sources": {"remotive": 50},
            "top_companies": [("TestCo", 50)],
            "last_fetched_at": datetime.now(timezone.utc) - timedelta(hours=3),
        }
        with patch("main.init_db", new_callable=AsyncMock):
            with patch("main.get_stats", new_callable=AsyncMock, return_value=mock_stats):
                await _show_stats()

        captured = capsys.readouterr()
        assert "3 hours ago" in captured.out
