"""Tests for the Ashby, Personio, and BambooHR ATS source modules."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config
from sources.ashby import AshbySource
from sources.bamboohr import BambooHRSource
from sources.personio import PersonioSource
from sources.registry import CompanyBoard


def _mock_response(status_code=200, json_data=None, text_data=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_data)
    resp.text = text_data
    return resp


# ── Ashby ────────────────────────────────────────────────────────────────
class TestAshbySource:
    @pytest.mark.asyncio
    async def test_no_companies_configured_returns_empty(self, monkeypatch):
        monkeypatch.setattr("sources.ashby.boards_for", lambda provider: [])
        source = AshbySource()
        assert await source.fetch() == []

    @pytest.mark.asyncio
    async def test_parses_jobs_and_builds_location_from_secondary(self, monkeypatch):
        monkeypatch.setattr(
            "sources.ashby.boards_for",
            lambda provider: [CompanyBoard(company="acme", provider="ashby", slug="acme")],
        )
        payload = {
            "jobs": [
                {
                    "title": "Senior Backend Engineer",
                    "location": "Berlin, Germany",
                    "secondaryLocations": [{"location": "Remote - EU"}],
                    "department": "Engineering",
                    "team": "Platform",
                    "isListed": True,
                    "isRemote": True,
                    "workplaceType": "Remote",
                    "descriptionPlain": "Join our backend team.",
                    "publishedAt": "2026-06-01T10:00:00.000+00:00",
                    "jobUrl": "https://jobs.ashbyhq.com/acme/123",
                }
            ]
        }
        source = AshbySource()
        with patch.object(source, "_get", new=AsyncMock(return_value=_mock_response(json_data=payload))):
            jobs = await source.fetch()

        assert len(jobs) == 1
        job = jobs[0]
        assert job.title == "Senior Backend Engineer"
        assert job.company == "acme"
        assert "Berlin, Germany" in job.location
        assert "Remote - EU" in job.location
        assert job.is_remote is True
        assert job.source == "ashby"

    @pytest.mark.asyncio
    async def test_404_board_returns_empty_without_crashing(self, monkeypatch):
        monkeypatch.setattr(
            "sources.ashby.boards_for",
            lambda provider: [CompanyBoard(company="doesnotexist", provider="ashby", slug="doesnotexist")],
        )
        source = AshbySource()
        with patch.object(source, "_get", new=AsyncMock(return_value=_mock_response(status_code=404))):
            jobs = await source.fetch()
        assert jobs == []


# ── Personio ─────────────────────────────────────────────────────────────
_PERSONIO_XML = """<?xml version="1.0" encoding="UTF-8"?>
<workzag-jobs>
  <position>
    <id>4103</id>
    <office>Berlin (Remote)</office>
    <department>Engineering</department>
    <recruitingCategory>Various</recruitingCategory>
    <name>Full Stack Developer</name>
    <employmentType>full-time</employmentType>
    <schedule>Remote</schedule>
    <keywords>python, react, remote</keywords>
    <jobDescriptions>
      <jobDescription>
        <name>About the role</name>
        <value><![CDATA[We are hiring a full stack developer, remote friendly.]]></value>
      </jobDescription>
    </jobDescriptions>
  </position>
  <position>
    <id>4104</id>
    <office>Warsaw</office>
    <department>Sales</department>
    <name>Sales Manager</name>
    <schedule>On-site</schedule>
    <keywords>sales</keywords>
    <jobDescriptions>
      <jobDescription>
        <name>About the role</name>
        <value><![CDATA[On-site role based in our Warsaw office.]]></value>
      </jobDescription>
    </jobDescriptions>
  </position>
</workzag-jobs>
"""


class TestPersonioSource:
    @pytest.mark.asyncio
    async def test_no_companies_configured_returns_empty(self, monkeypatch):
        monkeypatch.setattr("sources.personio.boards_for", lambda provider: [])
        source = PersonioSource()
        assert await source.fetch() == []

    @pytest.mark.asyncio
    async def test_parses_xml_feed(self, monkeypatch):
        monkeypatch.setattr(
            "sources.personio.boards_for",
            lambda provider: [CompanyBoard(company="acme", provider="personio", slug="acme")],
        )
        source = PersonioSource()
        with patch.object(source, "_get", new=AsyncMock(return_value=_mock_response(text_data=_PERSONIO_XML))):
            jobs = await source.fetch()

        assert len(jobs) == 2
        remote_job = next(j for j in jobs if j.title == "Full Stack Developer")
        assert remote_job.location == "Berlin (Remote)"
        assert remote_job.is_remote is True
        assert "full stack developer" in remote_job.description.lower()
        assert remote_job.url == "https://acme.jobs.personio.de/job/4103"

        onsite_job = next(j for j in jobs if j.title == "Sales Manager")
        assert onsite_job.is_remote is False

    @pytest.mark.asyncio
    async def test_malformed_xml_returns_empty_without_crashing(self, monkeypatch):
        monkeypatch.setattr(
            "sources.personio.boards_for",
            lambda provider: [CompanyBoard(company="acme", provider="personio", slug="acme")],
        )
        source = PersonioSource()

        async def fake_get(url, **kwargs):
            if url.endswith("/xml"):
                return _mock_response(text_data="not xml <<<")
            raise RuntimeError("html fallback unavailable")

        with patch.object(source, "_get", new=fake_get):
            jobs = await source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_html_fallback_parses_public_personio_pages(self, monkeypatch):
        monkeypatch.setattr(
            "sources.personio.boards_for",
            lambda provider: [CompanyBoard(company="acme", provider="personio", slug="acme")],
        )
        listing_html = """
        <html><body>
          <a href="/job/123">Senior Frontend Engineer Berlin / Remote Berlin Full-time</a>
          <a href="https://acme.jobs.personio.de/job/456">Sales Manager Paris Full-time</a>
        </body></html>
        """
        detail_html = """
        <html><body>
          <h1 class="job-position-title">Senior Frontend Engineer Berlin / Remote</h1>
          <div class="JobAttributes_jobMetaItemLocation__MX4Xg">Berlin</div>
          <div class="page_jobDescription__1wA05">
            <h2>Your mission</h2>
            <p>Build product features with React, TypeScript and Python.</p>
            <p>This role can be done onsite in Berlin or remote from Germany.</p>
          </div>
        </body></html>
        """

        async def fake_get(url, **kwargs):
            if url.endswith("/xml"):
                return _mock_response(status_code=404)
            if url == "https://acme.jobs.personio.de":
                return _mock_response(text_data=listing_html)
            if url == "https://acme.jobs.personio.de/job/123":
                return _mock_response(text_data=detail_html)
            raise RuntimeError("detail unavailable")

        source = PersonioSource()
        with patch.object(source, "_get", new=fake_get):
            jobs = await source.fetch()

        assert len(jobs) == 2
        parsed = next(job for job in jobs if job.url.endswith("/job/123"))
        assert parsed.title == "Senior Frontend Engineer Berlin / Remote"
        assert parsed.location == "Berlin"
        assert parsed.workplace_type == "hybrid"
        assert parsed.is_remote is True
        assert "React" in parsed.description
        assert "de" in parsed.eligible_countries

        card_only = next(job for job in jobs if job.url.endswith("/job/456"))
        assert card_only.title.startswith("Sales Manager Paris")


# ── BambooHR ─────────────────────────────────────────────────────────────
class TestBambooHRSource:
    @pytest.mark.asyncio
    async def test_no_companies_configured_returns_empty(self, monkeypatch):
        monkeypatch.setattr(config, "BAMBOOHR_COMPANIES", [])
        source = BambooHRSource()
        assert await source.fetch() == []

    @pytest.mark.asyncio
    async def test_parses_list_response(self, monkeypatch):
        monkeypatch.setattr(config, "BAMBOOHR_COMPANIES", ["acme"])
        payload = [
            {
                "id": 55,
                "jobOpeningName": "Platform Engineer",
                "location": {"city": "Berlin", "country": "Germany"},
                "locationLabel": "Berlin, Germany (Remote)",
                "department": "Engineering",
                "description": "Build our platform.",
            }
        ]
        source = BambooHRSource()
        with patch.object(source, "_get", new=AsyncMock(return_value=_mock_response(json_data=payload))):
            jobs = await source.fetch()

        assert len(jobs) == 1
        job = jobs[0]
        assert job.title == "Platform Engineer"
        assert job.location == "Berlin, Germany"
        assert job.is_remote is True
        assert job.url == "https://acme.bamboohr.com/careers/55"

    @pytest.mark.asyncio
    async def test_non_json_response_returns_empty_without_crashing(self, monkeypatch):
        monkeypatch.setattr(config, "BAMBOOHR_COMPANIES", ["acme"])
        resp = _mock_response()
        resp.json = MagicMock(side_effect=ValueError("no json"))
        source = BambooHRSource()
        with patch.object(source, "_get", new=AsyncMock(return_value=resp)):
            jobs = await source.fetch()
        assert jobs == []
