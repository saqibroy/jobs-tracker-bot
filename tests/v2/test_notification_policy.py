"""Focused Phase 4B notification-policy and read-only simulation tests."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import filters.pipeline as pipeline
import main
import storage.database as database_module
from filters.notification_policy import (
    NotificationPolicy,
    apply_freelance_permission_ceiling,
    assign_notification_tier,
    build_notification_simulation,
    employment_bucket,
    format_notification_simulation,
    select_company_candidates,
    simulation_policy,
)
from filters.profile import DEFAULT_NOTIFICATION_SECTION, parse_notification_policy
from models.job import Job
from models.scan import RejectionCode

_FRESH_JOB_TIME = datetime.now(timezone.utc)


def make_job(suffix: str, *, score: int, **overrides) -> Job:
    values = {
        "title": f"Frontend Developer {suffix}",
        "company": "Acme",
        "location": "Remote Germany",
        "workplace_type": "remote",
        "remote_scope": "germany",
        "url": f"https://example.test/jobs/{suffix}",
        "description": "Build React and TypeScript products.",
        "source": "test",
        "match_score": score,
        "posted_at": _FRESH_JOB_TIME,
        "fetched_at": _FRESH_JOB_TIME,
    }
    values.update(overrides)
    return Job(**values)


def allow_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline, "passes_location_filter", lambda job: True)
    monkeypatch.setattr(pipeline, "classify_employment", lambda job: job)
    monkeypatch.setattr(pipeline, "employment_rejection_reason", lambda job, policy: None)
    monkeypatch.setattr(pipeline, "passes_role_filter", lambda job: True)
    monkeypatch.setattr(pipeline, "passes_stack_filter", lambda job: True)
    monkeypatch.setattr(pipeline, "passes_language_filter", lambda job: True)
    monkeypatch.setattr(pipeline, "passes_senior_filter", lambda job, settings: True)
    monkeypatch.setattr(pipeline, "passes_salary_filter", lambda job, settings: True)
    monkeypatch.setattr(pipeline, "classify_ngo", lambda job: job)
    monkeypatch.setattr(pipeline, "compute_match_score", lambda job: job.match_score)


def settings() -> SimpleNamespace:
    return SimpleNamespace(
        COMPANY_BLOCKLIST=[],
        FILTER_SENIOR_ONLY=False,
        MIN_SALARY_EUR=0,
        SOURCE_MAX_AGE_DAYS={},
        MAX_JOB_AGE_DAYS=14,
        MINIMUM_MATCH_SCORE=0,
    )


def policy(**overrides) -> NotificationPolicy:
    values = dict(DEFAULT_NOTIFICATION_SECTION)
    values.update(overrides)
    return parse_notification_policy(values)


def test_valid_notification_policy_owns_every_bounded_value() -> None:
    parsed = parse_notification_policy(DEFAULT_NOTIFICATION_SECTION)

    assert parsed == NotificationPolicy(**DEFAULT_NOTIFICATION_SECTION)
    assert parsed.immediate_score == 70
    assert parsed.digest_score == 45
    assert parsed.explore_score == 30
    assert parsed.daily_explore_enabled is True
    assert parsed.freelance_permission_max_tier == "digest"


@pytest.mark.parametrize(
    "overrides",
    [
        {"immediate_score": 101},
        {"immediate_score": 45},
        {"digest_score": 30},
        {"explore_score": -1},
        {"daily_explore_enabled": 1},
        {"explore_hour_utc": -1},
        {"explore_hour_utc": 24},
        {"immediate_max_items": 0},
        {"digest_max_items": 26},
        {"explore_max_items": True},
        {"pending_max_age_days": 0},
        {"pending_max_age_days": 31},
        {"max_jobs_per_company": 0},
        {"max_jobs_per_company": 6},
        {"freelance_permission_max_tier": "none"},
    ],
)
def test_notification_policy_rejects_invalid_values(overrides: dict) -> None:
    values = dict(DEFAULT_NOTIFICATION_SECTION)
    values.update(overrides)
    with pytest.raises(ValueError, match="invalid notification policy"):
        parse_notification_policy(values)


def test_notification_policy_accepts_outer_valid_boundaries() -> None:
    parsed = policy(
        immediate_score=100,
        digest_score=99,
        explore_score=0,
        explore_hour_utc=23,
        immediate_max_items=1,
        digest_max_items=25,
        pending_max_age_days=30,
        max_jobs_per_company=5,
    )
    assert (parsed.immediate_score, parsed.digest_score, parsed.explore_score) == (
        100,
        99,
        0,
    )


@pytest.mark.parametrize(
    ("score", "expected"),
    [(70, "immediate"), (69, "digest"), (45, "digest"), (44, "explore"), (30, "explore"), (29, "none")],
)
def test_tier_assignment_exact_boundaries(score: int, expected: str) -> None:
    assert assign_notification_tier(score, policy()) == expected


def test_tier_assignment_disables_explore() -> None:
    assert assign_notification_tier(44, policy(daily_explore_enabled=False)) == "none"


@pytest.mark.parametrize(
    ("tier", "ceiling", "expected"),
    [
        ("immediate", "digest", "digest"),
        ("digest", "digest", "digest"),
        ("explore", "digest", "explore"),
        ("none", "digest", "none"),
        ("immediate", "immediate", "immediate"),
        ("immediate", "explore", "explore"),
        ("digest", "explore", "explore"),
    ],
)
def test_freelance_permission_ceiling(
    tier: str,
    ceiling: str,
    expected: str,
) -> None:
    job = make_job(
        f"{tier}-{ceiling}",
        score=80,
        employment_relationship="freelance",
        freelance_permission_required=True,
    )
    assert apply_freelance_permission_ceiling(
        job, tier, policy(freelance_permission_max_tier=ceiling)
    ) == expected


@pytest.mark.parametrize(
    "overrides",
    [
        {"employment_relationship": "freelance", "freelance_permission_required": False},
        {"employment_relationship": "employee", "freelance_permission_required": False},
    ],
)
def test_freelance_ceiling_does_not_change_unmarked_jobs(overrides: dict) -> None:
    job = make_job("unmarked", score=80, **overrides)
    assert apply_freelance_permission_ceiling(job, "immediate", policy()) == "immediate"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {
                "employment_relationship": "freelance",
                "work_schedule": "part_time",
                "contract_term": "fixed_term",
            },
            "freelance",
        ),
        (
            {"work_schedule": "part_time", "contract_term": "fixed_term"},
            "part_time",
        ),
        ({"employment_relationship": "contract_employee"}, "contract_or_fixed_term"),
        ({"contract_term": "fixed_term"}, "contract_or_fixed_term"),
        ({}, "standard"),
    ],
)
def test_employment_bucket_is_mutually_exclusive(overrides: dict, expected: str) -> None:
    assert employment_bucket(make_job(expected, score=70, **overrides)) == expected


def test_company_cap_never_lets_lower_tier_diversity_displace_immediate() -> None:
    jobs = [
        make_job("82", score=82, notification_tier="immediate"),
        make_job("75", score=75, notification_tier="immediate"),
        make_job(
            "35",
            score=35,
            notification_tier="explore",
            employment_relationship="freelance",
        ),
    ]
    result = select_company_candidates(jobs, 2, mode="diversity")

    assert [job.match_score for job in result.selected] == [82, 75]
    assert [job.match_score for job in result.excluded] == [35]


def test_company_cap_prefers_same_tier_employment_diversity() -> None:
    jobs = [
        make_job("82", score=82, notification_tier="immediate"),
        make_job(
            "77",
            score=77,
            notification_tier="immediate",
            work_schedule="part_time",
        ),
        make_job("75", score=75, notification_tier="immediate"),
    ]
    result = select_company_candidates(jobs, 2, mode="diversity")

    assert [job.match_score for job in result.selected] == [82, 77]
    assert employment_bucket(result.selected[1]) == "part_time"


@pytest.mark.parametrize("cap", range(1, 6))
def test_company_cap_one_through_five_is_one_overall_limit(cap: int) -> None:
    jobs = [
        make_job(
            str(index),
            score=90 - index,
            notification_tier=("immediate", "digest", "explore", "none")[index % 4],
            employment_relationship="freelance" if index % 3 == 0 else "unknown",
            work_schedule="part_time" if index % 3 == 1 else "unknown",
            contract_term="fixed_term" if index % 3 == 2 else "unknown",
        )
        for index in range(8)
    ]
    result = select_company_candidates(jobs, cap, mode="diversity")

    assert len(result.selected) == cap
    assert len(result.excluded) == len(jobs) - cap


def test_company_cap_all_same_bucket_uses_deterministic_global_rank() -> None:
    now = datetime(2026, 8, 7, tzinfo=timezone.utc)
    jobs = [
        make_job("old", score=80, notification_tier="immediate", posted_at=now - timedelta(days=1), id="z"),
        make_job("new-b", score=80, notification_tier="immediate", posted_at=now, id="b"),
        make_job("new-a", score=80, notification_tier="immediate", posted_at=now, id="a"),
    ]
    result = select_company_candidates(jobs, 2, mode="diversity")

    assert [job.id for job in result.selected] == ["a", "b"]


def test_pipeline_emits_one_company_cap_rejection_per_excluded_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_pipeline(monkeypatch)
    jobs = [
        make_job("alpha-82", score=82, source="alpha"),
        make_job("beta-77", score=77, source="beta", work_schedule="part_time"),
        make_job("beta-75", score=75, source="beta"),
    ]
    report = pipeline.run_filter_pipeline(
        jobs,
        settings=settings(),
        notification_policy=policy(max_jobs_per_company=2),
    )

    assert report.accepted_count == 2
    assert report.rejection_counts[RejectionCode.COMPANY_CAP] == 1
    assert report.per_source["beta"].rejection_counts[RejectionCode.COMPANY_CAP] == 1
    report.validate_accounting()


@pytest.mark.parametrize("enabled", [True, False])
def test_policy_registers_daily_explore_only_when_enabled(enabled: bool) -> None:
    class Scheduler:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def add_job(self, *args, **kwargs) -> None:
            self.calls.append((args, kwargs))

    scheduler = Scheduler()
    selected_policy = policy(daily_explore_enabled=enabled, explore_hour_utc=17)

    main._register_notification_delivery_jobs(scheduler, selected_policy)

    by_id = {kwargs["id"]: (args, kwargs) for args, kwargs in scheduler.calls}
    assert "digest" in by_id
    assert ("explore" in by_id) is enabled
    if enabled:
        trigger = by_id["explore"][0][1]
        assert "hour='17'" in str(trigger)
        assert "minute='0'" in str(trigger)


def test_pipeline_can_retain_hard_eligible_pre_cap_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_pipeline(monkeypatch)
    jobs = [make_job(str(index), score=80 - index) for index in range(3)]

    report = pipeline.run_filter_pipeline(
        jobs,
        settings=settings(),
        max_jobs_per_company=2,
        apply_company_cap=False,
    )

    assert report.accepted_jobs == jobs
    assert report.rejection_counts[pipeline.RejectionCode.COMPANY_CAP] == 0
    report.validate_accounting()


def test_simulation_reports_required_aggregate_and_a_b_c_cap_evidence() -> None:
    jobs = [
        make_job("80", score=80, source="alpha"),
        make_job("60", score=60, source="alpha", work_schedule="part_time"),
        make_job(
            "35",
            score=35,
            source="beta",
            employment_relationship="freelance",
            freelance_permission_required=True,
        ),
        make_job("20", score=20, source="beta", company="Other"),
        make_job("10", score=10, source="gamma", company="Third"),
    ]

    report = build_notification_simulation(jobs)

    assert report["hard_eligible_pre_cap_total"] == 5
    assert report["score_bands"] == {
        "70-100": 1,
        "45-69": 1,
        "30-44": 1,
        "15-29": 1,
        "0-14": 1,
    }
    assert report["source_distribution"] == {"alpha": 2, "beta": 2, "gamma": 1}
    assert report["freelance_count"] == 1
    assert report["freelance_permission_required_count"] == 1
    assert report["current_score_only_cap2_exclusions"] == 1
    scenarios = report["scenarios"]
    assert set(scenarios) == {
        "A-current-cap2",
        *(f"B-aggressive-cap{cap}" for cap in range(2, 6)),
        *(f"C-conservative-cap{cap}" for cap in range(2, 6)),
    }
    assert scenarios["A-current-cap2"]["explore"] == 0
    assert scenarios["B-aggressive-cap5"]["explore"] == 1
    assert scenarios["C-conservative-cap5"]["explore"] == 1
    assert "visibility" in scenarios["B-aggressive-cap2"]
    assert "same_tier_diversity_tradeoffs" in scenarios["B-aggressive-cap2"]

    output = format_notification_simulation(report)
    assert "description" not in output.lower()
    assert "Build React" not in output
    assert "A-current-cap2" in output


def test_simulation_reports_same_tier_diversity_score_tradeoff() -> None:
    jobs = [
        make_job("82", score=82),
        make_job("80", score=80),
        make_job("77-part", score=77, work_schedule="part_time"),
    ]

    report = build_notification_simulation(jobs)
    tradeoff = report["scenarios"]["C-conservative-cap2"][
        "same_tier_diversity_tradeoffs"
    ]

    assert tradeoff["added_by_tier"] == {"immediate": 1}
    assert tradeoff["removed_by_tier"] == {"immediate": 1}
    assert tradeoff["selected_score_delta"] == -3
    assert tradeoff["tier_counts_preserved"] is True


@pytest.mark.asyncio
async def test_simulator_fetches_once_and_never_uses_write_or_delivery_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_pipeline(monkeypatch)
    source = SimpleNamespace(
        name="test",
        safe_fetch=AsyncMock(return_value=[make_job("one", score=70)]),
    )
    monkeypatch.setattr(main.config, "MAX_CONCURRENT_SOURCES", 1)
    monkeypatch.setattr(main.config, "SOURCE_MAX_AGE_DAYS", {})
    monkeypatch.setattr(main.config, "MAX_JOB_AGE_DAYS", 14)
    monkeypatch.setattr(main.config, "MINIMUM_MATCH_SCORE", 0)
    monkeypatch.setattr(main.config, "COMPANY_BLOCKLIST", [])
    monkeypatch.setattr(main.config, "FILTER_SENIOR_ONLY", False)
    monkeypatch.setattr(main.config, "MIN_SALARY_EUR", 0)

    forbidden = AsyncMock(side_effect=AssertionError("write/delivery path called"))
    monkeypatch.setattr(main, "init_db", forbidden)
    monkeypatch.setattr(main, "save_jobs", forbidden)
    monkeypatch.setattr(main, "persist_scan_metrics", forbidden)
    monkeypatch.setattr(main, "process_pending_immediate_deliveries", forbidden)
    monkeypatch.setattr(
        main,
        "append_sniffed_candidates",
        lambda jobs: (_ for _ in ()).throw(AssertionError("discovery write called")),
    )

    report = await main.run_notification_simulation([source])

    assert report["hard_eligible_pre_cap_total"] == 1
    source.safe_fetch.assert_awaited_once()
    forbidden.assert_not_awaited()


@pytest.mark.asyncio
async def test_dry_run_does_not_write_ats_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = SimpleNamespace(name="empty", safe_fetch=AsyncMock(return_value=[]))
    discovery = AsyncMock()
    monkeypatch.setattr(main.config, "ENABLE_ATS_SNIFFING", True)
    monkeypatch.setattr(main, "append_sniffed_candidates", discovery)

    assert await main.run_scan([source], dry_run=True) == []
    discovery.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_simulation_does_not_create_or_mutate_database_or_zoho_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
) -> None:
    database_path = tmp_path / "sentinel.db"
    zoho_path = tmp_path / "zoho-state.json"
    zoho_path.write_bytes(b"phase4b-zoho-sentinel")
    if existing:
        database_path.write_bytes(b"phase4b-database-sentinel")
        database_hash = hashlib.sha256(database_path.read_bytes()).hexdigest()
        database_mtime = database_path.stat().st_mtime_ns
    zoho_hash = hashlib.sha256(zoho_path.read_bytes()).hexdigest()
    zoho_mtime = zoho_path.stat().st_mtime_ns
    monkeypatch.setattr(database_module.config, "DATABASE_PATH", str(database_path))
    monkeypatch.setattr(main.config, "DATABASE_PATH", str(database_path))
    monkeypatch.setattr(main.config, "ZOHO_OAUTH_TOKEN_FILE", str(zoho_path))
    source = SimpleNamespace(name="empty", safe_fetch=AsyncMock(return_value=[]))

    report = await main.run_notification_simulation([source])

    assert report["hard_eligible_pre_cap_total"] == 0
    if existing:
        assert hashlib.sha256(database_path.read_bytes()).hexdigest() == database_hash
        assert database_path.stat().st_mtime_ns == database_mtime
    else:
        assert not database_path.exists()
    assert hashlib.sha256(zoho_path.read_bytes()).hexdigest() == zoho_hash
    assert zoho_path.stat().st_mtime_ns == zoho_mtime
