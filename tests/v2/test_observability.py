"""Focused Phase 1A source and funnel observability tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest

import filters.pipeline as pipeline
import health
import main
from models.job import Job
from models.scan import (
    RejectionCode,
    ScanSummary,
    SourceFunnelMetrics,
    SourceStatus,
    classify_source_exception,
    sanitize_source_error,
)
from sources.base import BaseSource
from storage.database import (
    get_latest_scan_summary,
    get_latest_source_status,
    init_db,
    persist_scan_metrics,
    save_jobs,
)


def make_job(*, source: str = "alpha", suffix: str = "1", **overrides) -> Job:
    values = {
        "title": "Software Engineer",
        "company": "Acme",
        "location": "Remote worldwide",
        "remote_scope": "worldwide",
        "url": f"https://example.com/jobs/{suffix}",
        "description": "Build reliable software applications with a distributed product team.",
        "source": source,
    }
    values.update(overrides)
    return Job(**values)


def settings(**overrides) -> SimpleNamespace:
    values = {
        "COMPANY_BLOCKLIST": [],
        "FILTER_SENIOR_ONLY": False,
        "MIN_SALARY_EUR": 0,
        "SOURCE_MAX_AGE_DAYS": {},
        "MAX_JOB_AGE_DAYS": 14,
        "MINIMUM_MATCH_SCORE": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def allow_filter_dependencies(monkeypatch: pytest.MonkeyPatch, score: int = 70) -> None:
    monkeypatch.setattr(pipeline, "passes_location_filter", lambda job: True)
    monkeypatch.setattr(pipeline, "passes_role_filter", lambda job: True)
    monkeypatch.setattr(pipeline, "passes_stack_filter", lambda job: True)
    monkeypatch.setattr(pipeline, "passes_language_filter", lambda job: True)
    monkeypatch.setattr(pipeline, "classify_ngo", lambda job: job)

    def score_job(job: Job) -> int:
        job.notification_tier = "immediate" if score >= 70 else "digest"
        return score

    monkeypatch.setattr(pipeline, "compute_match_score", score_job)


class StubSource(BaseSource):
    name = "stub"

    def __init__(self, jobs: list[Job] | None = None, error: Exception | None = None):
        super().__init__()
        self.jobs = jobs or []
        self.error = error

    async def fetch(self) -> list[Job]:
        if self.error:
            raise self.error
        return self.jobs


def http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.com/jobs?token=secret")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("request failed", request=request, response=response)


@pytest.mark.asyncio
async def test_complete_source_success_and_healthy_zero_results() -> None:
    success = await StubSource([make_job(source="stub")]).fetch_outcome()
    empty = await StubSource().fetch_outcome()

    assert success.status is SourceStatus.HEALTHY
    assert success.raw_count == 1
    assert empty.status is SourceStatus.ZERO_RESULTS
    assert empty.raw_count == 0
    assert empty.issue_count == 0


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (http_error(429), SourceStatus.RATE_LIMITED),
        (http_error(401), SourceStatus.BLOCKED),
        (http_error(403), SourceStatus.BLOCKED),
        (http_error(407), SourceStatus.BLOCKED),
        (http_error(451), SourceStatus.BLOCKED),
        (http_error(503), SourceStatus.NETWORK_ERROR),
        (http_error(404), SourceStatus.UNKNOWN_ERROR),
        (httpx.ReadTimeout("slow request"), SourceStatus.NETWORK_ERROR),
        (httpx.ConnectError("connection failed"), SourceStatus.NETWORK_ERROR),
        (ValueError("invalid top-level JSON"), SourceStatus.PARSE_ERROR),
        (KeyError("missing jobs schema"), SourceStatus.PARSE_ERROR),
        (RuntimeError("unexpected bug"), SourceStatus.UNKNOWN_ERROR),
    ],
)
def test_complete_source_error_classification(error: Exception, expected: SourceStatus) -> None:
    assert classify_source_exception(error) is expected


@pytest.mark.asyncio
async def test_complete_source_failure_becomes_typed_outcome() -> None:
    outcome = await StubSource(error=httpx.ReadTimeout("timed out")).fetch_outcome()

    assert outcome.status is SourceStatus.NETWORK_ERROR
    assert outcome.jobs == []
    assert outcome.issue_count == 1
    assert outcome.sanitized_error == "timed out"


def test_error_sanitization_redacts_urls_secrets_controls_and_truncates() -> None:
    raw = (
        "failed https://example.com/path?token=hunter2#fragment "
        "password: swordfish Authorization=BearerSecret\n\x00"
        "https://discord.com/api/webhooks/123/very-secret "
        + "x" * 500
    )
    clean = sanitize_source_error(raw)

    assert clean is not None
    assert len(clean) == 300
    assert "hunter2" not in clean
    assert "fragment" not in clean
    assert "swordfish" not in clean
    assert "BearerSecret" not in clean
    assert "very-secret" not in clean
    assert "\n" not in clean and "\x00" not in clean
    assert "https://example.com/path" in clean


@pytest.mark.asyncio
async def test_legacy_safe_fetch_source_is_adapted() -> None:
    legacy = SimpleNamespace(name="legacy", safe_fetch=AsyncMock(return_value=[]))
    outcome = await main._fetch_source_outcome(legacy)

    assert outcome.status is SourceStatus.ZERO_RESULTS
    legacy.safe_fetch.assert_awaited_once()


@pytest.mark.parametrize(
    ("code", "settings_overrides", "job_overrides", "failed_dependency"),
    [
        (RejectionCode.COMPANY_BLOCKLIST, {"COMPANY_BLOCKLIST": ["acme"]}, {}, None),
        (RejectionCode.LOCATION, {}, {}, "passes_location_filter"),
        (RejectionCode.ROLE, {}, {}, "passes_role_filter"),
        (RejectionCode.STACK, {}, {}, "passes_stack_filter"),
        (RejectionCode.LANGUAGE, {}, {}, "passes_language_filter"),
        (
            RejectionCode.SENIORITY,
            {"FILTER_SENIOR_ONLY": True},
            {"title": "Mid-level Software Engineer"},
            None,
        ),
        (
            RejectionCode.SALARY,
            {"MIN_SALARY_EUR": 50_000},
            {"salary": "1000 EUR monthly"},
            None,
        ),
        (
            RejectionCode.RECENCY,
            {},
            {"posted_at": datetime.now(timezone.utc) - timedelta(days=40)},
            None,
        ),
    ],
)
def test_terminal_rejection_codes_before_scoring(
    monkeypatch: pytest.MonkeyPatch,
    code: RejectionCode,
    settings_overrides: dict,
    job_overrides: dict,
    failed_dependency: str | None,
) -> None:
    allow_filter_dependencies(monkeypatch)
    if failed_dependency:
        monkeypatch.setattr(pipeline, failed_dependency, lambda job: False)
    result = pipeline.run_filter_pipeline(
        [make_job(**job_overrides)],
        settings=settings(**settings_overrides),
    )

    assert result.accepted_count == 0
    assert result.rejection_counts[code] == 1
    result.validate_accounting()


def test_minimum_score_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    allow_filter_dependencies(monkeypatch, score=10)
    result = pipeline.run_filter_pipeline(
        [make_job()],
        settings=settings(MINIMUM_MATCH_SCORE=20),
    )
    assert result.rejection_counts[RejectionCode.MINIMUM_SCORE] == 1


def test_global_content_dedup_is_terminal_and_attributed(monkeypatch: pytest.MonkeyPatch) -> None:
    allow_filter_dependencies(monkeypatch)
    now = datetime.now(timezone.utc)
    newer = make_job(source="alpha", suffix="new", fetched_at=now)
    older = make_job(source="beta", suffix="old", fetched_at=now - timedelta(minutes=1))
    result = pipeline.run_filter_pipeline([older, newer], settings=settings())

    assert result.accepted_jobs == [newer]
    assert result.rejection_counts[RejectionCode.DUPLICATE_IN_MEMORY] == 1
    assert result.per_source["alpha"].accepted_count == 1
    assert result.per_source["beta"].rejection_counts[RejectionCode.DUPLICATE_IN_MEMORY] == 1
    result.validate_accounting()


def test_company_cap_rejection_and_per_source_accounting(monkeypatch: pytest.MonkeyPatch) -> None:
    allow_filter_dependencies(monkeypatch)
    jobs = [
        make_job(source="alpha", suffix="1", title="Software Engineer"),
        make_job(source="alpha", suffix="2", title="Frontend Developer"),
        make_job(source="beta", suffix="3", title="Backend Developer"),
    ]
    result = pipeline.run_filter_pipeline(jobs, settings=settings())

    assert result.accepted_count == 2
    assert result.rejection_counts[RejectionCode.COMPANY_CAP] == 1
    assert result.raw_count == result.accepted_count + result.rejected_count
    assert sum(item.raw_count for item in result.per_source.values()) == 3
    result.validate_accounting()


def scan_summary(
    *,
    scan_id: str = "scan-1",
    source: str = "alpha",
    status: SourceStatus = SourceStatus.HEALTHY,
    at: datetime | None = None,
) -> ScanSummary:
    timestamp = at or datetime.now(timezone.utc)
    metrics = SourceFunnelMetrics(
        source=source,
        started_at=timestamp - timedelta(seconds=1),
        completed_at=timestamp,
        duration_ms=1000,
        status=status,
    )
    return ScanSummary(scan_id, timestamp - timedelta(seconds=1), timestamp, {source: metrics})


@pytest.fixture
async def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "jobs.db"
    monkeypatch.setattr("storage.database.config.DATABASE_PATH", str(path))
    monkeypatch.setattr("main.config.DATABASE_PATH", str(path))
    await init_db()
    return path


@pytest.mark.asyncio
async def test_migration_is_idempotent(database: Path) -> None:
    await init_db()
    async with aiosqlite.connect(database) as db:
        cursor = await db.execute("PRAGMA table_info(source_scan_runs)")
        columns = {row[1] for row in await cursor.fetchall()}
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='source_scan_runs'"
        )
        indexes = {row[0] for row in await cursor.fetchall()}

    assert {
        "scan_id", "source", "started_at", "completed_at", "duration_ms",
        "status", "raw_count", "accepted_count", "unseen_count", "saved_count",
        "rejection_counts", "routing_counts", "issue_count", "sanitized_error",
        "created_at",
    } <= columns
    assert "idx_source_scan_runs_completed" in indexes
    assert "idx_source_scan_runs_source_completed" in indexes


@pytest.mark.asyncio
async def test_metrics_retention_removes_rows_older_than_30_days(database: Path) -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    async with aiosqlite.connect(database) as db:
        await db.execute(
            """
            INSERT INTO source_scan_runs (
                scan_id, source, started_at, completed_at, duration_ms, status,
                raw_count, accepted_count, unseen_count, saved_count,
                rejection_counts, routing_counts, issue_count, sanitized_error, created_at
            ) VALUES (?, ?, ?, ?, 0, 'healthy', 0, 0, 0, 0, '{}', '{}', 0, NULL, ?)
            """,
            ("old", "old-source", old, old, old),
        )
        await db.commit()

    await persist_scan_metrics(scan_summary(scan_id="current"))
    async with aiosqlite.connect(database) as db:
        cursor = await db.execute("SELECT scan_id FROM source_scan_runs ORDER BY scan_id")
        assert [row[0] for row in await cursor.fetchall()] == ["current"]


@pytest.mark.asyncio
async def test_persisted_error_is_sanitized_and_bounded(database: Path) -> None:
    summary = scan_summary(status=SourceStatus.UNKNOWN_ERROR)
    summary.sources["alpha"].issue_count = 1
    summary.sources["alpha"].sanitized_error = (
        "webhook_url=https://hooks.slack.com/services/T/B/SECRET "
        "client_secret=hunter2\n" + "x" * 500
    )
    await persist_scan_metrics(summary)

    async with aiosqlite.connect(database) as db:
        cursor = await db.execute("SELECT sanitized_error FROM source_scan_runs")
        stored = (await cursor.fetchone())[0]
    assert len(stored) <= 300
    assert "SECRET" not in stored
    assert "hunter2" not in stored
    assert "\n" not in stored


@pytest.mark.asyncio
async def test_source_timestamp_semantics(database: Path) -> None:
    now = datetime.now(timezone.utc)
    await persist_scan_metrics(
        scan_summary(scan_id="healthy", status=SourceStatus.HEALTHY, at=now - timedelta(hours=3))
    )
    await persist_scan_metrics(
        scan_summary(scan_id="failed", status=SourceStatus.NETWORK_ERROR, at=now - timedelta(hours=2))
    )
    await persist_scan_metrics(
        scan_summary(scan_id="partial", status=SourceStatus.PARTIAL_SUCCESS, at=now - timedelta(hours=1))
    )

    latest = await get_latest_source_status("alpha")
    assert latest is not None
    assert latest["status"] == "partial_success"
    assert latest["last_completed_at"].startswith((now - timedelta(hours=1)).date().isoformat())
    assert latest["last_usable_at"] == latest["last_completed_at"]
    assert latest["last_fully_successful_at"].startswith(
        (now - timedelta(hours=3)).date().isoformat()
    )


@pytest.mark.asyncio
async def test_save_jobs_returns_only_actual_inserts(database: Path) -> None:
    job = make_job()
    first = await save_jobs([job, job])
    second = await save_jobs([job])

    assert first == [job]
    assert second == []


@pytest.mark.asyncio
async def test_dry_run_does_not_modify_existing_database(
    database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = database.read_bytes()
    before_mtime = database.stat().st_mtime_ns
    monkeypatch.setattr(main.config, "ENABLE_ATS_SNIFFING", False)
    source = SimpleNamespace(
        name="alpha",
        safe_fetch=AsyncMock(return_value=[make_job(source="alpha")]),
    )

    await main.run_scan([source], dry_run=True)

    assert database.read_bytes() == before
    assert database.stat().st_mtime_ns == before_mtime


@pytest.mark.asyncio
async def test_notifications_receive_only_actually_inserted_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_filter_dependencies(monkeypatch)
    first = make_job(source="alpha", suffix="1")
    second = make_job(source="alpha", suffix="2", title="Frontend Developer")
    source = SimpleNamespace(name="alpha", safe_fetch=AsyncMock(return_value=[first, second]))
    monkeypatch.setattr(main.config, "ENABLE_ATS_SNIFFING", False)
    monkeypatch.setattr(main, "init_db", AsyncMock())
    monkeypatch.setattr(main, "filter_unseen", AsyncMock(return_value=[first, second]))
    monkeypatch.setattr(main, "save_jobs", AsyncMock(return_value=[first]))
    persist = AsyncMock()
    notify = AsyncMock()
    monkeypatch.setattr(main, "persist_scan_metrics", persist)
    monkeypatch.setattr(main, "get_latest_source_statuses", AsyncMock(return_value=[]))
    monkeypatch.setattr(main, "_send_notifications", notify)

    result = await main.run_scan([source], dry_run=False)

    assert result == [first]
    notify.assert_awaited_once_with([first])
    summary = persist.await_args.args[0]
    assert summary.accepted_count == 2
    assert summary.unseen_count == 2
    assert summary.saved_count == 1
    assert summary.routing_counts["immediate"] == 1


@pytest.mark.asyncio
async def test_health_payload_remains_backward_compatible() -> None:
    health.set_scan_summary(
        {
            "raw": 10,
            "eligible_role_matches": 3,
            "rejected": 7,
            "immediate": 1,
            "digest": 1,
            "diagnostic": 1,
            "accepted": 3,
            "unseen": 2,
            "saved": 1,
            "rejection_counts": {"location": 7},
            "source_health": {},
        }
    )
    response = await health._health_handler(None)  # type: ignore[arg-type]
    payload = json.loads(response.text)
    summary = payload["last_scan_summary"]

    for key in (
        "raw", "eligible_role_matches", "rejected", "immediate", "digest", "diagnostic"
    ):
        assert key in summary
    assert summary["accepted"] == 3
    assert summary["unseen"] == 2
    assert summary["saved"] == 1


@pytest.mark.asyncio
async def test_persisted_summary_restores_health_state(database: Path) -> None:
    summary = scan_summary(scan_id="restore")
    summary.sources["alpha"].raw_count = 1
    summary.sources["alpha"].accepted_count = 1
    summary.sources["alpha"].unseen_count = 1
    summary.sources["alpha"].saved_count = 1
    summary.sources["alpha"].routing_counts["digest"] = 1
    await persist_scan_metrics(summary)
    health._scan_summary = {}
    health._last_scan_time = None

    await main._restore_persisted_health_state()

    restored = health.get_scan_summary()
    assert restored["raw"] == 1
    assert restored["eligible_role_matches"] == 1
    assert restored["saved"] == 1
    assert restored["digest"] == 1
    assert health.get_last_scan_time() is not None


@pytest.mark.asyncio
async def test_latest_overall_scan_query(database: Path) -> None:
    await persist_scan_metrics(scan_summary(scan_id="latest"))
    latest = await get_latest_scan_summary()
    assert latest is not None
    assert latest["scan_id"] == "latest"
    assert latest["raw"] == 0
    assert latest["source_health"]["alpha"]["status"] == "healthy"


@pytest.mark.asyncio
async def test_stats_source_health_output_is_compact(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(main, "init_db", AsyncMock())
    monkeypatch.setattr(
        main,
        "get_stats",
        AsyncMock(
            return_value={
                "total": 0,
                "ngo_count": 0,
                "new_24h": 0,
                "sources": {},
                "notification_tiers": {},
                "top_companies": [],
                "last_fetched_at": None,
                "source_health": [
                    {
                        "source": "alpha",
                        "status": "network_error",
                        "raw": 0,
                        "accepted": 0,
                        "saved": 0,
                        "last_usable_at": None,
                    }
                ],
            }
        ),
    )

    await main._show_stats()
    output = capsys.readouterr().out
    assert "Latest source health" in output
    assert "alpha" in output
    assert "network_error" in output
    assert "never" in output
    assert len(output) < 4000


def test_daily_status_failure_names_are_bounded_to_five() -> None:
    summary = {
        "rejection_counts": {f"reason_{index}": index for index in range(10)},
        "source_health": {
            f"failed_{index}": {"status": "network_error"}
            for index in range(7)
        },
    }
    rejection_text, degraded_text = main._daily_status_details(summary)

    assert rejection_text.count("`") == 10  # five bounded reason labels
    assert degraded_text.count("`") == 10  # five bounded source names
    assert "+2 more" in degraded_text
    assert len(rejection_text) <= 1000
    assert len(degraded_text) <= 1000


@pytest.mark.asyncio
async def test_failed_source_does_not_hide_successful_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_filter_dependencies(monkeypatch)
    healthy = StubSource([make_job(source="stub")])
    failed = StubSource(error=httpx.ConnectError("offline"))
    failed.name = "failed"
    monkeypatch.setattr(main.config, "ENABLE_ATS_SNIFFING", False)

    jobs = await main.run_scan([healthy, failed], dry_run=True)
    source_health = health.get_scan_summary()["source_health"]

    assert len(jobs) == 1
    assert source_health["stub"]["status"] == "healthy"
    assert source_health["failed"]["status"] == "network_error"
    assert source_health["stub"]["last_usable_at"] is not None
    assert source_health["failed"]["last_usable_at"] is None
