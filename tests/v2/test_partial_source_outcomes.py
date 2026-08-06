"""Focused Phase 1B multi-component source outcome tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

import filters.pipeline as pipeline
import health
import main
import sources.ashby as ashby_module
import sources.greenhouse as greenhouse_module
import sources.himalayas as himalayas_module
import sources.idealist as idealist_module
import sources.jsonld as jsonld_module
import sources.lever as lever_module
import sources.linkedin as linkedin_module
import sources.personio as personio_module
import sources.remotive as remotive_module
import sources.stepstone as stepstone_module
import sources.workable as workable_module
from models.job import Job
from models.scan import SourceStatus
from sources.ashby import AshbySource
from sources.base import BaseSource
from sources.greenhouse import GreenhouseSource
from sources.himalayas import HimalayasSource
from sources.idealist import IdealistSource
from sources.jsonld import JsonLdCareerSource
from sources.lever import LeverSource
from sources.linkedin import LinkedInSource
from sources.personio import PersonioSource
from sources.remotive import RemotiveSource
from sources.registry import CompanyBoard
from sources.stepstone import StepstoneSource
from sources.workable import WorkableSource
from storage.database import (
    get_latest_source_status,
    init_db,
    persist_scan_metrics,
)


def make_job(*, source: str = "components", suffix: str = "1") -> Job:
    return Job(
        title=f"Software Engineer {suffix}",
        company=f"Company {suffix}",
        location="Remote worldwide",
        remote_scope="worldwide",
        url=f"https://example.com/jobs/{source}/{suffix}",
        description="Build reliable web software.",
        source=source,
    )


def http_error(status: int, message: str = "request failed") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.com/jobs?token=secret#private")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(message, request=request, response=response)


def json_response(payload: object, status: int = 200) -> httpx.Response:
    request = httpx.Request("GET", "https://example.com/jobs")
    return httpx.Response(status, request=request, json=payload)


class ComponentSource(BaseSource):
    """Small source that exercises BaseSource's shared result aggregator."""

    def __init__(self, components: list[tuple[object, list[Job] | Exception]]) -> None:
        super().__init__()
        self.name = "components"
        self.components = components

    async def fetch(self) -> list[Job]:
        identifiers = [item[0] for item in self.components]
        results = [item[1] for item in self.components]
        return self._consume_component_results(identifiers, results, str)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("components", "expected_status", "expected_jobs", "expected_issues"),
    [
        (
            [("good", [make_job()]), ("bad", httpx.ReadTimeout("offline"))],
            SourceStatus.PARTIAL_SUCCESS,
            1,
            1,
        ),
        (
            [("empty", []), ("bad", httpx.ReadTimeout("offline"))],
            SourceStatus.PARTIAL_SUCCESS,
            0,
            1,
        ),
        (
            [("one", [make_job()]), ("two", [])],
            SourceStatus.HEALTHY,
            1,
            0,
        ),
        (
            [("one", []), ("two", [])],
            SourceStatus.ZERO_RESULTS,
            0,
            0,
        ),
        (
            [("one", RuntimeError("broken")), ("two", ValueError("bad json"))],
            SourceStatus.PARSE_ERROR,
            0,
            2,
        ),
    ],
)
async def test_shared_component_status_semantics(
    components: list[tuple[object, list[Job] | Exception]],
    expected_status: SourceStatus,
    expected_jobs: int,
    expected_issues: int,
) -> None:
    outcome = await ComponentSource(components).fetch_outcome()

    assert outcome.status is expected_status
    assert len(outcome.jobs) == expected_jobs
    assert outcome.issue_count == expected_issues


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (http_error(429), SourceStatus.RATE_LIMITED),
        (http_error(403), SourceStatus.BLOCKED),
        (httpx.ConnectError("connection refused"), SourceStatus.NETWORK_ERROR),
        (ValueError("invalid top-level json"), SourceStatus.PARSE_ERROR),
        (RuntimeError("unexpected"), SourceStatus.UNKNOWN_ERROR),
    ],
)
async def test_all_failed_component_categories(
    error: Exception,
    expected: SourceStatus,
) -> None:
    outcome = await ComponentSource([("failed", error)]).fetch_outcome()

    assert outcome.status is expected
    assert outcome.status is not SourceStatus.PARTIAL_SUCCESS
    assert outcome.issue_count == 1


@pytest.mark.asyncio
async def test_complete_failure_precedence_includes_issues_beyond_detail_limit() -> None:
    components: list[tuple[object, list[Job] | Exception]] = [
        (f"unknown-{index}", RuntimeError("unknown")) for index in range(6)
    ]
    components.extend(
        [
            ("parse", ValueError("parse")),
            ("network", httpx.ReadTimeout("network")),
            ("blocked", http_error(403)),
            ("limited", http_error(429)),
        ]
    )

    outcome = await ComponentSource(components).fetch_outcome()

    assert outcome.status is SourceStatus.RATE_LIMITED
    assert outcome.issue_count == 10
    assert len(outcome.issues) == 5


@pytest.mark.asyncio
async def test_component_issue_details_are_bounded_and_sanitized() -> None:
    components = [
        (
            f"https://example.com/board/{index}?token=hunter2#fragment\n",
            RuntimeError(
                "password=swordfish failed at "
                "https://example.com/feed?access_token=private#fragment\x00"
            ),
        )
        for index in range(8)
    ]

    outcome = await ComponentSource(components).fetch_outcome()

    assert outcome.issue_count == 8
    assert len(outcome.issues) == 5
    assert outcome.sanitized_error is not None
    assert len(outcome.sanitized_error) <= 300
    assert "hunter2" not in outcome.sanitized_error
    assert "swordfish" not in outcome.sanitized_error
    assert "private" not in outcome.sanitized_error
    assert "fragment" not in outcome.sanitized_error
    assert "\n" not in outcome.sanitized_error
    assert "\x00" not in outcome.sanitized_error


_BOARD_ADAPTERS = [
    pytest.param(
        GreenhouseSource,
        greenhouse_module,
        "_fetch_board",
        "greenhouse",
        id="mixed-greenhouse-boards",
    ),
    pytest.param(
        AshbySource,
        ashby_module,
        "_fetch_company",
        "ashby",
        id="mixed-ashby-boards",
    ),
    pytest.param(
        PersonioSource,
        personio_module,
        "_fetch_company",
        "personio",
        id="mixed-personio-boards",
    ),
    pytest.param(
        LeverSource,
        lever_module,
        "_fetch_board",
        "lever",
        id="mixed-lever-boards",
    ),
    pytest.param(
        WorkableSource,
        workable_module,
        "_fetch_board",
        "workable",
        id="mixed-workable-boards",
    ),
    pytest.param(
        JsonLdCareerSource,
        jsonld_module,
        "_fetch_board",
        "jsonld",
        id="mixed-jsonld-career-urls",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("source_cls", "source_module", "worker_name", "name"), _BOARD_ADAPTERS)
async def test_mixed_employer_board_adapters_preserve_jobs(
    monkeypatch: pytest.MonkeyPatch,
    source_cls: type[BaseSource],
    source_module: object,
    worker_name: str,
    name: str,
) -> None:
    boards = [
        CompanyBoard(company="Good", provider=name, slug="good", url="https://good.example/jobs"),
        CompanyBoard(company="Stale", provider=name, slug="stale", url="https://stale.example/jobs"),
    ]
    monkeypatch.setattr(source_module, "boards_for", lambda _provider: boards)
    source = source_cls()

    async def fetch_board(board: CompanyBoard) -> list[Job]:
        if board.slug == "stale":
            raise http_error(404, "stale board https://example.com?token=private")
        return [make_job(source=name)]

    monkeypatch.setattr(source, worker_name, fetch_board)

    outcome = await source.fetch_outcome()

    assert outcome.status is SourceStatus.PARTIAL_SUCCESS
    assert len(outcome.jobs) == 1
    assert outcome.jobs[0].source == name
    assert outcome.issue_count == 1
    assert outcome.issues[0].component in {"board:stale", "career:stale"}


@pytest.mark.asyncio
async def test_malformed_greenhouse_listing_does_not_make_source_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = CompanyBoard(company="Good", provider="greenhouse", slug="good")
    monkeypatch.setattr(greenhouse_module, "boards_for", lambda _provider: [board])
    source = GreenhouseSource()
    response = json_response(
        {
            "jobs": [
                {"location": "malformed location"},
                {
                    "title": "Software Engineer",
                    "location": {"name": "Remote worldwide"},
                    "absolute_url": "https://example.com/jobs/valid",
                    "content": "Build software",
                },
            ]
        }
    )
    monkeypatch.setattr(source, "_get", AsyncMock(return_value=response))

    outcome = await source.fetch_outcome()

    assert outcome.status is SourceStatus.HEALTHY
    assert len(outcome.jobs) == 1
    assert outcome.issue_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "source_module", "items_name", "worker_name"),
    [
        pytest.param(
            RemotiveSource(),
            remotive_module,
            "_CATEGORIES",
            "_fetch_category",
            id="mixed-remotive-categories",
        ),
        pytest.param(
            LinkedInSource(),
            linkedin_module,
            "_SEARCH_QUERIES",
            "_fetch_query",
            id="mixed-linkedin-queries",
        ),
    ],
)
async def test_default_concurrent_multi_request_adapters_report_partial(
    monkeypatch: pytest.MonkeyPatch,
    source: BaseSource,
    source_module: object,
    items_name: str,
    worker_name: str,
) -> None:
    if isinstance(source, RemotiveSource):
        items: list[object] = ["good", "bad"]
    else:
        items = [
            {
                "keywords": "good",
                "location": "Germany",
                "f_WT": "2",
                "workplace_type": "remote",
            },
            {
                "keywords": "bad",
                "location": "Germany",
                "f_WT": "2",
                "workplace_type": "remote",
            },
        ]
    monkeypatch.setattr(source_module, items_name, items)

    async def fetch_item(item: object) -> list[Job]:
        label = item if isinstance(item, str) else item["keywords"]  # type: ignore[index]
        if label == "bad":
            raise httpx.ReadTimeout("component failed")
        return [make_job(source=source.name)]

    monkeypatch.setattr(source, worker_name, fetch_item)

    outcome = await source.fetch_outcome()

    assert outcome.status is SourceStatus.PARTIAL_SUCCESS
    assert len(outcome.jobs) == 1
    assert outcome.issue_count == 1


@pytest.mark.asyncio
async def test_mixed_idealist_queries_report_partial_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        idealist_module,
        "_QUERIES",
        [("good", "filter"), ("bad", "filter")],
    )
    source = IdealistSource()
    monkeypatch.setattr(
        source,
        "_post_algolia",
        AsyncMock(
            side_effect=[
                json_response({"hits": []}),
                httpx.ReadTimeout("query failed"),
            ]
        ),
    )

    outcome = await source.fetch_outcome()

    assert outcome.status is SourceStatus.PARTIAL_SUCCESS
    assert outcome.jobs == []
    assert outcome.issue_count == 1


@pytest.mark.asyncio
async def test_failed_later_himalayas_page_preserves_earlier_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(himalayas_module, "_MAX_PAGES", 2)
    item = {
        "title": "Software Engineer",
        "companyName": "Acme",
        "applicationLink": "https://example.com/himalayas/1",
        "categories": ["software"],
        "locationRestrictions": ["Germany"],
    }
    source = HimalayasSource()
    monkeypatch.setattr(
        source,
        "_get",
        AsyncMock(
            side_effect=[
                json_response({"jobs": [item] * himalayas_module._PAGE_SIZE}),
                httpx.ReadTimeout("later page failed"),
            ]
        ),
    )

    outcome = await source.fetch_outcome()

    assert outcome.status is SourceStatus.PARTIAL_SUCCESS
    assert len(outcome.jobs) == 1
    assert outcome.issue_count == 1


@pytest.mark.asyncio
async def test_normal_stepstone_pagination_termination_is_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stepstone_module, "_SEARCH_QUERIES", [{"was": "Developer", "wo": "Deutschland"}])
    monkeypatch.setattr(stepstone_module, "_MAX_PAGES", 2)
    source = StepstoneSource()
    monkeypatch.setattr(
        source,
        "_get",
        AsyncMock(return_value=json_response({"stellenangebote": []})),
    )

    outcome = await source.fetch_outcome()

    assert outcome.status is SourceStatus.ZERO_RESULTS
    assert outcome.issue_count == 0
    assert source._get.await_count == 1


@pytest.mark.asyncio
async def test_failed_later_stepstone_page_preserves_earlier_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stepstone_module, "_SEARCH_QUERIES", [{"was": "Developer", "wo": "Deutschland"}])
    monkeypatch.setattr(stepstone_module, "_MAX_PAGES", 2)
    first_page = {
        "stellenangebote": [
            {
                "refnr": "first",
                "titel": "Software Engineer",
                "arbeitgeber": "Acme",
                "arbeitsort": {"ort": "Berlin"},
            }
        ]
        * stepstone_module._PAGE_SIZE
    }
    source = StepstoneSource()
    monkeypatch.setattr(
        source,
        "_get",
        AsyncMock(side_effect=[json_response(first_page), httpx.ReadTimeout("page two failed")]),
    )

    outcome = await source.fetch_outcome()

    assert outcome.status is SourceStatus.PARTIAL_SUCCESS
    assert len(outcome.jobs) == 1
    assert outcome.issue_count == 1
    assert outcome.issues[0].component == "query:Developer/page:2"


@pytest.mark.asyncio
async def test_mixed_stepstone_queries_report_partial_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        stepstone_module,
        "_SEARCH_QUERIES",
        [
            {"was": "Frontend", "wo": "Deutschland"},
            {"was": "Backend", "wo": "Deutschland"},
        ],
    )
    monkeypatch.setattr(stepstone_module, "_MAX_PAGES", 1)
    source = StepstoneSource()
    monkeypatch.setattr(
        source,
        "_get",
        AsyncMock(
            side_effect=[
                json_response({"stellenangebote": []}),
                httpx.ConnectError("query failed"),
            ]
        ),
    )

    outcome = await source.fetch_outcome()

    assert outcome.status is SourceStatus.PARTIAL_SUCCESS
    assert outcome.jobs == []
    assert outcome.issue_count == 1


@pytest.mark.asyncio
async def test_all_stepstone_queries_failing_is_complete_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        stepstone_module,
        "_SEARCH_QUERIES",
        [
            {"was": "Frontend", "wo": "Deutschland"},
            {"was": "Backend", "wo": "Deutschland"},
        ],
    )
    monkeypatch.setattr(stepstone_module, "_MAX_PAGES", 1)
    source = StepstoneSource()
    monkeypatch.setattr(
        source,
        "_get",
        AsyncMock(side_effect=[http_error(429), httpx.ConnectError("offline")]),
    )

    outcome = await source.fetch_outcome()

    assert outcome.status is SourceStatus.RATE_LIMITED
    assert outcome.status is not SourceStatus.PARTIAL_SUCCESS
    assert outcome.issue_count == 2


@pytest.fixture
async def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "jobs.db"
    monkeypatch.setattr("storage.database.config.DATABASE_PATH", str(path))
    await init_db()
    return path


@pytest.mark.asyncio
async def test_partial_and_fully_successful_timestamp_semantics(database: Path) -> None:
    now = datetime.now(timezone.utc)
    source = ComponentSource([("good", [])])
    healthy = await source.fetch_outcome()
    healthy.source = "alpha"
    healthy.started_at = now - timedelta(hours=4, seconds=1)
    healthy.completed_at = now - timedelta(hours=4)
    healthy.status = SourceStatus.ZERO_RESULTS
    healthy_summary = main._build_scan_summary(
        scan_id="zero",
        started_at=healthy.started_at,
        outcomes=[healthy],
        filter_summary=pipeline.run_filter_pipeline([], settings=main.config),
    )
    await persist_scan_metrics(healthy_summary)

    partial = await ComponentSource(
        [("good", []), ("bad", httpx.ReadTimeout("offline"))]
    ).fetch_outcome()
    partial.source = "alpha"
    partial.started_at = now - timedelta(hours=2, seconds=1)
    partial.completed_at = now - timedelta(hours=2)
    partial_summary = main._build_scan_summary(
        scan_id="partial",
        started_at=partial.started_at,
        outcomes=[partial],
        filter_summary=pipeline.run_filter_pipeline([], settings=main.config),
    )
    await persist_scan_metrics(partial_summary)

    latest = await get_latest_source_status("alpha")
    assert latest is not None
    assert latest["status"] == "partial_success"
    assert latest["last_usable_at"] == latest["last_completed_at"]
    assert latest["last_fully_successful_at"].startswith(
        (now - timedelta(hours=4)).date().isoformat()
    )


def allow_filter_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline, "passes_location_filter", lambda job: True)
    monkeypatch.setattr(pipeline, "passes_role_filter", lambda job: True)
    monkeypatch.setattr(pipeline, "passes_stack_filter", lambda job: True)
    monkeypatch.setattr(pipeline, "passes_language_filter", lambda job: True)
    monkeypatch.setattr(pipeline, "classify_ngo", lambda job: job)

    def score_job(job: Job) -> int:
        job.notification_tier = "immediate"
        return 80

    monkeypatch.setattr(pipeline, "compute_match_score", score_job)


@pytest.mark.asyncio
async def test_partial_source_jobs_continue_through_save_alongside_healthy_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_filter_dependencies(monkeypatch)
    partial = ComponentSource(
        [
            ("good", [make_job(source="partial", suffix="partial")]),
            ("bad", httpx.ReadTimeout("offline")),
        ]
    )
    partial.name = "partial"
    healthy = ComponentSource([("good", [make_job(source="healthy", suffix="healthy")])])
    healthy.name = "healthy"
    monkeypatch.setattr(main.config, "ENABLE_ATS_SNIFFING", False)
    monkeypatch.setattr(main, "init_db", AsyncMock())
    monkeypatch.setattr(main, "filter_unseen", AsyncMock(side_effect=lambda jobs: jobs))
    monkeypatch.setattr(main, "save_jobs", AsyncMock(side_effect=lambda jobs: jobs))
    persisted = AsyncMock()
    monkeypatch.setattr(main, "persist_scan_metrics", persisted)
    monkeypatch.setattr(main, "get_latest_source_statuses", AsyncMock(return_value=[]))
    monkeypatch.setattr(main, "_send_notifications", AsyncMock())

    saved = await main.run_scan([partial, healthy], dry_run=False)

    assert len(saved) == 2
    summary = persisted.await_args.args[0]
    assert summary.sources["partial"].status is SourceStatus.PARTIAL_SUCCESS
    assert summary.sources["partial"].issue_count == 1
    assert summary.sources["partial"].saved_count == 1
    assert summary.sources["healthy"].status is SourceStatus.HEALTHY
    assert summary.sources["healthy"].saved_count == 1


@pytest.mark.asyncio
async def test_health_stats_and_daily_status_show_bounded_partial_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    source_health = {
        "greenhouse": {
            "status": "partial_success",
            "raw": 3,
            "accepted": 1,
            "saved": 1,
            "issue_count": 7,
            "sanitized_error": "board:stale [unknown_error]: token=secret failed",
            "last_completed_at": timestamp,
            "last_usable_at": timestamp,
            "last_fully_successful_at": "2026-08-05T10:00:00+00:00",
        }
    }
    health.set_scan_summary({"source_health": source_health})
    compact = health.get_scan_summary()["source_health"]["greenhouse"]
    assert compact["status"] == "partial_success"
    assert compact["issue_count"] == 7
    assert compact["last_usable_at"] == timestamp
    assert compact["last_fully_successful_at"] == "2026-08-05T10:00:00+00:00"
    assert "secret" not in compact["sanitized_error"]

    stats_item = {"source": "greenhouse", **compact}
    monkeypatch.setattr(main, "init_db", AsyncMock())
    monkeypatch.setattr(
        main,
        "get_stats",
        AsyncMock(
            return_value={
                "total": 0,
                "ngo_count": 0,
                "new_24h": 0,
                "sources": {},
                "notification_tiers": {},
                "top_companies": [],
                "last_fetched_at": None,
                "source_health": [stats_item],
            }
        ),
    )
    await main._show_stats()
    output = capsys.readouterr().out
    assert "partial_success" in output
    assert "issues" in output
    assert "7" in output
    assert "last full" in output
    assert "secret" not in output

    _rejections, degraded = main._daily_status_details(
        {"source_health": source_health}
    )
    assert "partial_success" in degraded
    assert "7 issue(s)" in degraded
    assert "secret" not in degraded
    assert len(degraded) <= 1000
