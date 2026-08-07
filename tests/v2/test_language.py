"""Focused Phase 3 model, policy, evaluator, pipeline, and routing tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langdetect.lang_detect_exception import LangDetectException
from pydantic import ValidationError

from filters.employment import EmploymentStructuredInput, classify_employment
from filters.language import detect_posting_language, evaluate_language
from filters.match import compute_match_score
from filters.pipeline import run_filter_pipeline
from filters.profile import LanguagePolicy, parse_language_policy
from models.job import (
    MAX_LANGUAGE_REASON_LENGTH,
    MAX_LANGUAGE_REASONS,
    Job,
)
from models.scan import RejectionCode


def policy(maximum: str = "b1", languages: frozenset[str] | None = None) -> LanguagePolicy:
    return LanguagePolicy(
        max_german_level=maximum,
        accepted_languages=languages if languages is not None else frozenset({"en"}),
    )


def make_job(description: str = "Build React and TypeScript web products.", **overrides) -> Job:
    values = {
        "title": "Frontend Developer",
        "company": "Acme",
        "location": "Remote worldwide",
        "remote_scope": "worldwide",
        "workplace_type": "remote",
        "url": "https://example.test/jobs/"
        + str(abs(hash((description, repr(sorted(overrides.items())))))),
        "description": description,
        "source": "test",
    }
    values.update(overrides)
    return Job(**values)


def evaluate(description: str, maximum: str = "b1") -> Job:
    job = make_job(description)
    evaluate_language(job, policy(maximum))
    return job


def test_language_model_defaults_and_normalization() -> None:
    job = make_job(
        posting_language=" DE ",
        german_requirement_status=" COMPATIBLE ",
        german_requirement_level=" B1 ",
    )
    assert job.posting_language == "de"
    assert job.german_requirement_status == "compatible"
    assert job.german_requirement_level == "b1"
    assert job.language_reasons == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("posting_language", "fr"),
        ("german_requirement_status", "rejected"),
        ("german_requirement_level", "c3"),
    ],
)
def test_invalid_language_literals_fail(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match=field):
        make_job(**{field: value})


def test_language_reasons_are_unique_and_bounded() -> None:
    job = make_job(
        language_reasons=[f" reason-{index} " + "x" * 200 for index in range(20)]
    )
    assert len(job.language_reasons) == MAX_LANGUAGE_REASONS
    assert len(set(job.language_reasons)) == MAX_LANGUAGE_REASONS
    assert all(len(reason) <= MAX_LANGUAGE_REASON_LENGTH for reason in job.language_reasons)


@pytest.mark.parametrize("maximum", ["A1", "a2", "B1", "b2", "C1", "c2"])
def test_language_policy_normalizes_supported_levels(maximum: str) -> None:
    parsed = parse_language_policy(
        {"max_german_level": maximum, "accepted_languages": ["EN"]}
    )
    assert parsed.max_german_level == maximum.lower()
    assert parsed.accepted_languages == frozenset({"en"})


@pytest.mark.parametrize(
    ("section", "message"),
    [
        (None, "candidate.max_german_level is required"),
        ({"accepted_languages": ["en"]}, "candidate.max_german_level is required"),
        ({"max_german_level": "native"}, "must be one of"),
        ({"max_german_level": "B1", "accepted_languages": "en"}, "must be a list"),
    ],
)
def test_language_policy_fails_clearly(section: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_language_policy(section)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("title", "description", "expected"),
    [
        ("Frontend Developer", "Build accessible React products for customers worldwide.", "en"),
        ("Frontend-Entwickler", "Du entwickelst moderne Webanwendungen für unser Berliner Team.", "de"),
        ("Développeur frontend", "Vous développez des applications accessibles pour nos clients.", "other"),
        ("Dev", "Short", "unknown"),
    ],
)
def test_posting_language_detection(title: str, description: str, expected: str) -> None:
    assert detect_posting_language(make_job(description, title=title)) == expected


def test_posting_language_detection_failure_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_: str) -> str:
        raise LangDetectException(0, "forced")

    monkeypatch.setattr("filters.language.detect", fail)
    assert detect_posting_language(make_job("Long enough text for language detection.")) == "unknown"


def test_posting_detection_is_deterministic() -> None:
    job = make_job("Du entwickelst moderne Webanwendungen für unser Berliner Team.")
    assert {detect_posting_language(job) for _ in range(5)} == {"de"}


@pytest.mark.parametrize("level", ["A1", "A2", "B1"])
def test_required_compatible_cefr(level: str) -> None:
    job = evaluate(f"German {level} required")
    assert job.german_requirement_status == "compatible"
    assert job.german_requirement_level == level.lower()


@pytest.mark.parametrize(
    ("description", "maximum", "passes", "status", "level"),
    [
        ("German B2 required", "b1", False, "incompatible", "b2"),
        ("German B2 required", "b2", True, "compatible", "b2"),
        ("German C1 required", "b2", False, "incompatible", "c1"),
        ("German C1 required", "c1", True, "compatible", "c1"),
        ("German C2 required", "c1", False, "incompatible", "c2"),
        ("German C2 required", "c2", True, "compatible", "c2"),
        ("Fluent German", "b1", False, "incompatible", "fluent"),
        ("Fluent German", "b2", True, "compatible", "fluent"),
        ("Business fluent German required", "b2", False, "incompatible", "business_fluent"),
        ("professional working proficiency in German", "c1", True, "compatible", "business_fluent"),
        ("Verhandlungssicher Deutsch", "b2", False, "incompatible", "business_fluent"),
        ("Verhandlungssicher Deutsch", "c1", True, "compatible", "business_fluent"),
        ("Native German required", "c2", False, "incompatible", "native"),
        ("German mother tongue required", "c2", False, "incompatible", "native"),
        ("Muttersprache Deutsch erforderlich", "c2", False, "incompatible", "native"),
        ("Deutsch als Muttersprache", "c2", False, "incompatible", "native"),
    ],
)
def test_required_cefr_and_descriptor_policy(
    description: str,
    maximum: str,
    passes: bool,
    status: str,
    level: str,
) -> None:
    job = make_job(description)
    assert evaluate_language(job, policy(maximum)) is passes
    assert job.german_requirement_status == status
    assert job.german_requirement_level == level


@pytest.mark.parametrize(
    ("description", "level"),
    [
        ("German B2 preferred", "b2"),
        ("German C1 nice-to-have", "c1"),
        ("Fluent German would be a plus", "fluent"),
        ("Business-fluent German preferred", "business_fluent"),
        ("Verhandlungssicheres Deutsch von Vorteil", "business_fluent"),
        ("Native German nice to have", "native"),
        ("German would be beneficial", "unknown"),
    ],
)
def test_optional_context_always_passes(description: str, level: str) -> None:
    job = make_job(description)
    assert evaluate_language(job, policy("a1")) is True
    assert job.german_requirement_status == "optional"
    assert job.german_requirement_level == level


@pytest.mark.parametrize(
    "description",
    [
        "German not required",
        "no German required",
        "German is not necessary",
        "Deutsch nicht erforderlich",
        "keine Deutschkenntnisse erforderlich",
        "Deutschkenntnisse sind keine Voraussetzung",
    ],
)
def test_explicit_negation_passes_as_optional_none(description: str) -> None:
    job = evaluate(description)
    assert job.german_requirement_status == "optional"
    assert job.german_requirement_level == "none"
    assert "german_requirement=none:not_required" in job.language_reasons


@pytest.mark.parametrize(
    "description",
    [
        "German B2 course provided",
        "The company offers German lessons up to B2.",
        "We provide German training as an employee benefit.",
        "Support for B2 certification is available.",
        "English C1 is required for this role.",
    ],
)
def test_non_requirement_context_does_not_reject(description: str) -> None:
    job = evaluate(description)
    assert job.german_requirement_status == "unspecified"
    assert job.german_requirement_level == "unknown"


def test_level_for_another_language_is_not_assigned_to_german() -> None:
    job = evaluate("We serve German customers; English C1 is required.")
    assert job.german_requirement_status == "unspecified"
    assert job.german_requirement_level == "unknown"


@pytest.mark.parametrize(
    "field",
    ["title", "tags"],
)
def test_title_and_tags_are_searched_for_requirements(field: str) -> None:
    overrides = {field: "German B2 required" if field == "title" else ["German B2 required"]}
    job = make_job("Build React products.", **overrides)
    assert evaluate_language(job, policy()) is False
    assert job.german_requirement_level == "b2"


@pytest.mark.parametrize(
    "description",
    [
        "German skills",
        "Good knowledge of German",
        "Deutschkenntnisse",
        "German communication skills",
    ],
)
def test_vague_german_evidence_passes_unknown(description: str) -> None:
    job = evaluate(description)
    assert job.german_requirement_status == "unknown"
    assert job.german_requirement_level == "unknown"


def test_required_lower_level_wins_over_optional_higher_level() -> None:
    job = evaluate("German B1 required; B2 preferred")
    assert job.german_requirement_status == "compatible"
    assert job.german_requirement_level == "b1"
    assert "german_requirement=b2:optional" in job.language_reasons


def test_direct_unqualified_level_is_a_requirement() -> None:
    job = evaluate("Qualifications: German C1")
    assert job.german_requirement_status == "incompatible"
    assert job.german_requirement_level == "c1"


def test_future_target_is_not_a_current_requirement() -> None:
    job = evaluate("Willingness to improve German and work towards B2")
    assert job.german_requirement_status == "unknown"
    assert evaluate_language(job, policy()) is True


def test_conflicting_required_and_negated_statements_pass_unknown() -> None:
    job = evaluate("German B2 required. German is not required.")
    assert job.german_requirement_status == "unknown"
    assert job.german_requirement_level == "b2"


@pytest.mark.parametrize(
    ("description", "status", "passes"),
    [
        ("German B1 or fluent English", "compatible", True),
        ("German B2 or English", "compatible", True),
        ("German B2 or fluent English", "unknown", True),
        ("German B2 or English C1", "unknown", True),
        ("German B2 and English", "incompatible", False),
        ("German B2 and fluent English", "incompatible", False),
    ],
)
def test_alternative_language_policy(description: str, status: str, passes: bool) -> None:
    job = make_job(description)
    assert evaluate_language(job, policy()) is passes
    assert job.german_requirement_status == status
    if status == "unknown":
        assert (
            "alternative_language_requirement=english_explicit_level_unmodeled"
            in job.language_reasons
        )


def test_unrestricted_alternative_requires_accepted_language() -> None:
    job = make_job("German B2 or English")
    assert evaluate_language(job, policy(languages=frozenset())) is False
    assert job.german_requirement_status == "incompatible"


def test_posting_language_never_gates_without_requirement() -> None:
    job = make_job(
        "Du entwickelst moderne React-Webanwendungen in unserem Berliner Produktteam.",
        title="Frontend-Entwickler",
    )
    assert evaluate_language(job, policy()) is True
    assert job.posting_language == "de"
    assert job.german_requirement_status == "unspecified"

    other = make_job(
        "Vous développez des applications React accessibles pour nos clients.",
        title="Frontend Developer",
        url="https://example.test/jobs/french",
    )
    assert evaluate_language(other, policy()) is True
    assert other.posting_language == "other"
    assert other.german_requirement_status == "unspecified"


PIPELINE_SETTINGS = SimpleNamespace(
    COMPANY_BLOCKLIST=[],
    FILTER_SENIOR_ONLY=False,
    MIN_SALARY_EUR=0,
    SOURCE_MAX_AGE_DAYS={},
    MAX_JOB_AGE_DAYS=14,
    MINIMUM_MATCH_SCORE=0,
)


def test_pipeline_has_one_language_rejection_and_accounting() -> None:
    jobs = [
        make_job(
            "React and TypeScript. German B2 required",
            source="alpha",
            company="Alpha",
        ),
        make_job(
            "React and TypeScript. German B1 required",
            source="beta",
            company="Beta",
            url="https://example.test/jobs/compatible",
        ),
    ]
    summary = run_filter_pipeline(
        jobs,
        settings=PIPELINE_SETTINGS,
        language_policy=policy(),
        verbose=True,
    )
    assert summary.rejection_counts[RejectionCode.LANGUAGE] == 1
    assert summary.raw_count == len(summary.accepted_jobs) + sum(summary.rejection_counts.values())
    for metrics in summary.per_source.values():
        assert metrics.raw_count == metrics.accepted_count + sum(metrics.rejection_counts.values())


def test_employment_rejection_remains_before_language() -> None:
    job = make_job(
        "German C2 required for this React role.",
        title="Frontend Engineering Internship",
        employment_relationship="internship",
    )
    summary = run_filter_pipeline(
        [job], settings=PIPELINE_SETTINGS, language_policy=policy(), verbose=True
    )
    assert summary.rejection_counts[RejectionCode.EMPLOYMENT_RELATIONSHIP] == 1
    assert summary.rejection_counts[RejectionCode.LANGUAGE] == 0
    assert job.german_requirement_status == "unknown"


def test_language_evaluation_preserves_structured_employment_metadata() -> None:
    job = classify_employment(
        make_job("React role. German B1 required."),
        EmploymentStructuredInput(
            employment_relationship="freelance",
            work_schedule="part_time",
            contract_term="fixed_term",
        ),
        structured_source="test",
    )
    before = (
        job.employment_relationship,
        job.work_schedule,
        job.contract_term,
        list(job.employment_reasons),
        job.freelance_permission_required,
    )
    assert evaluate_language(job, policy()) is True
    assert (
        job.employment_relationship,
        job.work_schedule,
        job.contract_term,
        job.employment_reasons,
        job.freelance_permission_required,
    ) == before


def test_scoring_and_routing_ignore_language_metadata() -> None:
    jobs = [
        make_job("Build React and TypeScript web products for customers worldwide."),
        make_job(
            "Du entwickelst React und TypeScript Webprodukte für unser Berliner Team.",
            title="Frontend Developer",
            url="https://example.test/jobs/german",
        ),
        make_job(
            "Build React and TypeScript web products. German B1 required.",
            url="https://example.test/jobs/b1",
        ),
    ]
    for job in jobs:
        assert evaluate_language(job, policy()) is True
        compute_match_score(job)
    assert {job.match_score for job in jobs} == {jobs[0].match_score}
    assert {job.notification_tier for job in jobs} == {jobs[0].notification_tier}
    assert jobs[1].german_requirement_status == "unspecified"
    assert jobs[2].german_requirement_status == "compatible"
