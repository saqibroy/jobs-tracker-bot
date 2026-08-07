"""Phase 2B structured source employment mapping and regression tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest

import sources.workable as workable_module
from filters.employment import (
    EmploymentStructuredInput,
    classify_employment,
    employment_rejection_reason,
)
from filters.match import compute_match_score
from filters.pipeline import run_filter_pipeline
from models.job import Job
from models.scan import RejectionCode, SourceStatus
from sources.arbeitnow import ArbeitnowSource, _arbeitnow_employment
from sources.ashby import AshbySource, _ashby_employment
from sources.greenhouse import GreenhouseSource, _greenhouse_employment
from sources.himalayas import HimalayasSource, _himalayas_employment
from sources.idealist import IdealistSource, _idealist_employment
from sources.jsonld import JsonLdCareerSource, _jsonld_employment
from sources.lever import LeverSource, _lever_employment
from sources.linkedin import LinkedInSource
from sources.personio import PersonioSource, _personio_employment
from sources.registry import CompanyBoard
from sources.remotive import RemotiveSource, _remotive_employment
from sources.workable import WorkableSource, _workable_employment
from storage.database import init_db, job_from_row, save_jobs

_FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_json_fixture(name: str) -> object:
    return json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))


def fixture_text(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text(encoding="utf-8")


def json_response(payload: object) -> httpx.Response:
    request = httpx.Request("GET", "https://example.com/jobs")
    return httpx.Response(200, request=request, json=payload)


def text_response(payload: str) -> httpx.Response:
    request = httpx.Request("GET", "https://example.com/jobs")
    return httpx.Response(200, request=request, text=payload)


def dimensions(value: EmploymentStructuredInput) -> tuple[str | None, str | None, str | None]:
    return (
        value.employment_relationship,
        value.work_schedule,
        value.contract_term,
    )


def mapped(provider: str, raw: object) -> EmploymentStructuredInput:
    if provider == "personio":
        values = raw if isinstance(raw, dict) else {}
        return _personio_employment(
            values.get("employmentType"), values.get("schedule")
        )[0]
    if provider == "lever":
        return _lever_employment(raw)
    if provider == "greenhouse":
        return _greenhouse_employment(raw)[0]
    if provider == "ashby":
        return _ashby_employment(raw)
    if provider == "workable":
        return _workable_employment(raw)
    if provider == "jsonld":
        return _jsonld_employment(raw)
    if provider == "arbeitnow":
        return _arbeitnow_employment(raw)
    if provider == "remotive":
        return _remotive_employment(raw)
    if provider == "himalayas":
        return _himalayas_employment(raw)
    if provider == "idealist":
        return _idealist_employment(raw)
    raise AssertionError(provider)


@pytest.mark.parametrize(
    ("provider", "recognized", "expected", "unknown", "malformed"),
    [
        (
            "personio",
            {"employmentType": "permanent", "schedule": "full-time"},
            ("employee", "full_time", "permanent"),
            {"employmentType": "trainee", "schedule": "full-or-part-time"},
            {"employmentType": ["permanent"], "schedule": {"name": "full-time"}},
        ),
        (
            "lever",
            "Permanent Full Time Employee",
            ("employee", "full_time", "permanent"),
            "Full-Time or Part-Time",
            ["Full-time"],
        ),
        (
            "greenhouse",
            [
                {"name": "Time Type", "value": "Part time"},
                {"name": "Employment Type", "value": "Fixed Term"},
            ],
            (None, "part_time", "fixed_term"),
            [{"name": "Employment Type", "value": "Regular"}],
            {"name": "Time Type", "value": "Part time"},
        ),
        ("ashby", "Intern", ("internship", None, None), "Contract", ["Intern"]),
        ("workable", "Full-time", (None, "full_time", None), "Other", {"name": "Full-time"}),
        (
            "jsonld",
            ["PART_TIME", "TEMPORARY"],
            (None, "part_time", "fixed_term"),
            "CONTRACTOR",
            {"type": "PART_TIME"},
        ),
        (
            "arbeitnow",
            ["Working student", "Part Time"],
            ("working_student", "part_time", None),
            ["Contract"],
            "Full Time",
        ),
        ("remotive", "freelance", ("freelance", None, None), "contract", ["freelance"]),
        ("himalayas", "Contractor", ("freelance", None, None), "Other", {"name": "Contractor"}),
        (
            "idealist",
            ["FULL_TIME", "TEMPORARY", "CONTRACT"],
            ("freelance", "full_time", "fixed_term"),
            ["VOLUNTEER"],
            "FULL_TIME",
        ),
    ],
)
def test_every_mapped_provider_handles_recognized_unknown_missing_and_malformed_values(
    provider: str,
    recognized: object,
    expected: tuple[str | None, str | None, str | None],
    unknown: object,
    malformed: object,
) -> None:
    assert dimensions(mapped(provider, recognized)) == expected
    assert dimensions(mapped(provider, unknown)) == (None, None, None)
    assert dimensions(mapped(provider, None)) == (None, None, None)
    assert dimensions(mapped(provider, malformed)) == (None, None, None)


@pytest.mark.asyncio
async def test_personio_xml_and_html_structured_metadata() -> None:
    source = PersonioSource()
    source._get = AsyncMock(return_value=text_response(fixture_text("personio_employment.xml")))
    board = CompanyBoard("Example", "personio", "example")

    jobs = await source._fetch_xml_company(board)
    assert jobs is not None
    structured_schedule, structured_relationship = jobs
    assert structured_schedule.work_schedule == "full_time"
    assert structured_schedule.contract_term == "fixed_term"
    assert "structured:personio:schedule=full_time" in structured_schedule.employment_reasons
    assert any(
        reason == "heuristic:description:term=fixed_term"
        for reason in structured_schedule.employment_reasons
    )
    assert structured_relationship.employment_relationship == "employee"
    assert structured_relationship.contract_term == "permanent"
    assert structured_relationship.employment_relationship != "freelance"
    assert any(
        reason == "structured:personio:employmentType=employee"
        for reason in structured_relationship.employment_reasons
    )

    links = source._extract_html_job_links(
        fixture_text("personio_employment.html"), "https://example.jobs.personio.de"
    )
    assert links == [
        (
            "https://example.jobs.personio.de/job/html-structured",
            "Frontend Engineer Berlin Teilzeit Festanstellung",
            ["Berlin", "Teilzeit", "Festanstellung"],
        )
    ]
    html_job = source._job_from_card(board, *links[0])
    assert html_job is not None
    assert dimensions(EmploymentStructuredInput(
        employment_relationship=html_job.employment_relationship,
        work_schedule=html_job.work_schedule,
        contract_term=html_job.contract_term,
    )) == ("employee", "part_time", "permanent")
    assert any("structured:personio:cardMetadata=" in reason for reason in html_job.employment_reasons)


@pytest.mark.asyncio
async def test_direct_json_provider_fixtures_apply_precedence_and_fallback() -> None:
    fixture = load_json_fixture("structured_employment_sources.json")
    assert isinstance(fixture, dict)
    cases = [
        (LeverSource(), "_fetch_board", "lever", fixture["lever"]),
        (GreenhouseSource(), "_fetch_board", "greenhouse", fixture["greenhouse"]),
        (AshbySource(), "_fetch_company", "ashby", fixture["ashby"]),
        (WorkableSource(), "_fetch_board", "workable", fixture["workable"]),
    ]
    parsed: dict[str, list[Job]] = {}
    for source, method_name, provider, payload in cases:
        source._get = AsyncMock(return_value=json_response(payload))
        board = CompanyBoard("Example", provider, "example")
        parsed[provider] = await getattr(source, method_name)(board)

    lever = parsed["lever"]
    assert lever[0].work_schedule == "full_time"
    assert lever[0].contract_term == "fixed_term"
    assert any("structured:lever:commitment=full_time" == r for r in lever[0].employment_reasons)
    assert lever[1].work_schedule == "part_time"
    assert not any(r.startswith("structured:lever:") for r in lever[1].employment_reasons)

    greenhouse = parsed["greenhouse"]
    assert (greenhouse[0].work_schedule, greenhouse[0].contract_term) == (
        "part_time", "fixed_term"
    )
    assert greenhouse[0].employment_relationship == "employee"
    assert greenhouse[1].work_schedule == "full_time"
    assert greenhouse[1].contract_term == "permanent"

    ashby = parsed["ashby"][0]
    assert ashby.employment_relationship == "internship"
    assert ashby.work_schedule == "full_time"
    assert employment_rejection_reason(ashby) is not None

    workable = parsed["workable"]
    assert workable[0].work_schedule == "full_time"
    assert workable[1].work_schedule == "part_time"
    assert not any(r.startswith("structured:workable:") for r in workable[1].employment_reasons)


@pytest.mark.asyncio
async def test_jsonld_string_array_unknown_and_malformed_values() -> None:
    source = JsonLdCareerSource()
    source._get = AsyncMock(return_value=text_response(fixture_text("jsonld_employment.html")))
    board = CompanyBoard(
        "Example", "jsonld", "example", url="https://example.com/careers"
    )
    job = (await source._fetch_board(board))[0]

    assert (job.work_schedule, job.contract_term) == ("part_time", "fixed_term")
    assert job.employment_relationship == "employee"
    assert "structured:jsonld:employmentType=part_time" in job.employment_reasons
    assert "structured:jsonld:employmentType=fixed_term" in job.employment_reasons


@pytest.mark.asyncio
async def test_default_aggregator_fixtures_classify_student_freelance_and_terms() -> None:
    fixture = load_json_fixture("structured_employment_sources.json")
    assert isinstance(fixture, dict)

    arbeitnow = ArbeitnowSource()
    arbeitnow._get = AsyncMock(return_value=json_response(fixture["arbeitnow"]))
    student = (await arbeitnow.fetch())[0]
    assert (student.employment_relationship, student.work_schedule) == (
        "working_student", "part_time"
    )
    assert employment_rejection_reason(student) is not None

    remotive = RemotiveSource()
    remotive._get = AsyncMock(return_value=json_response(fixture["remotive"]))
    freelance = (await remotive._fetch_category("software-dev"))[0]
    assert freelance.employment_relationship == "freelance"
    assert freelance.work_schedule == "full_time"
    assert employment_rejection_reason(freelance) is None
    assert freelance.freelance_permission_required is True

    himalayas = HimalayasSource()._parse_job(fixture["himalayas"])
    assert himalayas is not None
    assert himalayas.employment_relationship == "freelance"
    assert himalayas.employment_relationship != "employee"

    idealist = IdealistSource()._parse_hit(fixture["idealist"])
    assert idealist is not None
    assert (
        idealist.employment_relationship,
        idealist.work_schedule,
        idealist.contract_term,
    ) == ("freelance", "full_time", "fixed_term")


@pytest.mark.asyncio
async def test_malformed_listing_employment_metadata_does_not_change_source_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = load_json_fixture("structured_employment_sources.json")
    assert isinstance(fixture, dict)
    board = CompanyBoard("Example", "workable", "example")
    monkeypatch.setattr(workable_module, "boards_for", lambda _provider: [board])
    source = WorkableSource()
    source._get = AsyncMock(return_value=json_response(fixture["workable"]))

    outcome = await source.fetch_outcome()

    assert outcome.status is SourceStatus.HEALTHY
    assert outcome.issue_count == 0
    assert len(outcome.jobs) == 2


@pytest.mark.asyncio
async def test_mapped_jobs_survive_sibling_board_failure_as_partial_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = load_json_fixture("structured_employment_sources.json")
    assert isinstance(fixture, dict)
    boards = [
        CompanyBoard("Good", "workable", "good"),
        CompanyBoard("Stale", "workable", "stale"),
    ]
    monkeypatch.setattr(workable_module, "boards_for", lambda _provider: boards)
    source = WorkableSource()

    async def fetch(url: str, **_kwargs: object) -> httpx.Response:
        if "stale" in url:
            request = httpx.Request("GET", url)
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError(
                "stale board", request=request, response=response
            )
        return json_response(fixture["workable"])

    source._get = fetch  # type: ignore[method-assign]
    outcome = await source.fetch_outcome()

    assert outcome.status is SourceStatus.PARTIAL_SUCCESS
    assert outcome.issue_count == 1
    assert len(outcome.jobs) == 2
    assert outcome.jobs[0].work_schedule == "full_time"


def test_unsupported_structured_value_remains_unknown_and_unmapped_source_works() -> None:
    unsupported = Job(
        title="Software Engineer",
        company="Example",
        location="Remote worldwide",
        remote_scope="worldwide",
        url="https://example.com/unsupported-contract",
        description="Build reliable web software.",
        source="ashby",
    )
    classify_employment(
        unsupported,
        _ashby_employment("Contract"),
        structured_source="ashby",
        structured_fields={"employment_relationship": "employmentType"},
    )
    assert (
        unsupported.employment_relationship,
        unsupported.work_schedule,
        unsupported.contract_term,
    ) == ("unknown", "unknown", "unknown")
    assert not any(
        reason.startswith("structured:ashby:")
        for reason in unsupported.employment_reasons
    )

    html = """
    <div class="base-card">
      <a class="base-card__full-link" href="https://example.com/linkedin/1"></a>
      <h3 class="base-search-card__title">Software Engineer</h3>
      <h4 class="base-search-card__subtitle">Example</h4>
      <span class="job-search-card__location">Germany</span>
    </div>
    """
    linkedin = LinkedInSource()._parse_html(html)
    assert len(linkedin) == 1
    assert linkedin[0].employment_relationship == "unknown"
    assert linkedin[0].employment_reasons == []


def test_structured_enrichment_preserves_hashes_and_notification_routing() -> None:
    job = Job(
        title="Senior Frontend Engineer",
        company="Example",
        location="Remote worldwide",
        remote_scope="worldwide",
        workplace_type="remote",
        url="https://example.com/hash-stability",
        description="React TypeScript Next.js product work.",
        source="fixture",
    )
    original_hashes = (job.id, job.content_hash)
    classify_employment(
        job,
        EmploymentStructuredInput(employment_relationship="freelance"),
        structured_source="fixture",
        structured_fields={"employment_relationship": "employmentType"},
    )
    assert (job.id, job.content_hash) == original_hashes

    employee = job.model_copy(deep=True)
    employee.employment_relationship = "employee"
    job_score = compute_match_score(job)
    employee_score = compute_match_score(employee)
    assert job_score == employee_score
    assert job.notification_tier == employee.notification_tier


def test_structured_student_rejection_keeps_accounting_invariant() -> None:
    student = Job(
        title="Working Student Frontend Engineer",
        company="Example",
        location="Remote Germany",
        remote_scope="germany",
        workplace_type="remote",
        url="https://example.com/accounting/student",
        description="React TypeScript product work.",
        source="arbeitnow",
        employment_relationship="working_student",
        employment_reasons=[
            "structured:arbeitnow:job_types=working_student"
        ],
    )
    summary = run_filter_pipeline([student])
    summary.validate_accounting()
    assert summary.accepted_count == 0
    assert summary.rejection_counts[RejectionCode.EMPLOYMENT_RELATIONSHIP] == 1
    assert summary.per_source["arbeitnow"].raw_count == 1


@pytest.mark.asyncio
async def test_mapped_source_job_round_trips_without_schema_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "phase2b.db"
    monkeypatch.setattr("storage.database.config.DATABASE_PATH", str(path))
    await init_db()
    fixture = load_json_fixture("structured_employment_sources.json")
    assert isinstance(fixture, dict)
    job = IdealistSource()._parse_hit(fixture["idealist"])
    assert job is not None
    employment_rejection_reason(job)
    assert await save_jobs([job]) == [job]

    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job.id,))
        row = await cursor.fetchone()
    assert row is not None
    restored = job_from_row(row)
    assert restored.employment_relationship == "freelance"
    assert restored.work_schedule == "full_time"
    assert restored.contract_term == "fixed_term"
    assert restored.employment_reasons == job.employment_reasons
    assert restored.freelance_permission_required is True
