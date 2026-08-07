"""Focused Phase 3 persistence and CLI presentation tests."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

import main
from filters.language import evaluate_language
from filters.profile import LanguagePolicy
from models.job import Job
from storage.database import init_db, job_from_row, save_jobs


_PHASE_2B_JOBS_TABLE = """
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    content_hash TEXT,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT,
    is_remote INTEGER DEFAULT 1,
    workplace_type TEXT DEFAULT 'unknown',
    eligible_countries TEXT DEFAULT '[]',
    eligible_regions TEXT DEFAULT '[]',
    remote_scope TEXT,
    url TEXT NOT NULL UNIQUE,
    description TEXT,
    salary TEXT,
    tags TEXT,
    source TEXT,
    is_ngo INTEGER DEFAULT 0,
    match_score INTEGER DEFAULT 0,
    match_breakdown TEXT DEFAULT '{}',
    match_reasons TEXT DEFAULT '[]',
    eligibility_status TEXT DEFAULT 'unknown',
    eligibility_reasons TEXT DEFAULT '[]',
    notification_tier TEXT DEFAULT 'none',
    employment_relationship TEXT NOT NULL DEFAULT 'unknown',
    work_schedule TEXT NOT NULL DEFAULT 'unknown',
    contract_term TEXT NOT NULL DEFAULT 'unknown',
    weekly_hours INTEGER,
    contract_duration TEXT,
    freelance_rate TEXT,
    employment_reasons TEXT NOT NULL DEFAULT '[]',
    freelance_permission_required INTEGER NOT NULL DEFAULT 0,
    posted_at TEXT,
    fetched_at TEXT NOT NULL,
    notified INTEGER DEFAULT 0
)
"""


def make_job(**overrides) -> Job:
    values = {
        "title": "Frontend Developer",
        "company": "Acme",
        "location": "Remote worldwide",
        "remote_scope": "worldwide",
        "workplace_type": "remote",
        "url": "https://example.test/jobs/phase3",
        "description": "Build React and TypeScript web products.",
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
async def test_phase2b_database_migrates_safely_and_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "phase2b.db"
    async with aiosqlite.connect(path) as db:
        await db.execute(_PHASE_2B_JOBS_TABLE)
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
        columns_cursor = await db.execute("PRAGMA table_info(jobs)")
        columns = {row[1] for row in await columns_cursor.fetchall()}
        row_cursor = await db.execute("SELECT * FROM jobs WHERE id = 'old'")
        row = await row_cursor.fetchone()

    assert {
        "posting_language",
        "german_requirement_status",
        "german_requirement_level",
        "language_reasons",
    } <= columns
    assert row is not None
    restored = job_from_row(row)
    assert restored.posting_language == "unknown"
    assert restored.german_requirement_status == "unknown"
    assert restored.german_requirement_level == "unknown"
    assert restored.language_reasons == []


@pytest.mark.asyncio
async def test_language_fields_round_trip(database: Path) -> None:
    job = make_job(
        posting_language="de",
        german_requirement_status="compatible",
        german_requirement_level="b1",
        language_reasons=["posting_language=de", "german_requirement=b1:required"],
    )
    assert await save_jobs([job]) == [job]

    async with aiosqlite.connect(database) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job.id,))
        row = await cursor.fetchone()
    assert row is not None
    restored = job_from_row(row)
    assert restored.posting_language == "de"
    assert restored.german_requirement_status == "compatible"
    assert restored.german_requirement_level == "b1"
    assert restored.language_reasons == job.language_reasons


def test_malformed_language_reasons_fall_back_safely() -> None:
    job = make_job()
    row = job.model_dump()
    row["language_reasons"] = "{malformed"
    restored = job_from_row(row)
    assert restored.language_reasons == []


def test_normal_cli_is_quiet_for_ordinary_english_job(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job = make_job(
        posting_language="en",
        german_requirement_status="unspecified",
        language_reasons=["posting_language=en"],
    )
    main._print_jobs([job])
    assert "Language:" not in capsys.readouterr().out


def test_cli_shows_german_posting_and_requirement(
    capsys: pytest.CaptureFixture[str],
) -> None:
    posting = make_job(
        posting_language="de",
        german_requirement_status="unspecified",
        language_reasons=["posting_language=de"],
    )
    main._print_jobs([posting])
    assert "Language: German posting · German requirement unspecified" in capsys.readouterr().out

    required = make_job(
        url="https://example.test/jobs/b1",
        posting_language="en",
        german_requirement_status="compatible",
        german_requirement_level="b1",
        language_reasons=["posting_language=en", "german_requirement=b1:required"],
    )
    main._print_jobs([required], explain=True)
    output = capsys.readouterr().out
    assert "Language: German B1 required" in output
    assert "german_requirement=b1:required" in output


def test_rejected_explain_shows_candidate_maximum(
    capsys: pytest.CaptureFixture[str],
) -> None:
    policy = LanguagePolicy(
        max_german_level="b1", accepted_languages=frozenset({"en"})
    )
    job = make_job(description="German B2 required")
    assert evaluate_language(job, policy) is False
    main._print_rejections(
        [(job, "Language: German B2 required; candidate max B1")]
    )
    output = capsys.readouterr().out
    assert "Language: German B2 required; candidate max B1" in output
    assert "german_requirement=b2:required" in output


def test_ambiguous_alternative_cli_text(capsys: pytest.CaptureFixture[str]) -> None:
    policy = LanguagePolicy(
        max_german_level="b1", accepted_languages=frozenset({"en"})
    )
    job = make_job(description="German B2 or fluent English")
    assert evaluate_language(job, policy) is True
    main._print_jobs([job])
    assert (
        "Language: German B2 or explicit English proficiency requirement; "
        "compatibility uncertain"
    ) in capsys.readouterr().out
