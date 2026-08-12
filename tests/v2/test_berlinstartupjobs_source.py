"""Focused BerlinStartupJobs REST adapter and rollout-registration tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

import main
from filters.location import passes_location_filter
from models.scan import SourceStatus
from sources.berlinstartupjobs import BerlinStartupJobsSource
from sources.catalog import SOURCE_BY_NAME, SOURCE_GROUPS, manual_all_source_names


_FIXTURE_DIR = Path(__file__).parent / "fixtures"


def fixture(name: str) -> object:
    return json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))


def response(
    payload: object,
    *,
    status: int = 200,
    total_pages: int | None = None,
    url: str = "https://berlinstartupjobs.com/wp-json/wp/v2/posts",
) -> httpx.Response:
    headers = {}
    if total_pages is not None:
        headers["X-WP-TotalPages"] = str(total_pages)
    request = httpx.Request("GET", url)
    return httpx.Response(status, request=request, json=payload, headers=headers)


def category_response(payload: object | None = None) -> httpx.Response:
    return response(
        fixture("berlinstartupjobs_category.json") if payload is None else payload,
        url="https://berlinstartupjobs.com/wp-json/wp/v2/categories",
    )


def post_by_id(post_id: int, page: str = "page1") -> dict:
    payload = fixture(f"berlinstartupjobs_{page}.json")
    assert isinstance(payload, list)
    return copy.deepcopy(next(item for item in payload if item["id"] == post_id))


@pytest.mark.asyncio
async def test_one_page_mapping_uses_taxonomy_and_no_detail_requests() -> None:
    source = BerlinStartupJobsSource()
    source._get = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            category_response(),
            response(fixture("berlinstartupjobs_page1.json"), total_pages=1),
        ]
    )

    outcome = await source.fetch_outcome()

    assert outcome.status is SourceStatus.HEALTHY
    assert len(outcome.jobs) == 4
    assert source._get.await_count == 2
    requested_urls = [call.args[0] for call in source._get.await_args_list]
    assert requested_urls == [
        "https://berlinstartupjobs.com/wp-json/wp/v2/categories",
        "https://berlinstartupjobs.com/wp-json/wp/v2/posts",
    ]
    category_params = source._get.await_args_list[0].kwargs["params"]
    posts_params = source._get.await_args_list[1].kwargs["params"]
    assert category_params["slug"] == "engineering"
    assert posts_params["categories"] == "9"
    assert posts_params["per_page"] == "100"
    assert posts_params["_embed"] == "wp:term"
    assert "_embedded" in posts_params["_fields"]

    frontend = next(job for job in outcome.jobs if job.id == "berlinstartupjobs:5001")
    assert frontend.title == "Senior Frontend Developer – TypeScript & React // Example Labs"
    assert frontend.company == "Example Labs"
    assert frontend.location == "Berlin, Germany"
    assert frontend.url == (
        "https://berlinstartupjobs.com/engineering/"
        "senior-frontend-developer-example-labs/"
    )
    assert frontend.workplace_type == "onsite"
    assert frontend.is_remote is False
    assert frontend.posted_at is not None
    assert frontend.posted_at.isoformat() == "2026-08-10T10:00:00+00:00"
    assert "ReactJS" in frontend.tags
    assert "<p>" not in (frontend.description or "")
    assert frontend.salary == "Salary: €70,000–€85,000 per year"
    assert frontend.employment_relationship == "employee"
    assert frontend.work_schedule == "full_time"
    assert frontend.contract_term == "permanent"


@pytest.mark.asyncio
async def test_two_pages_are_sequential_and_deduplicate_provider_ids() -> None:
    source = BerlinStartupJobsSource()
    source._get = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            category_response(),
            response(fixture("berlinstartupjobs_page1.json"), total_pages=2),
            response(fixture("berlinstartupjobs_page2.json"), total_pages=2),
        ]
    )

    outcome = await source.fetch_outcome()

    assert outcome.status is SourceStatus.HEALTHY
    assert len(outcome.jobs) == 5
    assert len({job.id for job in outcome.jobs}) == 5
    assert source._get.await_count == 3
    assert [
        call.kwargs["params"].get("page")
        for call in source._get.await_args_list[1:]
    ] == ["1", "2"]

    plain = next(job for job in outcome.jobs if job.id == "berlinstartupjobs:5006")
    assert plain.salary is None
    assert plain.employment_relationship == "unknown"
    assert plain.work_schedule == "unknown"
    assert plain.contract_term == "unknown"


def test_provider_post_id_is_stable_when_public_url_changes() -> None:
    source = BerlinStartupJobsSource()
    first = post_by_id(5001)
    changed = copy.deepcopy(first)
    changed["link"] = "https://berlinstartupjobs.com/engineering/renamed-post/"

    first_job, _ = source._parse_post(first)
    changed_job, _ = source._parse_post(changed)

    assert first_job.id == changed_job.id == "berlinstartupjobs:5001"
    assert first_job.url != changed_job.url


def test_location_semantics_keep_remote_eligibility_conservative() -> None:
    source = BerlinStartupJobsSource()
    hybrid, _ = source._parse_post(post_by_id(5002))
    remote_only, _ = source._parse_post(post_by_id(5003))

    assert hybrid.workplace_type == "hybrid"
    assert hybrid.is_remote is False
    assert hybrid.remote_scope is None
    assert passes_location_filter(hybrid) is True

    assert remote_only.workplace_type == "remote"
    assert remote_only.is_remote is True
    assert remote_only.remote_scope == "unknown"
    assert remote_only.eligible_countries == []
    assert passes_location_filter(remote_only) is False

    remote_berlin_item = post_by_id(5003)
    location_terms = remote_berlin_item["_embedded"]["wp:term"][3]
    location_terms.insert(
        0,
        {
            "id": 301,
            "taxonomy": "job_location",
            "name": "Berlin, Germany",
            "slug": "berlin-germany",
        },
    )
    remote_berlin, _ = source._parse_post(remote_berlin_item)
    assert remote_berlin.workplace_type == "remote"
    assert remote_berlin.remote_scope == "germany"
    assert remote_berlin.eligible_countries == ["de"]
    assert passes_location_filter(remote_berlin) is True


def test_explicit_freelance_and_bounded_description_mapping() -> None:
    source = BerlinStartupJobsSource()
    freelance, _ = source._parse_post(post_by_id(5004))
    assert freelance.employment_relationship == "freelance"
    assert freelance.salary is None

    long_item = post_by_id(5006, "page2")
    long_item["content"]["rendered"] = f"<p>{'x' * 30_000}</p>"
    bounded, _ = source._parse_post(long_item)
    assert bounded.description is not None
    assert len(bounded.description) == 25_000


@pytest.mark.asyncio
async def test_zero_results_is_a_complete_usable_run() -> None:
    source = BerlinStartupJobsSource()
    source._get = AsyncMock(  # type: ignore[method-assign]
        side_effect=[category_response(), response([], total_pages=0)]
    )

    outcome = await source.fetch_outcome()

    assert outcome.status is SourceStatus.ZERO_RESULTS
    assert outcome.jobs == []
    assert outcome.issue_count == 0
    assert source._get.await_count == 2


@pytest.mark.asyncio
async def test_missing_engineering_category_is_parse_error() -> None:
    source = BerlinStartupJobsSource()
    source._get = AsyncMock(return_value=category_response([]))  # type: ignore[method-assign]

    outcome = await source.fetch_outcome()

    assert outcome.status is SourceStatus.PARSE_ERROR
    assert outcome.jobs == []
    assert source._get.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [(429, SourceStatus.RATE_LIMITED), (503, SourceStatus.NETWORK_ERROR)],
)
async def test_first_posts_page_failure_keeps_concrete_status(
    status: int,
    expected: SourceStatus,
) -> None:
    source = BerlinStartupJobsSource()
    source._get = AsyncMock(  # type: ignore[method-assign]
        side_effect=[category_response(), response({}, status=status)]
    )

    outcome = await source.fetch_outcome()

    assert outcome.status is expected
    assert outcome.status is not SourceStatus.PARTIAL_SUCCESS
    assert outcome.jobs == []
    assert outcome.issue_count == 1
    assert source._get.await_count == 2


@pytest.mark.asyncio
async def test_second_page_failure_is_partial_and_preserves_page_one() -> None:
    source = BerlinStartupJobsSource()
    source._get = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            category_response(),
            response(fixture("berlinstartupjobs_page1.json"), total_pages=2),
            response({}, status=503),
        ]
    )

    outcome = await source.fetch_outcome()

    assert outcome.status is SourceStatus.PARTIAL_SUCCESS
    assert len(outcome.jobs) == 4
    assert outcome.issue_count == 1
    assert source._get.await_count == 3


@pytest.mark.asyncio
async def test_reported_pages_beyond_bound_are_partial_without_fourth_request() -> None:
    source = BerlinStartupJobsSource()
    source._get = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            category_response(),
            response(fixture("berlinstartupjobs_page1.json"), total_pages=3),
            response(fixture("berlinstartupjobs_page2.json"), total_pages=3),
        ]
    )

    outcome = await source.fetch_outcome()

    assert outcome.status is SourceStatus.PARTIAL_SUCCESS
    assert len(outcome.jobs) == 5
    assert outcome.issue_count == 1
    assert "hard bound is 2" in (outcome.sanitized_error or "")
    assert source._get.await_count == 3


def test_catalog_registers_source_but_does_not_schedule_it() -> None:
    definition = SOURCE_BY_NAME["berlinstartupjobs"]

    assert definition.adapter_class is BerlinStartupJobsSource
    assert definition.manual_only is True
    assert definition.scheduled_group is None
    assert "berlinstartupjobs" in main.ALL_SOURCES
    assert "berlinstartupjobs" not in manual_all_source_names()
    assert all(
        "berlinstartupjobs" not in group.source_names for group in SOURCE_GROUPS
    )
    resolved = main._get_sources("berlinstartupjobs")
    assert len(resolved) == 1
    assert isinstance(resolved[0], BerlinStartupJobsSource)
