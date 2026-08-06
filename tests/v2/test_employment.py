"""Focused Phase 2A model, classifier, profile-policy, and pipeline tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import filters.pipeline as pipeline
from filters.employment import (
    EmploymentStructuredInput,
    classify_employment,
    employment_rejection_reason,
    extract_weekly_hours,
)
from filters.profile import (
    DEFAULT_EMPLOYMENT_SECTION,
    EmploymentPolicy,
    parse_employment_policy,
)
from filters.role import role_rejection_reason
from models.job import (
    MAX_EMPLOYMENT_DETAIL_LENGTH,
    MAX_EMPLOYMENT_REASON_LENGTH,
    MAX_EMPLOYMENT_REASONS,
    Job,
)
from models.scan import RejectionCode


def make_job(
    title: str = "Frontend Developer",
    description: str = "Build web products with React and TypeScript.",
    **overrides,
) -> Job:
    values = {
        "title": title,
        "company": "Acme",
        "location": "Remote worldwide",
        "remote_scope": "worldwide",
        "url": f"https://example.test/{abs(hash((title, description, tuple(sorted(overrides)))))}",
        "description": description,
        "source": "test",
    }
    values.update(overrides)
    return Job(**values)


@pytest.mark.parametrize(
    ("relationship", "schedule", "term"),
    [
        ("employee", "full_time", "permanent"),
        ("employee", "part_time", "permanent"),
        ("employee", "part_time", "fixed_term"),
        ("employee", "full_time", "fixed_term"),
        ("contract_employee", "full_time", "unknown"),
        ("freelance", "part_time", "unknown"),
        ("freelance", "full_time", "unknown"),
        ("freelance", "unknown", "unknown"),
        ("unknown", "unknown", "unknown"),
    ],
)
def test_independent_model_combinations_round_trip(
    relationship: str,
    schedule: str,
    term: str,
) -> None:
    original = make_job(
        employment_relationship=relationship,
        work_schedule=schedule,
        contract_term=term,
        weekly_hours=20 if schedule == "part_time" else None,
    )
    restored = Job.model_validate(original.model_dump())

    assert restored.employment_relationship == relationship
    assert restored.work_schedule == schedule
    assert restored.contract_term == term
    assert restored.weekly_hours == original.weekly_hours


def test_legacy_employment_defaults_are_safe() -> None:
    job = make_job()
    assert job.employment_relationship == "unknown"
    assert job.work_schedule == "unknown"
    assert job.contract_term == "unknown"
    assert job.weekly_hours is None
    assert job.contract_duration is None
    assert job.freelance_rate is None
    assert job.employment_reasons == []
    assert job.freelance_permission_required is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("employment_relationship", "consultant"),
        ("work_schedule", "flexible"),
        ("contract_term", "temporary"),
    ],
)
def test_invalid_employment_literals_fail_clearly(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match=field):
        make_job(**{field: value})


@pytest.mark.parametrize("value", [0, -1, 169, 1000, True])
def test_invalid_weekly_hours_fail(value: object) -> None:
    with pytest.raises(ValidationError, match="weekly_hours"):
        make_job(weekly_hours=value)


def test_employment_evidence_and_optional_text_are_bounded() -> None:
    job = make_job(
        contract_duration="  " + "x" * 300 + "  ",
        freelance_rate="\n" + "y" * 300,
        employment_reasons=[f"  reason {index}  " + "z" * 300 for index in range(30)],
    )

    assert len(job.contract_duration or "") == MAX_EMPLOYMENT_DETAIL_LENGTH
    assert len(job.freelance_rate or "") == MAX_EMPLOYMENT_DETAIL_LENGTH
    assert len(job.employment_reasons) == MAX_EMPLOYMENT_REASONS
    assert all(len(reason) <= MAX_EMPLOYMENT_REASON_LENGTH for reason in job.employment_reasons)
    assert all("\n" not in reason for reason in job.employment_reasons)


@pytest.mark.parametrize(
    ("title", "description", "relationship", "schedule", "term", "hours"),
    [
        (
            "Frontend Developer",
            "This is direct employment on a full-time permanent basis.",
            "employee", "full_time", "permanent", None,
        ),
        (
            "Frontend Developer",
            "Direct employment in a part-time position.",
            "employee", "part_time", "unknown", None,
        ),
        (
            "Frontend Developer",
            "Unbefristete Anstellung in Teilzeit, 20 Stunden/Woche.",
            "employee", "part_time", "permanent", 20,
        ),
        (
            "Frontend Developer",
            "Direct employment, part-time, on a 12-month contract.",
            "employee", "part_time", "fixed_term", None,
        ),
        (
            "Frontend Developer",
            "Direct employment, full-time, 6 month fixed-term role.",
            "employee", "full_time", "fixed_term", None,
        ),
        (
            "Contract Employee Frontend Developer",
            "Employment through a staffing agency, Vollzeit.",
            "contract_employee", "full_time", "unknown", None,
        ),
        (
            "Freelance Frontend Developer",
            "Build React applications on a B2B basis.",
            "freelance", "unknown", "unknown", None,
        ),
        (
            "Freelance Frontend Developer — Full-time",
            "Independent contractor engagement.",
            "freelance", "full_time", "unknown", None,
        ),
        (
            "Freiberufliche Frontend-Entwicklung",
            "Selbstständig in Teilzeit, Tagessatz: 700 EUR.",
            "freelance", "part_time", "unknown", None,
        ),
        (
            "Werkstudent:in Frontend Development",
            "20 h/week using React.",
            "working_student", "part_time", "unknown", 20,
        ),
        (
            "Software Engineering Internship",
            "Praktikum in Vollzeit.",
            "internship", "full_time", "unknown", None,
        ),
        (
            "Frontend Developer",
            "Join the internal platform team on a contract.",
            "unknown", "unknown", "unknown", None,
        ),
    ],
)
def test_deterministic_relationship_schedule_and_term_classification(
    title: str,
    description: str,
    relationship: str,
    schedule: str,
    term: str,
    hours: int | None,
) -> None:
    job = classify_employment(make_job(title, description))
    assert job.employment_relationship == relationship
    assert job.work_schedule == schedule
    assert job.contract_term == term
    assert job.weekly_hours == hours


def test_unknown_and_conflicting_relationship_evidence_stays_unknown() -> None:
    unknown = classify_employment(make_job(description="A web role with React."))
    conflict = classify_employment(
        make_job(description="This role is both a freelance internship opportunity.")
    )

    assert unknown.employment_relationship == "unknown"
    assert unknown.work_schedule == "unknown"
    assert unknown.contract_term == "unknown"
    assert conflict.employment_relationship == "unknown"
    assert any("relationship=conflict" in reason for reason in conflict.employment_reasons)


def test_intern_is_word_bound_and_does_not_match_internal() -> None:
    job = classify_employment(
        make_job(description="Develop internal tools for our international employee platform.")
    )
    assert job.employment_relationship == "unknown"


def test_title_and_tags_precede_description_per_dimension() -> None:
    job = classify_employment(
        make_job(
            title="Full-time Frontend Developer",
            description="The description incorrectly labels this as part-time and fixed-term.",
            tags=["permanent"],
        )
    )
    assert job.work_schedule == "full_time"
    assert job.contract_term == "permanent"


def test_same_scope_schedule_conflict_stays_unknown() -> None:
    job = classify_employment(
        make_job(description="This position may be either full-time or part-time.")
    )
    assert job.work_schedule == "unknown"
    assert any("schedule=conflict" in reason for reason in job.employment_reasons)


@pytest.mark.parametrize(
    ("text", "expected", "evidence"),
    [
        ("20h/week", 20, "value=20h/week"),
        ("20 h/week", 20, "value=20h/week"),
        ("20 hours per week", 20, "value=20h/week"),
        ("20 Stunden/Woche", 20, "value=20h/week"),
        ("30–32 hours/week", 32, "range=30-32h/week"),
        ("32 Std./Woche", 32, "value=32h/week"),
        ("Frontend Developer (32h)", 32, "value=32h/week"),
    ],
)
def test_weekly_hours_supported_forms(text: str, expected: int, evidence: str) -> None:
    scope = "title_tags" if "Developer" in text else "description"
    assert extract_weekly_hours(text, scope=scope) == (expected, evidence)


@pytest.mark.parametrize(
    "text",
    [
        "Salary: €80,000 per year",
        "30 vacation days",
        "At least 5 years of experience",
        "Start date 2026-08-07",
        "80% remote",
        "A 12-month contract",
        "Job ID 32h",
        "Reference 123456",
        "The arbitrary standalone number is 42",
        "The identifier is 32h for internal tracking",
    ],
)
def test_weekly_hours_rejects_false_positives(text: str) -> None:
    assert extract_weekly_hours(text) == (None, None)


def test_explicit_schedule_label_beats_hours_but_hours_are_retained() -> None:
    job = classify_employment(
        make_job(description="Full-time direct employment at 20 hours per week.")
    )
    assert job.work_schedule == "full_time"
    assert job.weekly_hours == 20


def test_33_or_more_hours_does_not_infer_full_time() -> None:
    job = classify_employment(make_job(description="Work 40 hours per week."))
    assert job.weekly_hours == 40
    assert job.work_schedule == "unknown"


def test_freelance_rate_requires_explicit_rate_context() -> None:
    freelance = classify_employment(
        make_job(description="B2B contractor engagement at €90/hour.")
    )
    employee_salary = classify_employment(
        make_job(description="Direct employment paying €90/hour or €7,000 per month.")
    )
    annual = classify_employment(
        make_job(description="Annual salary range €70,000–€90,000.")
    )

    assert freelance.freelance_rate == "€90/hour"
    assert freelance.employment_relationship == "freelance"
    assert employee_salary.freelance_rate is None
    assert annual.freelance_rate is None


def test_structured_values_win_independently_and_heuristics_fill_unknowns() -> None:
    job = classify_employment(
        make_job(
            description=(
                "Freelance part-time engagement on a 12-month fixed-term contract."
            )
        ),
        EmploymentStructuredInput(
            employment_relationship="employee",
            work_schedule="full_time",
        ),
    )

    assert job.employment_relationship == "employee"
    assert job.work_schedule == "full_time"
    assert job.contract_term == "fixed_term"
    assert job.contract_duration == "12 months"


def test_structured_term_suppresses_conflicting_duration_evidence() -> None:
    job = classify_employment(
        make_job(description="This is a 12-month fixed-term contract."),
        EmploymentStructuredInput(contract_term="permanent"),
    )
    assert job.contract_term == "permanent"
    assert job.contract_duration is None


def test_malformed_structured_values_are_ignored_per_dimension() -> None:
    job = classify_employment(
        make_job(description="Freelance work in part-time hours."),
        {
            "employment_relationship": "unsupported",
            "work_schedule": "full_time",
            "contract_term": {"bad": "shape"},
            "weekly_hours": 999,
        },
    )
    assert job.employment_relationship == "freelance"
    assert job.work_schedule == "full_time"
    assert job.contract_term == "unknown"
    assert job.weekly_hours is None


def policy(**overrides: object) -> EmploymentPolicy:
    values = dict(DEFAULT_EMPLOYMENT_SECTION)
    values.update(overrides)
    return parse_employment_policy(values)


def test_valid_profile_policy_and_derived_freelance_marker() -> None:
    current = policy()
    freelance = make_job(employment_relationship="freelance")
    employee = make_job(employment_relationship="employee")

    assert employment_rejection_reason(freelance, current) is None
    assert freelance.freelance_permission_required is True
    assert employment_rejection_reason(employee, current) is None
    assert employee.freelance_permission_required is False


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"accepted_relationships": ["employee", "volunteer"]}, "unsupported"),
        ({"accepted_schedules": ["full_time", "flexible"]}, "unsupported"),
        (
            {
                "accepted_relationships": ["employee", "internship"],
                "rejected_relationships": ["internship"],
            },
            "overlap",
        ),
        (
            {"preferred_weekly_hours_min": 41, "preferred_weekly_hours_max": 40},
            "must not exceed",
        ),
        ({"preferred_weekly_hours_min": 0}, "1 to 168"),
    ],
)
def test_invalid_profile_policy_fails_clearly(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        policy(**overrides)


def test_policy_accepts_unknown_and_rejects_students_and_interns() -> None:
    current = policy()
    assert employment_rejection_reason(make_job(), current) is None
    assert "rejected" in (
        employment_rejection_reason(
            make_job(employment_relationship="working_student"), current
        )
        or ""
    )
    assert "rejected" in (
        employment_rejection_reason(
            make_job(employment_relationship="internship"), current
        )
        or ""
    )


def test_policy_rejects_known_relationship_or_schedule_not_accepted() -> None:
    relationship_policy = policy(
        accepted_relationships=["unknown"],
        rejected_relationships=[],
    )
    schedule_policy = policy(accepted_schedules=["unknown"])

    assert "not accepted" in (
        employment_rejection_reason(
            make_job(employment_relationship="employee"), relationship_policy
        )
        or ""
    )
    assert "work schedule" in (
        employment_rejection_reason(
            make_job(work_schedule="full_time"), schedule_policy
        )
        or ""
    )


def test_preferred_hours_are_advisory_only() -> None:
    job = make_job(employment_relationship="employee", weekly_hours=10)
    assert employment_rejection_reason(job, policy()) is None
    assert "policy:weekly_hours_below_preference" in job.employment_reasons


def settings() -> SimpleNamespace:
    return SimpleNamespace(
        COMPANY_BLOCKLIST=[],
        FILTER_SENIOR_ONLY=False,
        MIN_SALARY_EUR=0,
        SOURCE_MAX_AGE_DAYS={},
        MAX_JOB_AGE_DAYS=14,
        MINIMUM_MATCH_SCORE=0,
    )


def allow_non_employment_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline, "passes_location_filter", lambda job: True)
    monkeypatch.setattr(pipeline, "passes_role_filter", lambda job: True)
    monkeypatch.setattr(pipeline, "passes_stack_filter", lambda job: True)
    monkeypatch.setattr(pipeline, "passes_language_filter", lambda job: True)
    monkeypatch.setattr(pipeline, "classify_ngo", lambda job: job)
    monkeypatch.setattr(pipeline, "compute_match_score", lambda job: 50)


def test_employment_gate_owns_one_terminal_rejection_before_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_non_employment_gates(monkeypatch)
    role_calls = 0

    def role_gate(job: Job) -> bool:
        nonlocal role_calls
        role_calls += 1
        return False

    monkeypatch.setattr(pipeline, "passes_role_filter", role_gate)
    result = pipeline.run_filter_pipeline(
        [make_job("Software Engineering Intern", source="alpha")],
        settings=settings(),
        employment_policy=policy(),
    )

    assert result.accepted_count == 0
    assert result.rejection_counts[RejectionCode.EMPLOYMENT_RELATIONSHIP] == 1
    assert result.rejection_counts[RejectionCode.ROLE] == 0
    assert role_calls == 0
    result.validate_accounting()


def test_location_remains_the_earlier_terminal_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_non_employment_gates(monkeypatch)
    monkeypatch.setattr(pipeline, "passes_location_filter", lambda job: False)
    result = pipeline.run_filter_pipeline(
        [make_job("Frontend Internship")],
        settings=settings(),
        employment_policy=policy(),
    )
    assert result.rejection_counts[RejectionCode.LOCATION] == 1
    assert result.rejection_counts[RejectionCode.EMPLOYMENT_RELATIONSHIP] == 0


def test_employment_accounting_holds_overall_and_per_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_non_employment_gates(monkeypatch)
    jobs = [
        make_job("Frontend Intern", source="alpha"),
        make_job("Frontend Working Student", source="beta", company="Beta"),
        make_job(
            "Frontend Developer",
            description="Direct employment, full-time.",
            source="beta",
            company="Gamma",
        ),
    ]
    result = pipeline.run_filter_pipeline(
        jobs,
        settings=settings(),
        employment_policy=policy(),
    )

    assert result.raw_count == 3
    assert result.accepted_count == 1
    assert result.rejection_counts[RejectionCode.EMPLOYMENT_RELATIONSHIP] == 2
    assert result.per_source["alpha"].raw_count == 1
    assert result.per_source["alpha"].rejection_counts[
        RejectionCode.EMPLOYMENT_RELATIONSHIP
    ] == 1
    assert result.per_source["beta"].raw_count == 2
    assert result.per_source["beta"].accepted_count == 1
    result.validate_accounting()


def test_role_filter_no_longer_hardcodes_employment_categories() -> None:
    assert role_rejection_reason(make_job("Frontend Intern")) is None
    assert role_rejection_reason(make_job("Frontend Working Student")) is None
    assert role_rejection_reason(make_job("Junior Frontend Developer")) is not None
