"""Focused Phase 2A persistence, presentation, and routing regressions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

import main
import notifiers.discord_notifier as discord_module
from filters.employment import (
    employment_display_lines,
    persisted_employment_display_lines,
)
from filters.match import compute_match_score
from models.job import Job
from notifiers.discord_notifier import DiscordNotifier
from notifiers.telegram_notifier import TelegramNotifier
from storage.database import init_db, job_from_row, save_jobs


_PHASE_1_JOBS_TABLE = """
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    content_hash TEXT,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT,
    is_remote INTEGER DEFAULT 1,
    remote_scope TEXT,
    url TEXT NOT NULL UNIQUE,
    description TEXT,
    salary TEXT,
    tags TEXT,
    source TEXT,
    is_ngo INTEGER DEFAULT 0,
    match_score INTEGER DEFAULT 0,
    posted_at TEXT,
    fetched_at TEXT NOT NULL,
    notified INTEGER DEFAULT 0
)
"""


def make_job(**overrides) -> Job:
    values = {
        "title": "Frontend Developer",
        "company": "Acme",
        "location": "Remote Germany",
        "remote_scope": "germany",
        "workplace_type": "remote",
        "url": "https://example.test/jobs/1",
        "description": "Build React and TypeScript products.",
        "source": "test",
    }
    values.update(overrides)
    return Job(**values)


@pytest.fixture
async def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "jobs.db"
    monkeypatch.setattr("storage.database.config.DATABASE_PATH", str(path))
    await init_db()
    return path


@pytest.mark.asyncio
async def test_phase1_database_migrates_safely_and_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "phase1.db"
    async with aiosqlite.connect(path) as db:
        await db.execute(_PHASE_1_JOBS_TABLE)
        await db.execute(
            """
            INSERT INTO jobs (
                id, content_hash, title, company, location, url, source, fetched_at
            ) VALUES ('old', 'hash', 'Frontend Developer', 'Old Co', 'Berlin',
                      'https://example.test/old', 'legacy',
                      '2026-08-01T10:00:00+00:00')
            """
        )
        await db.commit()

    monkeypatch.setattr("storage.database.config.DATABASE_PATH", str(path))
    await init_db()
    await init_db()

    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("PRAGMA table_info(jobs)")
        columns = {row[1] for row in await cursor.fetchall()}
        cursor = await db.execute("SELECT * FROM jobs WHERE id = 'old'")
        row = await cursor.fetchone()

    assert {
        "employment_relationship",
        "work_schedule",
        "contract_term",
        "weekly_hours",
        "contract_duration",
        "freelance_rate",
        "employment_reasons",
        "freelance_permission_required",
    } <= columns
    assert row is not None
    restored = job_from_row(row)
    assert restored.employment_relationship == "unknown"
    assert restored.work_schedule == "unknown"
    assert restored.contract_term == "unknown"
    assert restored.weekly_hours is None
    assert restored.contract_duration is None
    assert restored.freelance_rate is None
    assert restored.employment_reasons == []
    assert restored.freelance_permission_required is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("relationship", "schedule", "term", "hours"),
    [
        ("employee", "full_time", "permanent", 40),
        ("employee", "part_time", "fixed_term", 32),
        ("contract_employee", "full_time", "unknown", None),
        ("freelance", "part_time", "unknown", 20),
        ("freelance", "unknown", "unknown", None),
        ("unknown", "unknown", "unknown", None),
    ],
)
async def test_employment_fields_save_and_read_round_trip(
    database: Path,
    relationship: str,
    schedule: str,
    term: str,
    hours: int | None,
) -> None:
    suffix = f"{relationship}-{schedule}-{term}-{hours}"
    job = make_job(
        url=f"https://example.test/jobs/{suffix}",
        employment_relationship=relationship,
        work_schedule=schedule,
        contract_term=term,
        weekly_hours=hours,
        contract_duration="12 months" if term == "fixed_term" else None,
        freelance_rate="€600/day" if relationship == "freelance" else None,
        employment_reasons=["structured:relationship=" + relationship],
        freelance_permission_required=relationship == "freelance",
    )
    assert await save_jobs([job]) == [job]

    async with aiosqlite.connect(database) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job.id,))
        row = await cursor.fetchone()
    assert row is not None
    restored = job_from_row(row)

    assert restored.employment_relationship == relationship
    assert restored.work_schedule == schedule
    assert restored.contract_term == term
    assert restored.weekly_hours == hours
    assert restored.contract_duration == job.contract_duration
    assert restored.freelance_rate == job.freelance_rate
    assert restored.employment_reasons == job.employment_reasons
    assert restored.freelance_permission_required == (relationship == "freelance")


def test_employment_display_is_readable_bounded_and_omits_unknowns() -> None:
    known = make_job(
        employment_relationship="employee",
        work_schedule="part_time",
        contract_term="fixed_term",
        weekly_hours=32,
        contract_duration="12 months",
    )
    freelance = make_job(
        employment_relationship="freelance",
        freelance_rate="€600/day",
        freelance_permission_required=True,
    )
    unknown = make_job()

    assert employment_display_lines(known) == [
        "💼 Employee · Part-time · Fixed-term · 32h/week · 12 months"
    ]
    assert employment_display_lines(freelance) == [
        "💼 Freelance · €600/day",
        "⚠️ Freelance permission required",
    ]
    assert employment_display_lines(unknown) == []
    assert persisted_employment_display_lines(known.model_dump()) == employment_display_lines(known)


def test_cli_normal_and_explain_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job = make_job(
        employment_relationship="freelance",
        work_schedule="part_time",
        weekly_hours=20,
        freelance_permission_required=True,
        employment_reasons=["heuristic:title_tags:relationship=freelance"],
    )
    main._print_jobs([job])
    normal = capsys.readouterr().out
    assert "💼 Freelance · Part-time · 20h/week" in normal
    assert "⚠️ Freelance permission required" in normal
    assert "heuristic:title_tags" not in normal

    main._print_jobs([job], explain=True)
    explain = capsys.readouterr().out
    assert "heuristic:title_tags:relationship=freelance" in explain

    main._print_jobs([make_job()])
    unknown = capsys.readouterr().out
    assert "💼" not in unknown


def test_cli_rejection_explain_includes_employment_evidence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job = make_job(
        title="Frontend Intern",
        employment_relationship="internship",
        employment_reasons=["heuristic:title_tags:relationship=internship"],
    )
    main._print_rejections(
        [(job, "employment: employment relationship 'internship' is rejected by profile")]
    )
    output = capsys.readouterr().out
    assert "💼 Internship" in output
    assert "heuristic:title_tags:relationship=internship" in output
    assert "EMPLOYMENT" in output


def test_telegram_format_includes_employment_and_permission_marker() -> None:
    job = make_job(
        employment_relationship="freelance",
        work_schedule="part_time",
        weekly_hours=20,
        freelance_permission_required=True,
    )
    output = TelegramNotifier._format_job(job)
    assert "💼 Freelance · Part-time · 20h/week" in output
    assert "⚠️ Freelance permission required" in output
    assert "employment_relationship" not in output


@pytest.mark.asyncio
async def test_discord_format_includes_employment_and_permission_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_embeds: list[object] = []

    class FakeWebhook:
        def __init__(self, **kwargs) -> None:
            self.embed = None

        def add_embed(self, embed) -> None:
            self.embed = embed
            sent_embeds.append(embed)

        async def execute(self):
            return SimpleNamespace(status_code=204)

    monkeypatch.setattr(discord_module, "AsyncDiscordWebhook", FakeWebhook)
    job = make_job(
        employment_relationship="freelance",
        work_schedule="full_time",
        freelance_permission_required=True,
    )
    await DiscordNotifier(webhook_url="https://example.test/webhook")._send_single_job(job)

    assert len(sent_embeds) == 1
    description = getattr(sent_embeds[0], "description")
    assert "💼 Freelance · Full-time" in description
    assert "⚠️ Freelance permission required" in description


def test_employee_and_freelance_metadata_do_not_change_score_or_tier() -> None:
    shared = {
        "title": "Senior Frontend Developer",
        "description": "Build an NGO platform with React, TypeScript and Next.js.",
        "is_ngo": True,
    }
    employee = make_job(
        **shared,
        url="https://example.test/employee",
        employment_relationship="employee",
    )
    freelance = make_job(
        **shared,
        url="https://example.test/freelance",
        employment_relationship="freelance",
        freelance_permission_required=True,
    )

    assert compute_match_score(employee) == compute_match_score(freelance)
    assert employee.notification_tier == freelance.notification_tier


@pytest.mark.asyncio
async def test_permission_marker_does_not_suppress_or_reroute_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    employee = make_job(
        url="https://example.test/employee",
        employment_relationship="employee",
        notification_tier="immediate",
    )
    freelance = make_job(
        url="https://example.test/freelance",
        employment_relationship="freelance",
        freelance_permission_required=True,
        notification_tier="immediate",
    )
    discord_send = AsyncMock()
    telegram_send = AsyncMock()
    monkeypatch.setattr(main.config, "DISCORD_WEBHOOK_URL", "https://example.test/discord")
    monkeypatch.setattr(main.config, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(main.config, "TELEGRAM_CHAT_ID", "chat")
    monkeypatch.setattr(
        main,
        "DiscordNotifier",
        lambda: SimpleNamespace(send_jobs=discord_send),
    )
    monkeypatch.setattr(
        main,
        "TelegramNotifier",
        lambda: SimpleNamespace(send_jobs=telegram_send),
    )
    mark = AsyncMock()
    monkeypatch.setattr(main, "mark_notified", mark)

    await main._send_notifications([employee, freelance])

    discord_send.assert_awaited_once_with([employee, freelance])
    telegram_send.assert_awaited_once_with([employee, freelance])
    mark.assert_awaited_once_with([employee.id, freelance.id])
