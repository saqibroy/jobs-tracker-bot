from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import config
import integrations.zoho_mail as zoho_module
import notifiers.delivery as delivery_module
from integrations.job_alerts import AlertParserRegistry
from integrations.zoho_mail import ZohoMailIngestionWorker
from job_ingestion import (
    JobIngestionCandidate,
    JobIngestionStatus,
    process_discovered_jobs,
)
from models.job import Job
from notifiers.base import DeliverySuccess
from notifiers.delivery import process_pending_immediate_deliveries
from runtime_leases import immediate_delivery_lease, job_ingestion_lease
from sources.catalog import GROUP_BY_ID, manual_all_source_names
from storage.database import (
    filter_unseen,
    get_delivery_receipts,
    init_db,
    save_jobs,
)
from storage.zoho_mail import init_zoho_mail_db
from tests.v2.test_phase6a1_alert_foundation import (
    FakeZohoAPI,
    SyntheticAlertParser,
    message,
)


@pytest.fixture
async def replay_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "replay.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_path))
    monkeypatch.setattr(config, "ZOHO_INITIAL_SYNC_FROM", "")
    monkeypatch.setattr(config, "ZOHO_COMPANY_DISCOVERY_ENABLED", False)
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL_NGO", "")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(
        zoho_module,
        "process_pending_immediate_deliveries",
        AsyncMock(),
    )
    await init_zoho_mail_db()
    await init_db()
    return db_path


def alert_worker(api: FakeZohoAPI) -> ZohoMailIngestionWorker:
    return ZohoMailIngestionWorker(
        api,
        parser_registry=AlertParserRegistry((SyntheticAlertParser(),)),
    )


@pytest.mark.asyncio
async def test_item_persistence_then_pipeline_failure_replays_pending(
    replay_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_process = zoho_module.process_discovered_jobs
    monkeypatch.setattr(
        zoho_module,
        "process_discovered_jobs",
        AsyncMock(side_effect=RuntimeError("pipeline unavailable")),
    )
    first_api = FakeZohoAPI([message("replay")], contents={"replay": "cards"})
    with pytest.raises(RuntimeError, match="pipeline unavailable"):
        await alert_worker(first_api).run(dry_run=False)
    with sqlite3.connect(replay_db) as db:
        assert db.execute(
            "SELECT state FROM email_job_alert_items"
        ).fetchone()[0] == "pending"
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert db.execute(
            "SELECT processed, processing_version FROM zoho_mail_messages"
        ).fetchone() == (0, 0)
        assert db.execute("SELECT COUNT(*) FROM zoho_mail_sync_state").fetchone()[0] == 0

    monkeypatch.setattr(zoho_module, "process_discovered_jobs", real_process)
    missing_api = FakeZohoAPI(
        [message("replay", days_ago=15)],
        contents={"replay": "cards"},
    )
    with pytest.raises(RuntimeError, match="pending alert item was not reproduced"):
        await ZohoMailIngestionWorker(
            missing_api,
            parser_registry=AlertParserRegistry(
                (SyntheticAlertParser(item_count=0),)
            ),
        ).run(dry_run=False)
    with sqlite3.connect(replay_db) as db:
        assert db.execute(
            "SELECT state FROM email_job_alert_items"
        ).fetchone()[0] == "pending"
        assert db.execute("SELECT COUNT(*) FROM zoho_mail_sync_state").fetchone()[0] == 0

    second_api = FakeZohoAPI(
        [message("replay", days_ago=15)],
        contents={"replay": "cards"},
    )
    result = await alert_worker(second_api).run(dry_run=False)
    assert result.processed_alert_items == 1
    with sqlite3.connect(replay_db) as db:
        assert db.execute(
            "SELECT state, terminal_outcome FROM email_job_alert_items"
        ).fetchone() == ("processed", "saved")
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_job_save_then_item_completion_failure_retries_as_duplicate(
    replay_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_complete = zoho_module.complete_alert_item_results
    monkeypatch.setattr(
        zoho_module,
        "complete_alert_item_results",
        AsyncMock(side_effect=RuntimeError("completion failed")),
    )
    first_api = FakeZohoAPI([message("after-save")], contents={"after-save": "cards"})
    with pytest.raises(RuntimeError, match="completion failed"):
        await alert_worker(first_api).run(dry_run=False)
    with sqlite3.connect(replay_db) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert db.execute(
            "SELECT state FROM email_job_alert_items"
        ).fetchone()[0] == "pending"

    monkeypatch.setattr(zoho_module, "complete_alert_item_results", real_complete)
    second_api = FakeZohoAPI([message("after-save")], contents={"after-save": "cards"})
    result = await alert_worker(second_api).run(dry_run=False)
    assert result.pipeline_accepted == 1
    with sqlite3.connect(replay_db) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert db.execute(
            "SELECT state, terminal_outcome FROM email_job_alert_items"
        ).fetchone() == ("processed", "duplicate")
        assert db.execute(
            "SELECT COUNT(*) FROM job_delivery_receipts"
        ).fetchone()[0] == 0


def discovered_job(
    url: str,
    *,
    source: str,
    title: str = "Senior Frontend React Developer",
) -> Job:
    return Job(
        title=title,
        company="Concurrency GmbH",
        location="Remote - Germany",
        url=url,
        source=source,
        is_remote=True,
        workplace_type="remote",
        remote_scope="germany",
        description="React TypeScript frontend product",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mail_first", [False, True])
@pytest.mark.parametrize("different_url", [False, True])
async def test_source_mail_ingestion_serializes_url_and_content_dedup(
    replay_db: Path,
    mail_first: bool,
    different_url: bool,
) -> None:
    source_job = discovered_job("https://jobs.invalid/source", source="linkedin")
    mail_job = discovered_job(
        "https://jobs.invalid/mail" if different_url else source_job.url,
        source="synthetic_alert",
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def held_filter(jobs: list[Job]) -> list[Job]:
        entered.set()
        await release.wait()
        return await filter_unseen(jobs)

    first_job = mail_job if mail_first else source_job
    second_job = source_job if mail_first else mail_job
    first = asyncio.create_task(
        process_discovered_jobs(
            [JobIngestionCandidate("first", first_job)],
            persist=True,
            associate_items=True,
            filter_unseen_fn=held_filter,
        )
    )
    await entered.wait()
    second = asyncio.create_task(
        process_discovered_jobs(
            [JobIngestionCandidate("second", second_job)],
            persist=True,
            associate_items=True,
        )
    )
    await asyncio.sleep(0)
    release.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert first_result.item_results[0].status == JobIngestionStatus.SAVED
    assert second_result.item_results[0].status == JobIngestionStatus.DUPLICATE
    with sqlite3.connect(replay_db) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_one_batch_rejects_distinct_alert_identities_with_duplicate_content(
    replay_db: Path,
) -> None:
    first = discovered_job("https://jobs.invalid/batch-one", source="provider_one")
    second = discovered_job("https://jobs.invalid/batch-two", source="provider_two")
    result = await process_discovered_jobs(
        [
            JobIngestionCandidate("provider_one:id:1", first),
            JobIngestionCandidate("provider_two:id:2", second),
        ],
        persist=True,
        associate_items=True,
    )
    assert sorted(item.status.value for item in result.item_results) == [
        "rejected",
        "saved",
    ]
    rejected = next(
        item
        for item in result.item_results
        if item.status == JobIngestionStatus.REJECTED
    )
    assert rejected.rejection_code == "duplicate_in_memory"
    with sqlite3.connect(replay_db) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["exception", "cancellation"])
async def test_ingestion_lease_releases_after_failure(
    replay_db: Path,
    mode: str,
) -> None:
    entered = asyncio.Event()
    never = asyncio.Event()

    async def failing_filter(jobs: list[Job]) -> list[Job]:
        entered.set()
        if mode == "exception":
            raise RuntimeError("dedup failed")
        await never.wait()
        return jobs

    task = asyncio.create_task(
        process_discovered_jobs(
            [discovered_job("https://jobs.invalid/first", source="source")],
            persist=True,
            filter_unseen_fn=failing_filter,
        )
    )
    await entered.wait()
    if mode == "cancellation":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(RuntimeError, match="dedup failed"):
            await task

    result = await asyncio.wait_for(
        process_discovered_jobs(
            [discovered_job("https://jobs.invalid/second", source="source")],
            persist=True,
        ),
        timeout=2,
    )
    assert len(result.saved_jobs) == 1


class FakeDiscord:
    general_configured = True
    ngo_configured = False

    def __init__(
        self,
        *,
        wait: asyncio.Event | None = None,
        fail: bool = False,
    ) -> None:
        self.calls: list[list[str]] = []
        self.wait = wait
        self.fail = fail
        self.entered = asyncio.Event()

    def has_destination(self, destination: str) -> bool:
        return destination == "discord_general"

    async def send_jobs(
        self,
        jobs: list[Job],
        *,
        include_batch_header: bool = True,
    ) -> list[DeliverySuccess]:
        del include_batch_header
        self.calls.append([job.id for job in jobs])
        self.entered.set()
        if self.wait is not None:
            await self.wait.wait()
        if self.fail:
            raise RuntimeError("provider unavailable")
        return [DeliverySuccess(job.id, "discord_general") for job in jobs]


class FakeTelegram:
    configured = False


@pytest.mark.asyncio
async def test_concurrent_immediate_attempts_send_one_external_obligation(
    replay_db: Path,
) -> None:
    job = discovered_job("https://jobs.invalid/immediate", source="source")
    job.notification_tier = "immediate"
    job.match_score = 99
    await save_jobs([job])
    discord = FakeDiscord()
    first, second = await asyncio.gather(
        process_pending_immediate_deliveries(
            discord_notifier=discord,
            telegram_notifier=FakeTelegram(),
        ),
        process_pending_immediate_deliveries(
            discord_notifier=discord,
            telegram_notifier=FakeTelegram(),
        ),
    )
    assert len(discord.calls) == 1
    assert first.selected_count + second.selected_count == 1
    receipts = await get_delivery_receipts(job.id)
    assert [(row["delivery_kind"], row["destination"]) for row in receipts] == [
        ("immediate", "discord_general")
    ]


@pytest.mark.asyncio
async def test_delivery_lease_releases_after_cancellation(replay_db: Path) -> None:
    job = discovered_job("https://jobs.invalid/cancel-delivery", source="source")
    job.notification_tier = "immediate"
    job.match_score = 99
    await save_jobs([job])
    wait = asyncio.Event()
    blocked = FakeDiscord(wait=wait)
    task = asyncio.create_task(
        process_pending_immediate_deliveries(
            discord_notifier=blocked,
            telegram_notifier=FakeTelegram(),
        )
    )
    await blocked.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    succeeding = FakeDiscord()
    result = await asyncio.wait_for(
        process_pending_immediate_deliveries(
            discord_notifier=succeeding,
            telegram_notifier=FakeTelegram(),
        ),
        timeout=2,
    )
    assert result.selected_count == 1
    assert len(succeeding.calls) == 1


@pytest.mark.asyncio
async def test_delivery_lease_releases_after_provider_exception(replay_db: Path) -> None:
    job = discovered_job("https://jobs.invalid/failing-delivery", source="source")
    job.notification_tier = "immediate"
    job.match_score = 99
    await save_jobs([job])
    failed = await process_pending_immediate_deliveries(
        discord_notifier=FakeDiscord(fail=True),
        telegram_notifier=FakeTelegram(),
    )
    assert failed.successes == ()
    succeeding = FakeDiscord()
    retried = await asyncio.wait_for(
        process_pending_immediate_deliveries(
            discord_notifier=succeeding,
            telegram_notifier=FakeTelegram(),
        ),
        timeout=2,
    )
    assert retried.selected_count == 1
    assert len(succeeding.calls) == 1


@pytest.mark.asyncio
async def test_delivery_lease_releases_after_database_exception(
    replay_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_get_pending = delivery_module.get_pending_delivery_jobs
    monkeypatch.setattr(
        delivery_module,
        "get_pending_delivery_jobs",
        AsyncMock(side_effect=RuntimeError("selection database unavailable")),
    )
    with pytest.raises(RuntimeError, match="selection database unavailable"):
        await process_pending_immediate_deliveries(
            discord_notifier=FakeDiscord(),
            telegram_notifier=FakeTelegram(),
        )

    monkeypatch.setattr(
        delivery_module,
        "get_pending_delivery_jobs",
        real_get_pending,
    )
    result = await asyncio.wait_for(
        process_pending_immediate_deliveries(
            discord_notifier=FakeDiscord(),
            telegram_notifier=FakeTelegram(),
        ),
        timeout=2,
    )
    assert result.selected_count == 0


@pytest.mark.asyncio
async def test_reverse_lease_order_fails_fast() -> None:
    async with immediate_delivery_lease():
        with pytest.raises(RuntimeError, match="during delivery"):
            async with job_ingestion_lease():
                pass
    async with job_ingestion_lease():
        with pytest.raises(RuntimeError, match="during job ingestion"):
            async with immediate_delivery_lease():
                pass


def test_phase5_scheduling_and_source_catalog_remain_unchanged() -> None:
    assert GROUP_BY_ID["source_group_a"].source_names == (
        "greenhouse",
        "ashby",
        "personio",
        "lever",
        "workable",
        "jsonld",
    )
    assert GROUP_BY_ID["source_group_b"].source_names == (
        "arbeitnow",
        "remotive",
        "himalayas",
        "remoteok",
        "idealist",
        "linkedin",
        "berlinstartupjobs",
    )
    assert "source_group_c" not in GROUP_BY_ID
    assert "stepstone" not in manual_all_source_names()
    scheduling = config.load_scheduling_settings(
        {
            "MAX_CONCURRENT_SOURCE_ADAPTERS": "2",
            "MAX_CONCURRENT_SOURCE_COMPONENTS": "3",
            "MAX_CONCURRENT_HTTP_REQUESTS": "4",
        }
    )
    assert scheduling.max_concurrent_source_adapters == 2
    assert scheduling.max_concurrent_source_components == 3
    assert scheduling.max_concurrent_http_requests == 4
