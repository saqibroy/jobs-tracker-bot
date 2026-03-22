"""Tests for storage/database.py — stats and query functions."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models.job import Job
from storage.database import get_stats, get_total_count, get_recent_unnotified, mark_notified, init_db, save_jobs


# ── helpers ────────────────────────────────────────────────────────────────

def _make_job(**overrides) -> Job:
    """Create a minimal Job for testing, with sensible defaults."""
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
    db_path = str(tmp_path / "test_jobs.db")
    monkeypatch.setattr("storage.database.config.DATABASE_PATH", db_path)
    await init_db()
    return db_path


# ═══════════════════════════════════════════════════════════════════════════
#  get_stats
# ═══════════════════════════════════════════════════════════════════════════

class TestGetStats:
    @pytest.mark.asyncio
    async def test_empty_database(self, tmp_db):
        stats = await get_stats()
        assert stats["total"] == 0
        assert stats["ngo_count"] == 0
        assert stats["new_24h"] == 0
        assert stats["sources"] == {}
        assert stats["top_companies"] == []
        assert stats["last_fetched_at"] is None

    @pytest.mark.asyncio
    async def test_total_count(self, tmp_db):
        jobs = [
            _make_job(title=f"Dev {i}", url=f"https://example.com/{i}")
            for i in range(5)
        ]
        await save_jobs(jobs)
        stats = await get_stats()
        assert stats["total"] == 5

    @pytest.mark.asyncio
    async def test_ngo_count(self, tmp_db):
        jobs = [
            _make_job(title="NGO Dev", url="https://example.com/ngo1", is_ngo=True),
            _make_job(title="NGO Dev 2", url="https://example.com/ngo2", is_ngo=True),
            _make_job(title="Regular Dev", url="https://example.com/gen1", is_ngo=False),
        ]
        await save_jobs(jobs)
        stats = await get_stats()
        assert stats["ngo_count"] == 2

    @pytest.mark.asyncio
    async def test_new_24h(self, tmp_db):
        """Jobs fetched within the last 24h should be counted."""
        # save_jobs uses Job.fetched_at which defaults to now()
        jobs = [
            _make_job(title="Fresh Dev", url="https://example.com/fresh"),
        ]
        await save_jobs(jobs)
        stats = await get_stats()
        assert stats["new_24h"] == 1

    @pytest.mark.asyncio
    async def test_sources_breakdown(self, tmp_db):
        jobs = [
            _make_job(title="Dev A", url="https://example.com/a", source="remotive"),
            _make_job(title="Dev B", url="https://example.com/b", source="remotive"),
            _make_job(title="Dev C", url="https://example.com/c", source="arbeitnow"),
            _make_job(title="Dev D", url="https://example.com/d", source="idealist"),
        ]
        await save_jobs(jobs)
        stats = await get_stats()
        assert stats["sources"]["remotive"] == 2
        assert stats["sources"]["arbeitnow"] == 1
        assert stats["sources"]["idealist"] == 1

    @pytest.mark.asyncio
    async def test_top_companies(self, tmp_db):
        jobs = [
            _make_job(title="Dev 1", company="Mozilla", url="https://example.com/1"),
            _make_job(title="Dev 2", company="Mozilla", url="https://example.com/2"),
            _make_job(title="Dev 3", company="Mozilla", url="https://example.com/3"),
            _make_job(title="Dev 4", company="Wikimedia", url="https://example.com/4"),
            _make_job(title="Dev 5", company="Wikimedia", url="https://example.com/5"),
            _make_job(title="Dev 6", company="Acme", url="https://example.com/6"),
        ]
        await save_jobs(jobs)
        stats = await get_stats()
        companies = stats["top_companies"]
        # Should be sorted by count descending
        assert companies[0] == ("Mozilla", 3)
        assert companies[1] == ("Wikimedia", 2)
        assert companies[2] == ("Acme", 1)

    @pytest.mark.asyncio
    async def test_top_companies_limited_to_10(self, tmp_db):
        jobs = [
            _make_job(
                title=f"Dev at Company{i}",
                company=f"Company{i}",
                url=f"https://example.com/{i}",
            )
            for i in range(15)
        ]
        await save_jobs(jobs)
        stats = await get_stats()
        assert len(stats["top_companies"]) == 10

    @pytest.mark.asyncio
    async def test_last_fetched_at(self, tmp_db):
        jobs = [
            _make_job(title="Fresh", url="https://example.com/fresh"),
        ]
        await save_jobs(jobs)
        stats = await get_stats()
        assert stats["last_fetched_at"] is not None
        # Should be within the last minute
        delta = datetime.now(timezone.utc) - stats["last_fetched_at"]
        assert delta.total_seconds() < 60


# ═══════════════════════════════════════════════════════════════════════════
#  get_total_count (existing, ensure still works)
# ═══════════════════════════════════════════════════════════════════════════

class TestGetTotalCount:
    @pytest.mark.asyncio
    async def test_empty(self, tmp_db):
        assert await get_total_count() == 0

    @pytest.mark.asyncio
    async def test_after_insert(self, tmp_db):
        jobs = [
            _make_job(title=f"Dev {i}", url=f"https://example.com/{i}")
            for i in range(3)
        ]
        await save_jobs(jobs)
        assert await get_total_count() == 3


# ═══════════════════════════════════════════════════════════════════════════
#  Digest: mark_notified + get_recent_unnotified
# ═══════════════════════════════════════════════════════════════════════════

class TestDigestNotification:
    @pytest.mark.asyncio
    async def test_recent_unnotified_returns_new_jobs(self, tmp_db):
        """Jobs saved in the last 6 hours with notified=0 should appear."""
        jobs = [
            _make_job(title="Fresh Dev", url="https://example.com/fresh1"),
            _make_job(title="Fresh Dev 2", url="https://example.com/fresh2"),
        ]
        await save_jobs(jobs)
        recent = await get_recent_unnotified(hours=6)
        assert len(recent) == 2

    @pytest.mark.asyncio
    async def test_mark_notified_excludes_from_digest(self, tmp_db):
        """After mark_notified, jobs should NOT appear in get_recent_unnotified."""
        jobs = [
            _make_job(title="Job A", url="https://example.com/a"),
            _make_job(title="Job B", url="https://example.com/b"),
            _make_job(title="Job C", url="https://example.com/c"),
        ]
        await save_jobs(jobs)

        # All 3 should appear before marking
        recent = await get_recent_unnotified(hours=6)
        assert len(recent) == 3

        # Mark first two as notified
        ids_to_mark = [jobs[0].id, jobs[1].id]
        await mark_notified(ids_to_mark)

        # Only 1 should remain
        recent_after = await get_recent_unnotified(hours=6)
        assert len(recent_after) == 1
        assert recent_after[0]["title"] == "Job C"

    @pytest.mark.asyncio
    async def test_digest_does_not_repeat_after_mark(self, tmp_db):
        """Simulates two digest cycles — second should return 0 jobs."""
        jobs = [
            _make_job(title="Dev X", url="https://example.com/x"),
        ]
        await save_jobs(jobs)

        # First digest cycle
        recent1 = await get_recent_unnotified(hours=6)
        assert len(recent1) == 1
        await mark_notified([r["id"] for r in recent1])

        # Second digest cycle — should be empty
        recent2 = await get_recent_unnotified(hours=6)
        assert len(recent2) == 0

    @pytest.mark.asyncio
    async def test_mark_notified_empty_list_no_error(self, tmp_db):
        """Calling mark_notified with empty list should not raise."""
        await mark_notified([])  # Should be a no-op

    @pytest.mark.asyncio
    async def test_recent_unnotified_respects_limit(self, tmp_db):
        """get_recent_unnotified should respect the limit parameter."""
        jobs = [
            _make_job(title=f"Dev {i}", url=f"https://example.com/{i}")
            for i in range(20)
        ]
        await save_jobs(jobs)
        recent = await get_recent_unnotified(hours=6, limit=5)
        assert len(recent) == 5
