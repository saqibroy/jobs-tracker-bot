"""Phase 5A catalog, scheduler, persistence, and grouped-health tests."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

import config
import health
import main
from filters.notification_policy import select_company_candidates
from models.job import Job
from models.scan import ScanSummary, SourceFunnelMetrics, SourceStatus
from sources.base import BaseSource
from sources.ats_url_sniffer import append_sniffed_candidates
from sources.catalog import (
    GROUP_A_ID,
    GROUP_B_ID,
    GROUP_BY_ID,
    SOURCE_BY_NAME,
    SOURCE_CATALOG,
    SOURCE_GROUPS,
    SourceDefinition,
    direct_network_constructs,
    manual_all_source_names,
    unreviewed_scheduled_network_bypasses,
)
from storage.database import (
    get_group_last_completed,
    get_latest_scan_summary,
    get_latest_source_statuses,
    init_db,
    persist_scan_metrics,
)


GROUP_A_NAMES = (
    "greenhouse",
    "ashby",
    "personio",
    "lever",
    "workable",
    "jsonld",
)
GROUP_B_NAMES = (
    "arbeitnow",
    "remotive",
    "himalayas",
    "remoteok",
    "idealist",
    "linkedin",
    "berlinstartupjobs",
)


def test_catalog_has_exact_phase5a_groups_and_manual_all_union() -> None:
    assert tuple(group.scheduler_id for group in SOURCE_GROUPS) == (
        GROUP_A_ID,
        GROUP_B_ID,
    )
    assert GROUP_BY_ID[GROUP_A_ID].source_names == GROUP_A_NAMES
    assert GROUP_BY_ID[GROUP_B_ID].source_names == GROUP_B_NAMES
    assert GROUP_BY_ID[GROUP_B_ID].cadence_minutes == 120
    assert manual_all_source_names() == GROUP_A_NAMES + GROUP_B_NAMES
    assert "source_group_c" not in GROUP_BY_ID


def test_catalog_keeps_linkedin_scheduled_and_stepstone_manual_only() -> None:
    linkedin = SOURCE_BY_NAME["linkedin"]
    stepstone = SOURCE_BY_NAME["stepstone"]
    assert linkedin.scheduled_group == GROUP_B_ID
    assert linkedin.manual_only is False
    assert stepstone.scheduled_group is None
    assert stepstone.manual_only is True
    assert "stepstone" not in manual_all_source_names()


def test_catalog_has_unique_scheduled_membership_and_all_adapters_resolve() -> None:
    scheduled = [name for group in SOURCE_GROUPS for name in group.source_names]
    assert len(scheduled) == len(set(scheduled))
    assert set(main.ALL_SOURCES) == set(SOURCE_BY_NAME)
    for entry in SOURCE_CATALOG:
        resolved = main._get_sources(entry.name)
        assert len(resolved) == 1
        assert isinstance(resolved[0], entry.adapter_class)


def test_scheduling_settings_validate_independent_limits_and_legacy_fallback() -> None:
    defaults = config.load_scheduling_settings({})
    assert defaults.max_concurrent_source_adapters == 2
    assert defaults.max_concurrent_source_components == 3
    assert defaults.max_concurrent_http_requests == 4
    assert defaults.group_a_startup_delay_minutes == 1
    assert defaults.group_b_startup_delay_minutes == 6

    legacy = config.load_scheduling_settings({"MAX_CONCURRENT_SOURCES": "7"})
    assert legacy.max_concurrent_source_adapters == 7
    assert legacy.max_concurrent_source_components == 3
    assert legacy.max_concurrent_http_requests == 4

    explicit = config.load_scheduling_settings(
        {
            "MAX_CONCURRENT_SOURCES": "invalid-but-unused",
            "MAX_CONCURRENT_SOURCE_ADAPTERS": "2",
            "MAX_CONCURRENT_SOURCE_COMPONENTS": "5",
            "MAX_CONCURRENT_HTTP_REQUESTS": "6",
        }
    )
    assert explicit.max_concurrent_source_adapters == 2
    assert explicit.max_concurrent_source_components == 5
    assert explicit.max_concurrent_http_requests == 6


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("MAX_CONCURRENT_SOURCE_ADAPTERS", "0"),
        ("MAX_CONCURRENT_SOURCE_COMPONENTS", "not-an-int"),
        ("MAX_CONCURRENT_HTTP_REQUESTS", "65"),
    ],
)
def test_scheduling_settings_reject_invalid_concurrency(key: str, value: str) -> None:
    with pytest.raises(ValueError, match=key):
        config.load_scheduling_settings({key: value})


def test_scheduling_settings_reject_invalid_offsets() -> None:
    with pytest.raises(ValueError, match="distinct"):
        config.load_scheduling_settings(
            {
                "SOURCE_GROUP_A_STARTUP_DELAY_MINUTES": "2",
                "SOURCE_GROUP_B_STARTUP_DELAY_MINUTES": "2",
            }
        )
    with pytest.raises(ValueError, match="Group A cadence"):
        config.load_scheduling_settings(
            {
                "SOURCE_GROUP_A_INTERVAL_MINUTES": "5",
                "SOURCE_GROUP_A_STARTUP_DELAY_MINUTES": "5",
            }
        )


def test_scheduled_source_network_audit_has_no_unreviewed_bypass() -> None:
    assert unreviewed_scheduled_network_bypasses() == {}


class _FixtureSource(BaseSource):
    name = "fixture"

    async def fetch(self):
        return []


def test_network_guard_flags_representative_new_direct_client() -> None:
    entry = SourceDefinition("fixture", _FixtureSource, GROUP_A_ID, False)
    findings = unreviewed_scheduled_network_bypasses(
        source_loader=lambda _entry: "async with httpx.AsyncClient() as client: pass",
        entries=(entry,),
    )
    assert findings == {"fixture": ("httpx.AsyncClient",)}
    assert direct_network_constructs("requests.get('https://example.test')") == (
        "requests request",
    )


class _FakeScheduler:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object, dict]] = []
        self.jobs: dict[str, SimpleNamespace] = {}

    def add_job(self, function, trigger, **kwargs) -> None:
        self.calls.append((function, trigger, kwargs))
        self.jobs[kwargs["id"]] = SimpleNamespace(
            id=kwargs["id"], next_run_time=kwargs.get("next_run_time")
        )

    def get_job(self, job_id: str):
        return self.jobs.get(job_id)


class _RuntimeScheduler(_FakeScheduler):
    def __init__(self) -> None:
        super().__init__()
        self.running = False

    def start(self) -> None:
        self.running = True

    def shutdown(self, wait: bool = False) -> None:
        del wait
        self.running = False


def test_group_scheduler_registration_is_stable_staggered_and_bounded() -> None:
    scheduler = _FakeScheduler()
    now = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)
    main._register_source_group_jobs(scheduler, now=now)  # type: ignore[arg-type]

    by_id = {kwargs["id"]: kwargs for _, _, kwargs in scheduler.calls}
    assert set(by_id) == {GROUP_A_ID, GROUP_B_ID}
    assert "scan" not in by_id
    assert "source_group_c" not in by_id
    for group in SOURCE_GROUPS:
        kwargs = by_id[group.scheduler_id]
        assert kwargs["max_instances"] == 1
        assert kwargs["coalesce"] is True
        assert kwargs["misfire_grace_time"] == config.SOURCE_GROUP_MISFIRE_GRACE_SECONDS
        assert kwargs["next_run_time"] == now + timedelta(
            minutes=group.startup_delay_minutes
        )
        assert kwargs["next_run_time"] > now
        assert 0 < group.startup_delay_minutes < group.cadence_minutes
    assert by_id[GROUP_A_ID]["next_run_time"] != by_id[GROUP_B_ID]["next_run_time"]
    assert main._next_source_group_run_time(scheduler) == min(  # type: ignore[arg-type]
        by_id[GROUP_A_ID]["next_run_time"],
        by_id[GROUP_B_ID]["next_run_time"],
    )


def _metrics(source: str, completed_at: datetime) -> SourceFunnelMetrics:
    return SourceFunnelMetrics(
        source=source,
        started_at=completed_at - timedelta(seconds=1),
        completed_at=completed_at,
        duration_ms=1000,
        status=SourceStatus.HEALTHY,
        raw_count=1,
        accepted_count=1,
    )


@pytest.mark.asyncio
async def test_scan_scope_migration_is_idempotent_and_preserves_legacy_rows(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "legacy.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(database))
    async with aiosqlite.connect(database) as db:
        await db.execute(
            """
            CREATE TABLE source_scan_runs (
                scan_id TEXT NOT NULL, source TEXT NOT NULL,
                started_at TEXT NOT NULL, completed_at TEXT NOT NULL,
                duration_ms INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
                raw_count INTEGER NOT NULL DEFAULT 0,
                accepted_count INTEGER NOT NULL DEFAULT 0,
                unseen_count INTEGER NOT NULL DEFAULT 0,
                saved_count INTEGER NOT NULL DEFAULT 0,
                rejection_counts TEXT NOT NULL DEFAULT '{}',
                routing_counts TEXT NOT NULL DEFAULT '{}',
                issue_count INTEGER NOT NULL DEFAULT 0,
                sanitized_error TEXT, created_at TEXT NOT NULL,
                PRIMARY KEY (scan_id, source)
            )
            """
        )
        await db.execute(
            """
            INSERT INTO source_scan_runs (
                scan_id, source, started_at, completed_at, status, created_at
            ) VALUES ('legacy', 'greenhouse', '2026-01-01', '2026-01-01',
                      'healthy', '2026-01-01')
            """
        )
        await db.commit()

    await init_db()
    await init_db()
    async with aiosqlite.connect(database) as db:
        columns = {
            row[1]: row for row in await (await db.execute("PRAGMA table_info(source_scan_runs)")).fetchall()
        }
        indexes = {
            row[1] for row in await (await db.execute("PRAGMA index_list(source_scan_runs)")).fetchall()
        }
        scope = await (await db.execute(
            "SELECT scan_scope FROM source_scan_runs WHERE scan_id='legacy'"
        )).fetchone()
    assert columns["scan_scope"][3] == 1
    assert columns["scan_scope"][4] == "'legacy_all'"
    assert scope == ("legacy_all",)
    assert "idx_source_scan_runs_scope_completed" in indexes
    assert "idx_source_scan_runs_scope_created" in indexes


@pytest.mark.asyncio
async def test_group_scopes_restore_latest_scope_and_preserve_per_source_health(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "scopes.db"))
    await init_db()
    first = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)
    second = first + timedelta(minutes=5)
    third = second + timedelta(minutes=5)
    await persist_scan_metrics(
        ScanSummary("a", first - timedelta(seconds=1), first, {
            "greenhouse": _metrics("greenhouse", first)
        }, scan_scope="group_a")
    )
    await persist_scan_metrics(
        ScanSummary("b", second - timedelta(seconds=1), second, {
            "linkedin": _metrics("linkedin", second)
        }, scan_scope="group_b")
    )
    await persist_scan_metrics(
        ScanSummary("manual", third - timedelta(seconds=1), third, {
            "arbeitnow": _metrics("arbeitnow", third)
        }, scan_scope="manual_all")
    )

    restored = await get_latest_scan_summary()
    assert restored is not None
    assert restored["scope"] == "manual_all"
    assert restored["completed_at"] == third.isoformat()
    assert restored["sources"] == {"arbeitnow": 1}
    assert restored["group_last_completed"] == {
        "group_a": first.isoformat(),
        "group_b": second.isoformat(),
    }
    source_health = {item["source"]: item for item in await get_latest_source_statuses()}
    assert source_health["greenhouse"]["last_completed_at"] == first.isoformat()
    assert source_health["linkedin"]["last_completed_at"] == second.isoformat()
    assert source_health["arbeitnow"]["last_completed_at"] == third.isoformat()
    assert await get_group_last_completed() == restored["group_last_completed"]


@pytest.mark.asyncio
async def test_health_keeps_legacy_fields_and_adds_scope_groups_and_readiness() -> None:
    health.set_core_ready(False)
    assert health.is_core_ready() is False
    health.set_scan_summary(
        {
            "scope": "group_b",
            "raw": 3,
            "eligible_role_matches": 2,
            "rejected": 1,
            "immediate": 1,
            "digest": 1,
            "diagnostic": 0,
            "sources": {"linkedin": 3},
            "group_last_completed": {"group_a": "2026-08-11T08:00:00+00:00"},
        }
    )
    next_time = datetime.now(timezone.utc) + timedelta(seconds=30)
    health.set_next_scan_time(next_time)
    health.set_core_ready(True)
    response = await health._health_handler(None)  # type: ignore[arg-type]
    payload = json.loads(response.text)
    assert payload["status"] == "ok"
    assert payload["ready"] is True
    assert payload["ready_at"]
    assert 0 <= payload["next_scan_in_seconds"] <= 30
    summary = payload["last_scan_summary"]
    assert summary["scope"] == "group_b"
    assert summary["group_last_completed"] == {
        "group_a": "2026-08-11T08:00:00+00:00"
    }
    for key in ("raw", "eligible_role_matches", "rejected", "immediate", "digest"):
        assert key in summary
    health.set_core_ready(False)
    assert health.is_core_ready() is False


def test_daily_status_labels_scope_and_group_freshness() -> None:
    text = main._daily_group_freshness(
        {
            "scope": "group_b",
            "group_last_completed": {
                "group_b": "2026-08-11T08:05:00+00:00",
                "group_a": "2026-08-11T08:00:00+00:00",
            },
        }
    )
    assert "group_a" in text
    assert "group_b" in text
    assert len(text) <= 1000


def _job(suffix: str, *, source: str, company: str = "Acme") -> Job:
    return Job(
        title=f"Frontend Engineer {suffix}",
        company=company,
        location="Germany",
        is_remote=True,
        workplace_type="remote",
        eligible_countries=["de"],
        remote_scope="germany",
        url=f"https://jobs.example/{suffix}",
        description="React TypeScript frontend product engineering",
        source=source,
        match_score=80,
        notification_tier="immediate",
    )


def test_company_cap_remains_scan_local_without_persistent_quota() -> None:
    group_a = [_job("a1", source="greenhouse"), _job("a2", source="ashby")]
    group_b = [_job("b1", source="linkedin"), _job("b2", source="arbeitnow")]
    selected_a = select_company_candidates(
        group_a, 2, mode="diversity"
    )
    selected_b = select_company_candidates(
        group_b, 2, mode="diversity"
    )
    combined = select_company_candidates(
        group_a + group_b, 2, mode="diversity"
    )
    assert len(selected_a.selected) == 2
    assert len(selected_b.selected) == 2
    assert len(combined.selected) == 2
    assert len(combined.excluded) == 2


def test_ats_discovery_is_scope_local_and_append_idempotent(tmp_path: Path) -> None:
    seed = tmp_path / "sniffed.txt"
    direct = _job("direct", source="greenhouse")
    direct.url = "https://jobs.ashbyhq.com/new-company/123"
    aggregator = _job("aggregator", source="linkedin")
    aggregator.url = "https://jobs.ashbyhq.com/new-company/123"
    assert append_sniffed_candidates([direct], path=seed, existing_keys=set()) == 0
    assert not seed.exists()
    assert append_sniffed_candidates([aggregator], path=seed, existing_keys=set()) == 1
    assert append_sniffed_candidates([aggregator], path=seed, existing_keys=set()) == 0
    assert seed.read_text(encoding="utf-8").count("ashby:new-company") == 1


def test_phase5a_introduces_no_playwright_or_chromium_runtime() -> None:
    root = Path(__file__).resolve().parents[2]
    dependency_text = (root / "requirements.txt").read_text(encoding="utf-8").lower()
    docker_text = (root / "Dockerfile").read_text(encoding="utf-8").lower()
    config_text = (root / "config.py").read_text(encoding="utf-8")
    assert "playwright" not in dependency_text
    assert "chromium" not in dependency_text
    assert "playwright" not in docker_text
    assert "chromium" not in docker_text
    assert "DISABLE_PLAYWRIGHT" not in config_text


@pytest.mark.asyncio
async def test_core_readiness_precedes_first_group_and_restarts_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transitions: list[bool] = []
    original_set_ready = health.set_core_ready

    def record_ready(value: bool) -> None:
        transitions.append(value)
        original_set_ready(value)

    class ImmediateStopEvent:
        def set(self) -> None:
            pass

        async def wait(self) -> None:
            assert health.is_core_ready() is True

    runner = SimpleNamespace(cleanup=AsyncMock())
    monkeypatch.setattr(health, "set_core_ready", record_ready)
    monkeypatch.setattr(health, "start_health_server", AsyncMock(return_value=runner))
    monkeypatch.setattr(main, "AsyncIOScheduler", _RuntimeScheduler)
    monkeypatch.setattr(main, "init_db", AsyncMock())
    monkeypatch.setattr(main, "_restore_persisted_health_state", AsyncMock())
    monkeypatch.setattr(main, "get_total_count", AsyncMock(return_value=0))
    monkeypatch.setattr(main.asyncio, "Event", ImmediateStopEvent)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *_args: None)
    for key in (
        "DAILY_STATUS_ENABLED",
        "WEEKLY_DIGEST_ENABLED",
        "ZOHO_MAIL_SYNC_ENABLED",
    ):
        monkeypatch.setattr(config, key, False)
    for key in (
        "DISCORD_BOT_TOKEN",
        "DISCORD_COMMAND_CHANNEL_ID",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "DISCORD_WEBHOOK_URL",
    ):
        monkeypatch.setattr(config, key, "")

    await main._async_main([])
    await main._async_main([])
    assert transitions == [False, True, False, False, True, False]
    assert health.is_core_ready() is False
    assert runner.cleanup.await_count == 2


@pytest.mark.asyncio
async def test_discord_and_telegram_connection_failures_do_not_block_core_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    integration_ready_states: list[bool] = []

    class YieldingStopEvent:
        def set(self) -> None:
            pass

        async def wait(self) -> None:
            assert health.is_core_ready() is True
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    class FailedDiscordBot:
        def __init__(self, **_kwargs) -> None:
            pass

        def set_scan_times(self, **_kwargs) -> None:
            pass

        async def start(self, _token: str) -> None:
            integration_ready_states.append(health.is_core_ready())
            raise RuntimeError("discord unavailable")

        def is_closed(self) -> bool:
            return True

        async def close(self) -> None:
            pass

    class FailedTelegramApp:
        updater = None
        running = False

        async def shutdown(self) -> None:
            pass

    class FailedTelegramNotifier:
        def build_application(self, **_kwargs):
            return FailedTelegramApp()

        async def register_commands(self) -> None:
            integration_ready_states.append(health.is_core_ready())
            raise RuntimeError("telegram unavailable")

    import discord_bot
    import notifiers.telegram_notifier as telegram_module

    runner = SimpleNamespace(cleanup=AsyncMock())
    monkeypatch.setattr(discord_bot, "JobTrackerBot", FailedDiscordBot)
    monkeypatch.setattr(telegram_module, "TelegramNotifier", FailedTelegramNotifier)
    monkeypatch.setattr(health, "start_health_server", AsyncMock(return_value=runner))
    monkeypatch.setattr(main, "AsyncIOScheduler", _RuntimeScheduler)
    monkeypatch.setattr(main, "init_db", AsyncMock())
    monkeypatch.setattr(main, "_restore_persisted_health_state", AsyncMock())
    monkeypatch.setattr(main, "get_total_count", AsyncMock(return_value=0))
    monkeypatch.setattr(main.asyncio, "Event", YieldingStopEvent)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *_args: None)
    monkeypatch.setattr(config, "DAILY_STATUS_ENABLED", False)
    monkeypatch.setattr(config, "WEEKLY_DIGEST_ENABLED", False)
    monkeypatch.setattr(config, "ZOHO_MAIL_SYNC_ENABLED", False)
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "token")
    monkeypatch.setattr(config, "DISCORD_COMMAND_CHANNEL_ID", "123")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")

    await main._async_main([])
    assert integration_ready_states == [True, True]
    assert health.is_core_ready() is False


@pytest.mark.asyncio
async def test_source_failure_does_not_clear_core_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    health.set_core_ready(True)
    monkeypatch.setattr(main, "run_scan", AsyncMock(side_effect=RuntimeError("offline")))
    await main._scheduled_source_group(GROUP_A_ID)
    assert health.is_core_ready() is True
    health.set_core_ready(False)
