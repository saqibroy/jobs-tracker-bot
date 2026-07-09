"""Deterministic v2 acceptance tests for Germany eligibility and CV fit."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from filters.language import passes_language_filter
from filters.location import apply_eligibility
from filters.match import compute_match_score
from filters.role import passes_role_filter
from models.job import Job
from storage.database import _CREATE_TABLE
from sources.greenhouse import GreenhouseSource
from sources.jsonld import JsonLdCareerSource
from sources.lever import LeverSource
from sources.registry import CompanyBoard
from sources.workable import WorkableSource
from sources.base import BaseSource


def make_job(
    title: str = "Senior Full-Stack Engineer",
    location: str = "Europe",
    *,
    workplace: str = "remote",
    description: str = "Build web products with React, TypeScript, Next.js and Python.",
    **kwargs,
) -> Job:
    return Job(
        title=title,
        company=kwargs.pop("company", "Example"),
        location=location,
        workplace_type=workplace,
        is_remote=workplace in ("remote", "hybrid"),
        url=kwargs.pop("url", f"https://example.test/{abs(hash((title, location, workplace)))}"),
        description=description,
        source=kwargs.pop("source", "test"),
        **kwargs,
    )


def test_country_only_remote_jobs_are_rejected():
    for country in ("Poland", "France", "Spain", "United Kingdom", "United States"):
        job = make_job(location=f"Remote - {country}")
        assert not apply_eligibility(job), country
        assert job.eligibility_status == "ineligible"


def test_berlin_hybrid_and_onsite_are_accepted():
    for workplace in ("hybrid", "onsite"):
        job = make_job(location="Berlin, Germany", workplace=workplace)
        assert apply_eligibility(job)
        assert job.remote_scope == "berlin"


def test_non_berlin_onsite_is_rejected():
    for city in ("Munich, Germany", "Hamburg, Germany"):
        assert not apply_eligibility(make_job(location=city, workplace="onsite"))


def test_remote_germany_broad_regions_and_foreign_employer_are_accepted():
    cases = [
        make_job(location="Germany"),
        make_job(location="Europe"),
        make_job(location="EMEA"),
        make_job(location="Worldwide"),
        make_job(
            location="Remote",
            company="US Company",
            eligible_countries=["de"],
        ),
    ]
    assert all(apply_eligibility(job) for job in cases)


def test_residency_restriction_overrides_broad_remote_location():
    job = make_job(
        location="Worldwide",
        description="This is remote, but applicants must be based in Poland or France.",
    )
    assert not apply_eligibility(job)
    assert "residency" in job.eligibility_reasons[0]


def test_incidental_work_in_europe_phrase_is_not_a_residency_rule():
    job = make_job(
        location="Nantes",
        description="You will work in feature teams serving patients across Europe.",
    )
    assert not apply_eligibility(job)


def test_unknown_remote_scope_is_rejected():
    job = make_job(location="Remote")
    assert not apply_eligibility(job)
    assert job.remote_scope == "unknown"


def test_role_profile_accepts_target_and_rejects_noise():
    assert passes_role_filter(make_job(title="Senior Frontend Engineer"))
    assert passes_role_filter(make_job(title="Backend Engineer", description="Python Django APIs"))
    assert not passes_role_filter(make_job(title="Technical Program Manager"))
    assert not passes_role_filter(make_job(title="Security Lead"))
    assert not passes_role_filter(make_job(title="Junior React Developer"))
    assert not passes_role_filter(make_job(title="Ruby on Rails Developer", tags=["Entry-level"]))
    assert not passes_role_filter(make_job(title="Applied AI Engineer", description="Model research"))


def test_advanced_german_requirement_is_rejected():
    job = make_job(description="English posting. Professional German at C1 level is required.")
    assert not passes_language_filter(job)


def test_score_is_explainable_and_routes_notifications():
    strong = make_job(
        description=(
            "Build an NGO web platform with React, Next.js, TypeScript, Python, "
            "Django, GraphQL, PostgreSQL and accessibility."
        )
    )
    strong.is_ngo = True
    apply_eligibility(strong)
    assert compute_match_score(strong) >= 70
    assert strong.notification_tier == "immediate"
    assert set(strong.match_breakdown) == {
        "role", "stack", "seniority", "mission", "work_model",
    }

    borderline = make_job(
        title="Web Developer",
        description="Develop web applications with React.",
    )
    apply_eligibility(borderline)
    score = compute_match_score(borderline)
    assert 45 <= score < 70
    assert borderline.notification_tier == "digest"

    mixed = make_job(
        title="Senior FullStack Kotlin / React Engineer",
        description="Build the Kotlin backend and React frontend.",
    )
    apply_eligibility(mixed)
    assert compute_match_score(mixed) < 70
    assert mixed.notification_tier == "digest"


def test_database_schema_contains_explainable_routing_fields():
    with sqlite3.connect(":memory:") as db:
        db.execute(_CREATE_TABLE)
        columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
    assert {
        "workplace_type", "eligible_countries", "eligible_regions",
        "match_breakdown", "match_reasons", "eligibility_status",
        "eligibility_reasons", "notification_tier",
    } <= columns


class FakeResponse:
    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text
        self.status_code = 200

    def json(self):
        return self._payload


def test_greenhouse_contract_parses_full_content_and_location():
    source = GreenhouseSource()
    source._get = AsyncMock(return_value=FakeResponse({
        "jobs": [{
            "id": 1,
            "title": "Senior Frontend Engineer",
            "updated_at": "2026-07-01T10:00:00Z",
            "location": {"name": "Berlin, Germany (Hybrid)"},
            "absolute_url": "https://example.test/gh/1",
            "content": "&lt;p&gt;React and TypeScript product work&lt;/p&gt;",
            "departments": [{"name": "Engineering"}],
            "offices": [{"name": "Berlin"}],
        }]
    }))
    board = CompanyBoard("Example", "greenhouse", "example")
    jobs = asyncio.run(source._fetch_board(board))
    assert jobs[0].workplace_type == "hybrid"
    assert jobs[0].eligible_countries == ["de"]
    assert "React and TypeScript" in jobs[0].description


def test_lever_contract_uses_all_locations_and_plain_description():
    source = LeverSource()
    source._get = AsyncMock(return_value=FakeResponse([{
        "id": "1",
        "text": "Senior Full-Stack Engineer",
        "categories": {
            "allLocations": ["Germany", "France"],
            "team": "Engineering",
            "commitment": "Full-time",
        },
        "workplaceType": "remote",
        "descriptionPlain": "React TypeScript Python",
        "hostedUrl": "https://example.test/lever/1",
    }]))
    board = CompanyBoard("Example", "lever", "example")
    jobs = asyncio.run(source._fetch_board(board))
    assert jobs[0].workplace_type == "remote"
    assert jobs[0].eligible_countries == ["de", "fr"]
    assert apply_eligibility(jobs[0])


def test_workable_contract_parses_public_widget_shape():
    source = WorkableSource()
    source._get = AsyncMock(return_value=FakeResponse({
        "jobs": [{
            "title": "Frontend Engineer",
            "url": "https://example.test/workable/1",
            "location": {
                "location_str": "Germany",
                "telecommuting": True,
                "workplace_type": "remote",
            },
            "description": "<p>React and Vue</p>",
            "created_at": "2026-07-01T10:00:00Z",
        }]
    }))
    board = CompanyBoard("Example", "workable", "example")
    jobs = asyncio.run(source._fetch_board(board))
    assert jobs[0].eligible_countries == ["de"]
    assert jobs[0].workplace_type == "remote"


def test_jsonld_contract_extracts_direct_public_job_posting():
    posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Senior Frontend Engineer",
        "description": "<p>React and TypeScript</p>",
        "datePosted": "2026-07-01",
        "jobLocationType": "TELECOMMUTE",
        "applicantLocationRequirements": {
            "@type": "Country",
            "name": "Germany",
        },
        "url": "https://example.test/jobs/1",
    }
    html = (
        '<html><script type="application/ld+json">'
        + json.dumps(posting)
        + "</script></html>"
    )
    source = JsonLdCareerSource()
    source._get = AsyncMock(return_value=FakeResponse(text=html))
    board = CompanyBoard("Example", "jsonld", "example", url="https://example.test/jobs")
    jobs = asyncio.run(source._fetch_board(board))
    assert jobs[0].eligible_countries == ["de"]
    assert jobs[0].workplace_type == "remote"


class DummySource(BaseSource):
    name = "dummy"

    async def fetch(self):
        return []


class FakeClient:
    responses: list[httpx.Response] = []
    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, **kwargs):
        type(self).calls += 1
        return type(self).responses.pop(0)


def response(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("GET", "https://example.test"))


def test_permanent_http_failure_is_not_retried():
    FakeClient.responses = [response(404)]
    FakeClient.calls = 0
    with patch("sources.base.httpx.AsyncClient", FakeClient):
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(DummySource()._get("https://example.test"))
    assert FakeClient.calls == 1


def test_server_failure_is_retried_then_recovers():
    FakeClient.responses = [response(503), response(200)]
    FakeClient.calls = 0
    with (
        patch("sources.base.httpx.AsyncClient", FakeClient),
        patch("sources.base.asyncio.sleep", new=AsyncMock()),
    ):
        result = asyncio.run(DummySource()._get("https://example.test"))
    assert result.status_code == 200
    assert FakeClient.calls == 2
