from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from models.job import Job
from models.scan import RejectionCode, SanitizedSourceIssue, SourceFetchOutcome, SourceStatus
from sources.http_budget import SourceHttpBudget
from tools.source_yield_probe import (
    MAX_LABEL_CHARS,
    MAX_OBSERVATION_BYTES,
    aggregate_report,
    append_observation,
    build_observation,
    open_production_db_readonly,
)


def _database(path: Path, rows: list[tuple[str, str]] | None = None) -> Path:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE jobs (content_hash TEXT, source TEXT)")
        connection.executemany(
            "INSERT INTO jobs (content_hash, source) VALUES (?, ?)", rows or []
        )
    return path


def _job(label: str, *, score: int = 85, tier: str = "immediate") -> Job:
    return Job(
        title=f"Frontend Engineer {label}",
        company=f"Company {label}",
        location="Berlin, Germany",
        is_remote=False,
        workplace_type="onsite",
        url=f"https://example.test/jobs/{label}",
        source="berlinstartupjobs",
        match_score=score,
        notification_tier=tier,
    )


def _outcome(jobs: list[Job], status: SourceStatus = SourceStatus.HEALTHY) -> SourceFetchOutcome:
    now = datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc)
    return SourceFetchOutcome(
        "berlinstartupjobs", jobs, status, now, now, 12, ()
    )


def _budget(attempts: int = 2) -> SourceHttpBudget:
    budget = SourceHttpBudget(4)
    budget.total_attempts = attempts
    budget.observed_peak = 1 if attempts else 0
    return budget


def test_production_database_is_opened_explicitly_read_only(tmp_path: Path) -> None:
    database = _database(tmp_path / "jobs.db")

    with open_production_db_readonly(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone() == (0,)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute(
                "INSERT INTO jobs (content_hash, source) VALUES ('x', 'test')"
            )


def test_observation_does_not_write_production_database(tmp_path: Path) -> None:
    job = _job("immutable")
    database = _database(tmp_path / "jobs.db", [(job.content_hash, "arbeitnow")])
    before = database.read_bytes()

    build_observation(_outcome([job]), [job], {}, _budget(), database)

    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone() == (1,)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall() == [("jobs",)]


def test_observation_marks_existing_and_unique_accepted_jobs(tmp_path: Path) -> None:
    existing = _job("existing", score=88)
    unique = _job("unique", score=67, tier="digest")
    database = _database(
        tmp_path / "jobs.db",
        [(existing.content_hash, "arbeitnow"), (existing.content_hash, "linkedin")],
    )

    observation = build_observation(
        _outcome([existing, unique]),
        [existing, unique],
        {RejectionCode.LOCATION: 0},
        _budget(),
        database,
    )

    by_hash = {item["content_hash"]: item for item in observation["accepted"]}
    assert by_hash[existing.content_hash]["already_in_production"] is True
    assert by_hash[existing.content_hash]["unique_at_observation"] is False
    assert by_hash[existing.content_hash]["production_sources"] == [
        "arbeitnow",
        "linkedin",
    ]
    assert by_hash[unique.content_hash]["already_in_production"] is False
    assert by_hash[unique.content_hash]["unique_at_observation"] is True


def test_report_deduplicates_hashes_across_observations(tmp_path: Path) -> None:
    job = _job("repeat")
    database = _database(tmp_path / "jobs.db")
    output = tmp_path / "observations.jsonl"
    first = build_observation(
        _outcome([job]), [job], {}, _budget(2), database,
        observed_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
    )
    second = build_observation(
        _outcome([job]), [job], {}, _budget(3), database,
        observed_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )
    append_observation(output, first)
    append_observation(output, second)

    report = aggregate_report(output)

    assert report["observation_count"] == 2
    assert report["union_accepted_jobs"] == 1
    assert report["union_unique_at_observation_jobs"] == 1
    assert report["jobs_repeatedly_observed"]["count"] == 1
    assert report["jobs_repeatedly_observed"]["items"][0]["observation_count"] == 2


def test_report_aggregates_tiers_companies_existing_and_http(tmp_path: Path) -> None:
    existing = _job("existing", score=90)
    unique = _job("unique", score=65, tier="digest")
    database = _database(tmp_path / "jobs.db", [(existing.content_hash, "remotive")])
    output = tmp_path / "observations.jsonl"
    observation = build_observation(
        _outcome([existing, unique]), [existing, unique], {}, _budget(3), database
    )
    append_observation(output, observation)

    report = aggregate_report(output)

    assert report["successful_observations"] == 1
    assert report["failed_observations"] == 0
    assert report["unique_companies"] == 2
    assert report["notification_tiers"] == {
        "immediate": 1,
        "digest": 1,
        "explore": 0,
        "none": 0,
    }
    assert report["jobs_already_represented"]["count"] == 1
    assert report["http"]["attempts_total"] == 3


def test_empty_and_malformed_jsonl_are_reported_without_crashing(tmp_path: Path) -> None:
    output = tmp_path / "observations.jsonl"
    output.write_text("", encoding="utf-8")
    assert aggregate_report(output)["observation_count"] == 0

    output.write_text("\nnot-json\n{}\n", encoding="utf-8")

    report = aggregate_report(output)

    assert report["observation_count"] == 0
    assert report["malformed_line_count"] == 3
    assert report["first_observation"] is None
    assert report["union_accepted_jobs"] == 0


def test_source_failure_observation_and_report(tmp_path: Path) -> None:
    database = _database(tmp_path / "jobs.db")
    output = tmp_path / "observations.jsonl"
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    outcome = SourceFetchOutcome(
        "berlinstartupjobs",
        [],
        SourceStatus.NETWORK_ERROR,
        now,
        now,
        20,
        (SanitizedSourceIssue(SourceStatus.NETWORK_ERROR, "network unavailable"),),
    )
    observation = build_observation(outcome, [], {}, _budget(3), database)
    append_observation(output, observation)

    report = aggregate_report(output)

    assert observation["status"] == "network_error"
    assert observation["source_error"] == "network unavailable"
    assert report["successful_observations"] == 0
    assert report["failed_observations"] == 1
    assert report["source_failures"] == {"network_error": 1}


def test_observation_output_is_bounded_and_contains_no_description_or_url(
    tmp_path: Path,
) -> None:
    jobs = [_job(str(index)) for index in range(260)]
    for job in jobs:
        job.company = "C" * 1_000
        job.title = "T" * 1_000
        job.description = "private body " * 10_000
    database = _database(tmp_path / "jobs.db")
    output = tmp_path / "observations.jsonl"

    observation = build_observation(_outcome(jobs), jobs, {}, _budget(), database)
    append_observation(output, observation)
    encoded = output.read_bytes()

    assert len(encoded) <= MAX_OBSERVATION_BYTES + 1
    assert len(observation["accepted"]) == 200
    assert observation["accepted_truncated_count"] == 60
    assert all(len(item["company"]) <= MAX_LABEL_CHARS for item in observation["accepted"])
    assert b"private body" not in encoded
    assert b"https://" not in encoded
