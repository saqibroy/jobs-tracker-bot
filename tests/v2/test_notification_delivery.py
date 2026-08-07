"""Focused Phase 4A notification tier, receipt, and retry tests."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import discord_webhook
import pytest
from pydantic import ValidationError
from telegram.error import TelegramError

import health
import main
import filters.pipeline as pipeline
import notifiers.delivery as delivery_module
import notifiers.discord_notifier as discord_module
import notifiers.telegram_notifier as telegram_module
import storage.database as database_module
from filters.match import compute_match_score
from models.job import Job
from models.scan import empty_routing_counts
from notifiers.base import DeliverySuccess, resolve_discord_destination
from notifiers.delivery import (
    build_digest_payload,
    process_pending_digest_delivery,
    process_pending_immediate_deliveries,
)
from notifiers.discord_notifier import DiscordNotifier
from notifiers.telegram_notifier import TelegramNotifier
from storage.database import (
    get_delivery_receipts,
    get_pending_delivery_jobs,
    get_weekly_ngo_jobs,
    init_db,
    record_delivery_receipts,
    save_jobs,
)


def make_job(suffix: str = "1", **overrides) -> Job:
    values = {
        "title": f"Frontend Developer {suffix}",
        "company": "Acme",
        "location": "Remote Germany",
        "workplace_type": "remote",
        "remote_scope": "germany",
        "url": f"https://example.test/jobs/{suffix}",
        "description": "Build React and TypeScript products.",
        "source": "test",
        "notification_tier": "immediate",
        "match_score": 80,
        "posted_at": datetime.now(timezone.utc),
        "fetched_at": datetime.now(timezone.utc),
    }
    values.update(overrides)
    return Job(**values)


@pytest.fixture
async def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "jobs.db"
    monkeypatch.setattr(database_module.config, "DATABASE_PATH", str(path))
    await init_db()
    return path


@pytest.mark.parametrize("tier", ["none", "explore", "digest", "immediate"])
def test_four_notification_tiers_validate(tier: str) -> None:
    assert make_job(notification_tier=tier).notification_tier == tier


def test_unknown_notification_tier_rejected() -> None:
    with pytest.raises(ValidationError):
        make_job(notification_tier="urgent")


@pytest.mark.parametrize(
    ("title", "description", "workplace_type", "expected_score", "expected_tier"),
    [
        ("Primary Developer", "react typescript python", "unknown", 70, "immediate"),
        ("Secondary Developer", "react", "remote", 45, "digest"),
        ("Secondary Developer", "react", "unknown", 40, "none"),
    ],
)
def test_phase4a_thresholds_never_assign_explore(
    monkeypatch: pytest.MonkeyPatch,
    title: str,
    description: str,
    workplace_type: str,
    expected_score: int,
    expected_tier: str,
) -> None:
    def profile_list(section: str, key: str) -> list[str]:
        values = {
            ("roles", "primary"): ["primary"],
            ("roles", "secondary"): ["secondary"],
            ("stack", "core"): ["react", "typescript", "python"],
            ("stack", "supporting"): [],
            ("stack", "incompatible"): [],
            ("mission", "keywords"): [],
        }
        return values[(section, key)]

    monkeypatch.setattr("filters.match.profile_list", profile_list)
    monkeypatch.setattr(
        "filters.match.profile_value",
        lambda section, key, default: 70 if key == "immediate_score" else 45,
    )
    job = make_job(
        title=title,
        description=description,
        workplace_type=workplace_type,
        is_ngo=False,
    )
    assert compute_match_score(job) == expected_score
    assert job.notification_tier == expected_tier
    assert job.notification_tier != "explore"


@pytest.mark.asyncio
async def test_phase3_database_migrates_receipts_idempotently_and_preserves_legacy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "phase3.db"
    async with aiosqlite.connect(path) as db:
        await db.execute(database_module._CREATE_TABLE)
        await db.execute(
            """
            INSERT INTO jobs (
                id, content_hash, title, company, url, source, fetched_at, notified
            ) VALUES ('old', 'hash', 'Developer', 'Old Co',
                      'https://example.test/old', 'legacy',
                      '2026-08-01T10:00:00+00:00', 1)
            """
        )
        await db.commit()
    monkeypatch.setattr(database_module.config, "DATABASE_PATH", str(path))

    await init_db()
    await init_db()

    async with aiosqlite.connect(path) as db:
        columns = {
            row[1]
            for row in await (await db.execute(
                "PRAGMA table_info(job_delivery_receipts)"
            )).fetchall()
        }
        indexes = await (await db.execute(
            "PRAGMA index_list(job_delivery_receipts)"
        )).fetchall()
        notified = (await (await db.execute(
            "SELECT notified FROM jobs WHERE id = 'old'"
        )).fetchone())[0]
        receipt_count = (await (await db.execute(
            "SELECT COUNT(*) FROM job_delivery_receipts"
        )).fetchone())[0]

    assert columns == {"job_id", "delivery_kind", "destination", "delivered_at"}
    assert any(row[2] for row in indexes)  # primary-key uniqueness index
    assert notified == 1
    assert receipt_count == 0


@pytest.mark.asyncio
async def test_receipt_writes_are_idempotent(database: Path) -> None:
    job = make_job()
    await save_jobs([job])
    success = DeliverySuccess(job.id, "discord_general")

    assert await record_delivery_receipts("immediate", [success]) == 1
    assert await record_delivery_receipts("immediate", [success]) == 0
    assert len(await get_delivery_receipts(job.id)) == 1


@pytest.mark.asyncio
async def test_legacy_notified_rows_are_suppressed_without_fabricated_receipts(
    database: Path,
) -> None:
    immediate = make_job("immediate")
    digest = make_job("digest", notification_tier="digest")
    await save_jobs([immediate, digest])
    async with aiosqlite.connect(database) as db:
        await db.execute("UPDATE jobs SET notified = 1")
        await db.commit()

    assert await get_pending_delivery_jobs("immediate", "discord_general") == []
    assert await get_pending_delivery_jobs("digest", "discord_general") == []
    assert await get_delivery_receipts() == []


@pytest.mark.parametrize(
    ("is_ngo", "general", "ngo", "expected"),
    [
        (False, True, False, "discord_general"),
        (True, True, True, "discord_ngo"),
        (True, True, False, "discord_general"),
        (False, False, True, None),
    ],
)
def test_discord_destination_resolution(
    is_ngo: bool,
    general: bool,
    ngo: bool,
    expected: str | None,
) -> None:
    assert resolve_discord_destination(
        make_job(is_ngo=is_ngo),
        general_configured=general,
        ngo_configured=ngo,
    ) == expected


class FakeDiscord:
    def __init__(
        self,
        *,
        general: bool = True,
        ngo: bool = False,
        successful_ids: set[str] | None = None,
        grouped_success: bool = True,
    ) -> None:
        self.general_configured = general
        self.ngo_configured = ngo
        self.successful_ids = successful_ids
        self.grouped_success = grouped_success
        self.calls: list[list[str]] = []
        self.grouped_calls: list[list[str]] = []

    def has_destination(self, destination: str) -> bool:
        return self.general_configured if destination == "discord_general" else self.ngo_configured

    async def send_jobs(self, jobs: list[Job], **_kwargs) -> list[DeliverySuccess]:
        self.calls.append([job.id for job in jobs])
        successes = self.successful_ids
        result = []
        for job in jobs:
            if successes is not None and job.id not in successes:
                continue
            destination = "discord_ngo" if job.is_ngo and self.ngo_configured else "discord_general"
            result.append(DeliverySuccess(job.id, destination))
        return result

    async def send_grouped_digest(self, payload, *, total_jobs: int):
        del total_jobs
        self.grouped_calls.append([job.id for job in payload.jobs])
        if not self.grouped_success:
            return []
        return [DeliverySuccess(job.id, "discord_general") for job in payload.jobs]


class FakeTelegram:
    def __init__(self, *, configured: bool = True, successful_ids: set[str] | None = None):
        self.configured = configured
        self.successful_ids = successful_ids
        self.calls: list[list[str]] = []

    async def send_jobs(self, jobs: list[Job]) -> list[DeliverySuccess]:
        self.calls.append([job.id for job in jobs])
        successes = self.successful_ids
        return [
            DeliverySuccess(job.id, "telegram")
            for job in jobs
            if successes is None or job.id in successes
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("discord_ok", "telegram_ok", "expected"),
    [
        (True, True, {"discord_general", "telegram"}),
        (True, False, {"discord_general"}),
        (False, True, {"telegram"}),
        (False, False, set()),
    ],
)
async def test_per_destination_partial_success_contract(
    database: Path,
    discord_ok: bool,
    telegram_ok: bool,
    expected: set[str],
) -> None:
    job = make_job()
    await save_jobs([job])
    discord = FakeDiscord(successful_ids={job.id} if discord_ok else set())
    telegram = FakeTelegram(successful_ids={job.id} if telegram_ok else set())

    await process_pending_immediate_deliveries(
        discord_notifier=discord, telegram_notifier=telegram
    )

    receipts = await get_delivery_receipts(job.id)
    assert {item["destination"] for item in receipts} == expected


@pytest.mark.asyncio
async def test_retry_sends_only_missing_telegram_obligation(database: Path) -> None:
    job = make_job()
    await save_jobs([job])
    first_discord = FakeDiscord(successful_ids={job.id})
    first_telegram = FakeTelegram(successful_ids=set())
    await process_pending_immediate_deliveries(
        discord_notifier=first_discord, telegram_notifier=first_telegram
    )

    retry_discord = FakeDiscord(successful_ids={job.id})
    retry_telegram = FakeTelegram(successful_ids={job.id})
    await process_pending_immediate_deliveries(
        discord_notifier=retry_discord, telegram_notifier=retry_telegram
    )

    assert retry_discord.calls == []
    assert retry_telegram.calls == [[job.id]]
    assert {r["destination"] for r in await get_delivery_receipts(job.id)} == {
        "discord_general",
        "telegram",
    }


@pytest.mark.asyncio
async def test_retry_sends_only_missing_discord_obligation(database: Path) -> None:
    job = make_job()
    await save_jobs([job])
    await process_pending_immediate_deliveries(
        discord_notifier=FakeDiscord(successful_ids=set()),
        telegram_notifier=FakeTelegram(successful_ids={job.id}),
    )
    retry_discord = FakeDiscord(successful_ids={job.id})
    retry_telegram = FakeTelegram(successful_ids={job.id})

    await process_pending_immediate_deliveries(
        discord_notifier=retry_discord, telegram_notifier=retry_telegram
    )

    assert retry_discord.calls == [[job.id]]
    assert retry_telegram.calls == []


@pytest.mark.asyncio
async def test_ngo_uses_one_discord_destination_and_configuration_change_does_not_resend(
    database: Path,
) -> None:
    job = make_job(is_ngo=True)
    await save_jobs([job])
    discord = FakeDiscord(general=True, ngo=True)
    await process_pending_immediate_deliveries(
        discord_notifier=discord,
        telegram_notifier=FakeTelegram(configured=False),
    )
    assert discord.calls == [[job.id]]
    assert [r["destination"] for r in await get_delivery_receipts(job.id)] == [
        "discord_ngo"
    ]

    pending_after_fallback_change = await get_pending_delivery_jobs(
        "immediate",
        "discord_general",
        ngo_webhook_configured=False,
    )
    assert pending_after_fallback_change == []


@pytest.mark.asyncio
async def test_successful_siblings_receipt_while_failed_job_remains_pending(
    database: Path,
) -> None:
    jobs = [make_job(str(index)) for index in range(3)]
    await save_jobs(jobs)
    discord = FakeDiscord(successful_ids={jobs[0].id, jobs[2].id})
    await process_pending_immediate_deliveries(
        discord_notifier=discord,
        telegram_notifier=FakeTelegram(configured=False),
    )

    assert {r["job_id"] for r in await get_delivery_receipts()} == {
        jobs[0].id,
        jobs[2].id,
    }
    pending = await get_pending_delivery_jobs("immediate", "discord_general")
    assert [job.id for job in pending] == [jobs[1].id]


@pytest.mark.asyncio
async def test_discord_http_failure_omits_only_failed_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter([204, 500, 204])

    class Webhook:
        def __init__(self, **_kwargs):
            pass

        def add_embed(self, _embed) -> None:
            pass

        async def execute(self):
            return SimpleNamespace(status_code=next(responses))

    monkeypatch.setattr(discord_module, "AsyncDiscordWebhook", Webhook)
    monkeypatch.setattr(discord_module, "_DELAY_BETWEEN_EMBEDS", 0)
    jobs = [make_job(str(index)) for index in range(3)]
    successes = await DiscordNotifier(
        webhook_url="https://example.test/general"
    ).send_jobs(jobs, include_batch_header=False)

    assert [item.job_id for item in successes] == [jobs[0].id, jobs[2].id]


@pytest.mark.asyncio
async def test_telegram_returns_exact_successful_siblings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Bot:
        def __init__(self, **_kwargs):
            pass

        async def send_message(self, **kwargs) -> None:
            if "Bad" in kwargs["text"]:
                raise TelegramError("failed")

    monkeypatch.setattr(telegram_module, "Bot", Bot)
    monkeypatch.setattr(telegram_module, "_DELAY_BETWEEN_MESSAGES", 0)
    jobs = [make_job("good-a"), make_job("bad", title="Bad Job"), make_job("good-c")]
    successes = await TelegramNotifier(
        bot_token="token", chat_id="chat"
    ).send_jobs(jobs)
    assert [item.job_id for item in successes] == [jobs[0].id, jobs[2].id]


@pytest.mark.asyncio
async def test_production_scan_retries_immediate_when_no_rows_are_saved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = SimpleNamespace(name="empty", safe_fetch=AsyncMock(return_value=[]))
    monkeypatch.setattr(main.config, "ENABLE_ATS_SNIFFING", False)
    monkeypatch.setattr(main, "init_db", AsyncMock())
    monkeypatch.setattr(main, "filter_unseen", AsyncMock(return_value=[]))
    monkeypatch.setattr(main, "persist_scan_metrics", AsyncMock())
    monkeypatch.setattr(main, "get_latest_source_statuses", AsyncMock(return_value=[]))
    delivery = AsyncMock()
    monkeypatch.setattr(main, "_send_notifications", delivery)

    assert await main.run_scan([source], dry_run=False) == []
    delivery.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_pending_selection_is_deterministic_and_excludes_stale_rows(
    database: Path,
) -> None:
    now = datetime.now(timezone.utc)
    jobs = [
        make_job("low", match_score=70, posted_at=now),
        make_job("newer", match_score=80, posted_at=now - timedelta(hours=1)),
        make_job("older", match_score=80, posted_at=now - timedelta(days=2)),
        make_job(
            "stale",
            match_score=100,
            posted_at=now - timedelta(days=15),
            fetched_at=now - timedelta(days=15),
        ),
    ]
    await save_jobs(jobs)
    pending = await get_pending_delivery_jobs(
        "immediate", "discord_general", now=now
    )
    assert [job.id for job in pending] == [
        jobs[1].id,
        jobs[2].id,
        jobs[0].id,
    ]


@pytest.mark.asyncio
async def test_digest_carries_over_more_than_fifteen_and_ignores_six_hour_boundary(
    database: Path,
) -> None:
    now = datetime.now(timezone.utc)
    jobs = [
        make_job(
            f"digest-{index}",
            notification_tier="digest",
            fetched_at=now - timedelta(days=7),
            posted_at=now - timedelta(days=7),
            match_score=60 - index,
        )
        for index in range(17)
    ]
    stale = make_job(
        "digest-stale",
        notification_tier="digest",
        fetched_at=now - timedelta(days=15),
        posted_at=now - timedelta(days=15),
        match_score=99,
    )
    await save_jobs([*jobs, stale])
    discord = FakeDiscord()

    first = await process_pending_digest_delivery(
        total_jobs=17, discord_notifier=discord
    )
    second = await process_pending_digest_delivery(
        total_jobs=17, discord_notifier=discord
    )

    assert (first.selected_count, len(first.successes)) == (15, 15)
    assert (second.selected_count, len(second.successes)) == (2, 2)
    assert len(await get_delivery_receipts()) == 17
    assert await get_delivery_receipts(stale.id) == []


def test_grouped_payload_retains_exact_rendered_membership() -> None:
    jobs = [
        make_job(
            str(index), notification_tier="digest", match_score=80 - index
        )
        for index in range(3)
    ]
    one = build_digest_payload(jobs[:1])
    bounded = build_digest_payload(
        jobs, max_description_chars=len(one.description)
    )
    assert [job.id for job in bounded.jobs] == [jobs[0].id]
    assert len(bounded.description) <= len(one.description)


@pytest.mark.asyncio
async def test_digest_receipts_only_rendered_jobs(
    database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = [
        make_job(
            str(index), notification_tier="digest", match_score=80 - index
        )
        for index in range(3)
    ]
    await save_jobs(jobs)
    original_builder = build_digest_payload
    one = original_builder(jobs[:1])
    monkeypatch.setattr(
        delivery_module,
        "build_digest_payload",
        lambda selected: original_builder(
            selected, max_description_chars=len(one.description)
        ),
    )

    result = await process_pending_digest_delivery(
        total_jobs=3, discord_notifier=FakeDiscord()
    )

    assert result.selected_count == 3
    assert result.included_count == 1
    assert [r["job_id"] for r in await get_delivery_receipts()] == [jobs[0].id]


@pytest.mark.asyncio
async def test_failed_grouped_digest_records_no_receipts(database: Path) -> None:
    await save_jobs([make_job(notification_tier="digest")])
    result = await process_pending_digest_delivery(
        total_jobs=1,
        discord_notifier=FakeDiscord(grouped_success=False),
    )
    assert result.selected_count == 1
    assert result.successes == ()
    assert await get_delivery_receipts() == []


def test_explore_routing_metrics_are_additive() -> None:
    counts = empty_routing_counts()
    assert counts == {
        "immediate": 0,
        "digest": 0,
        "explore": 0,
        "diagnostic": 0,
    }
    health.set_scan_summary(
        {"immediate": 1, "digest": 2, "diagnostic": 3}
    )
    assert health.get_scan_summary()["explore"] == 0


@pytest.mark.asyncio
async def test_scan_aggregates_explore_separately_from_none_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explore = make_job("explore", company="Explore Co")
    diagnostic = make_job("none", company="Diagnostic Co")
    source = SimpleNamespace(
        name="test", safe_fetch=AsyncMock(return_value=[explore, diagnostic])
    )
    monkeypatch.setattr(pipeline, "passes_location_filter", lambda job: True)
    monkeypatch.setattr(pipeline, "passes_role_filter", lambda job: True)
    monkeypatch.setattr(pipeline, "passes_stack_filter", lambda job: True)
    monkeypatch.setattr(pipeline, "passes_language_filter", lambda job: True)
    monkeypatch.setattr(pipeline, "classify_ngo", lambda job: job)

    def score(job: Job) -> int:
        job.notification_tier = "explore" if job is explore else "none"
        return 30 if job is explore else 20

    monkeypatch.setattr(pipeline, "compute_match_score", score)
    monkeypatch.setattr(main.config, "ENABLE_ATS_SNIFFING", False)
    monkeypatch.setattr(main, "init_db", AsyncMock())
    monkeypatch.setattr(main, "filter_unseen", AsyncMock(side_effect=lambda jobs: jobs))
    monkeypatch.setattr(main, "save_jobs", AsyncMock(side_effect=lambda jobs: jobs))
    persist = AsyncMock()
    monkeypatch.setattr(main, "persist_scan_metrics", persist)
    monkeypatch.setattr(main, "get_latest_source_statuses", AsyncMock(return_value=[]))
    monkeypatch.setattr(main, "_send_notifications", AsyncMock())

    await main.run_scan([source], dry_run=False)
    summary = persist.await_args.args[0]
    assert summary.routing_counts["explore"] == 1
    assert summary.routing_counts["diagnostic"] == 1


@pytest.mark.asyncio
async def test_daily_status_renders_additive_explore_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    class Webhook:
        def __init__(self, **_kwargs):
            pass

        def add_embed(self, embed) -> None:
            captured["embed"] = embed

        async def execute(self):
            return SimpleNamespace(status_code=204)

    monkeypatch.setattr(discord_webhook, "AsyncDiscordWebhook", Webhook)
    monkeypatch.setattr(main.config, "DISCORD_WEBHOOK_URL", "https://example.test")
    monkeypatch.setattr(main, "get_total_count", AsyncMock(return_value=10))
    monkeypatch.setattr(
        main,
        "get_stats",
        AsyncMock(return_value={"new_24h": 2}),
    )
    health.set_scan_summary(
        {
            "raw": 5,
            "accepted": 4,
            "rejected": 1,
            "immediate": 1,
            "digest": 1,
            "explore": 2,
            "diagnostic": 0,
        }
    )

    await main.send_daily_status_summary()

    fields = str(captured["embed"].fields)
    assert "explore" in fields
    assert "`2` explore" in fields


@pytest.mark.asyncio
async def test_weekly_ngo_digest_query_ignores_one_time_receipts(database: Path) -> None:
    job = make_job(is_ngo=True, notification_tier="digest")
    await save_jobs([job])
    await record_delivery_receipts(
        "digest", [DeliverySuccess(job.id, "discord_general")]
    )
    first = await get_weekly_ngo_jobs()
    second = await get_weekly_ngo_jobs()
    assert [row["id"] for row in first] == [job.id]
    assert [row["id"] for row in second] == [job.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_dry_run_never_creates_or_mutates_delivery_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
) -> None:
    path = tmp_path / "sentinel.db"
    if existing:
        path.write_bytes(b"phase4a-sentinel")
        before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        before_mtime = path.stat().st_mtime_ns
    monkeypatch.setattr(database_module.config, "DATABASE_PATH", str(path))
    monkeypatch.setattr(main.config, "ENABLE_ATS_SNIFFING", False)
    source = SimpleNamespace(name="empty", safe_fetch=AsyncMock(return_value=[]))

    assert await main.run_scan([source], dry_run=True) == []

    if existing:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash
        assert path.stat().st_mtime_ns == before_mtime
    else:
        assert not path.exists()
