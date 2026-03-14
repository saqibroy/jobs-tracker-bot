"""Tests for the Idealist source (Algolia integration)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from sources.idealist import IdealistSource


# ── Sample Algolia response hit ───────────────────────────────────────────
def _make_hit(overrides: dict | None = None) -> dict:
    """Return a realistic Algolia hit dict."""
    hit = {
        "objectID": "abc123",
        "type": "JOB",
        "name": "Senior Software Engineer",
        "orgName": "Save The Planet Foundation",
        "orgType": "NONPROFIT",
        "locationType": "REMOTE",
        "remoteZone": "WORLDWIDE",
        "remoteCountry": None,
        "city": None,
        "state": None,
        "stateStr": None,
        "country": "US",
        "description": "We are looking for a senior engineer...",
        "url": {"en": "/en/nonprofit-job/abc123-senior-software-engineer"},
        "published": 1773500000,  # unix epoch
        "keywords": ["Engineering", "Technology"],
        "areasOfFocus": ["ENVIRONMENT"],
        "functions": ["SOFTWARE_DEVELOPMENT"],
        "salaryCurrency": "USD",
        "salaryMinimum": 90000.0,
        "salaryMaximum": 130000.0,
        "salaryPeriod": "YEAR",
        "remoteOk": True,
    }
    if overrides:
        hit.update(overrides)
    return hit


def _algolia_response(hits: list[dict], nb_hits: int | None = None) -> dict:
    """Wrap hits in a mock Algolia response."""
    return {
        "hits": hits,
        "nbHits": nb_hits if nb_hits is not None else len(hits),
        "page": 0,
        "nbPages": 1,
        "hitsPerPage": 50,
    }


# ── Helpers ────────────────────────────────────────────────────────────────
class TestIdealistParseHit:
    """Tests for _parse_hit() field mapping."""

    def setup_method(self):
        self.source = IdealistSource()

    def test_basic_fields(self):
        job = self.source._parse_hit(_make_hit())
        assert job is not None
        assert job.title == "Senior Software Engineer"
        assert job.company == "Save The Planet Foundation"
        assert job.source == "idealist"
        assert job.is_remote is True
        assert job.remote_scope == "worldwide"
        assert job.url == "https://www.idealist.org/en/nonprofit-job/abc123-senior-software-engineer"

    def test_salary_formatting(self):
        job = self.source._parse_hit(_make_hit())
        assert job.salary == "$90,000 – $130,000/yr"

    def test_salary_min_only(self):
        job = self.source._parse_hit(_make_hit({"salaryMaximum": None}))
        assert job.salary == "$90,000+/yr"

    def test_salary_max_only(self):
        job = self.source._parse_hit(_make_hit({"salaryMinimum": None}))
        assert job.salary == "Up to $130,000/yr"

    def test_no_salary(self):
        job = self.source._parse_hit(_make_hit({
            "salaryMinimum": None,
            "salaryMaximum": None,
        }))
        assert job.salary is None

    def test_eur_salary(self):
        job = self.source._parse_hit(_make_hit({
            "salaryCurrency": "EUR",
            "salaryMinimum": 50000,
            "salaryMaximum": 70000,
            "salaryPeriod": "YEAR",
        }))
        assert job.salary == "€50,000 – €70,000/yr"

    def test_monthly_salary(self):
        job = self.source._parse_hit(_make_hit({
            "salaryMinimum": 5000,
            "salaryMaximum": 8000,
            "salaryPeriod": "MONTH",
        }))
        assert job.salary == "$5,000 – $8,000/mo"

    def test_ngo_from_orgtype(self):
        job = self.source._parse_hit(_make_hit({"orgType": "NONPROFIT"}))
        assert job.is_ngo is True

    def test_not_ngo_from_orgtype(self):
        job = self.source._parse_hit(_make_hit({"orgType": "CONSULTANT"}))
        assert job.is_ngo is False

    def test_ngo_social_enterprise(self):
        job = self.source._parse_hit(_make_hit({"orgType": "SOCIAL_ENTERPRISE"}))
        assert job.is_ngo is True

    def test_posted_at(self):
        job = self.source._parse_hit(_make_hit({"published": 1700000000}))
        assert job.posted_at == datetime.fromtimestamp(1700000000, tz=timezone.utc)

    def test_tags_merged(self):
        job = self.source._parse_hit(_make_hit())
        expected = {"Engineering", "Technology", "ENVIRONMENT", "SOFTWARE_DEVELOPMENT"}
        assert set(job.tags) == expected

    def test_location_worldwide(self):
        job = self.source._parse_hit(_make_hit({
            "remoteZone": "WORLDWIDE",
            "city": None,
            "country": None,
        }))
        assert job.location == "Remote (Worldwide)"

    def test_location_country_remote(self):
        job = self.source._parse_hit(_make_hit({
            "remoteZone": "COUNTRY",
            "remoteCountry": "DE",
            "city": "Berlin",
            "country": "DE",
        }))
        assert "Berlin" in job.location
        assert "Remote (DE)" in job.location

    def test_location_no_geo(self):
        job = self.source._parse_hit(_make_hit({
            "remoteZone": None,
            "remoteCountry": None,
            "city": None,
            "state": None,
            "stateStr": None,
            "country": None,
        }))
        assert job.location == "Remote"

    def test_skip_missing_name(self):
        job = self.source._parse_hit(_make_hit({"name": ""}))
        assert job is None

    def test_skip_missing_org(self):
        job = self.source._parse_hit(_make_hit({"orgName": ""}))
        assert job is None

    def test_skip_missing_url(self):
        job = self.source._parse_hit(_make_hit({"url": {}}))
        assert job is None

    def test_description_truncated(self):
        long_desc = "x" * 10000
        job = self.source._parse_hit(_make_hit({"description": long_desc}))
        assert len(job.description) == 5000


class TestIdealistFetch:
    """Tests for the full fetch() method with mocked HTTP."""

    @pytest.mark.asyncio
    async def test_successful_fetch_deduplicates(self):
        """Two Algolia queries may return overlapping hits; fetch() deduplicates."""
        source = IdealistSource()
        hit_a = _make_hit()  # objectID = abc123
        hit_b = _make_hit({"objectID": "def456", "name": "DevOps Engineer",
                           "url": {"en": "/en/nonprofit-job/def456-devops"}})
        # Query 1 returns hit_a + hit_b; query 2 returns hit_a again (duplicate)
        resp1 = MagicMock()
        resp1.json.return_value = _algolia_response([hit_a, hit_b])
        resp2 = MagicMock()
        resp2.json.return_value = _algolia_response([hit_a])

        async def fake_post(*, query, filters):
            if "TECHNOLOGY_IT" in filters:
                return resp1
            return resp2

        with patch.object(source, "_post_algolia", side_effect=fake_post):
            jobs = await source.fetch()

        assert len(jobs) == 2  # abc123 not duplicated
        titles = {j.title for j in jobs}
        assert titles == {"Senior Software Engineer", "DevOps Engineer"}

    @pytest.mark.asyncio
    async def test_all_queries_fail_returns_empty(self):
        source = IdealistSource()

        async def fake_post(*, query, filters):
            return None

        with patch.object(source, "_post_algolia", side_effect=fake_post):
            jobs = await source.fetch()

        assert jobs == []

    @pytest.mark.asyncio
    async def test_one_query_fails_other_succeeds(self):
        source = IdealistSource()
        hit = _make_hit()
        resp = MagicMock()
        resp.json.return_value = _algolia_response([hit])

        async def fake_post(*, query, filters):
            if "TECHNOLOGY_IT" in filters:
                return None  # first query fails
            return resp  # second query succeeds

        with patch.object(source, "_post_algolia", side_effect=fake_post):
            jobs = await source.fetch()

        assert len(jobs) == 1

    @pytest.mark.asyncio
    async def test_malformed_hit_skipped(self):
        source = IdealistSource()
        good = _make_hit()
        bad = {"objectID": "bad", "name": None}  # will fail validation
        resp = MagicMock()
        resp.json.return_value = _algolia_response([good, bad])
        empty = MagicMock()
        empty.json.return_value = _algolia_response([])

        async def fake_post(*, query, filters):
            if "TECHNOLOGY_IT" in filters:
                return resp
            return empty

        with patch.object(source, "_post_algolia", side_effect=fake_post):
            jobs = await source.fetch()

        assert len(jobs) == 1
        assert jobs[0].title == "Senior Software Engineer"


class TestBuildLocation:
    """Tests for _build_location static method."""

    def test_worldwide(self):
        assert IdealistSource._build_location({"remoteZone": "WORLDWIDE"}) == "Remote (Worldwide)"

    def test_country_with_city(self):
        loc = IdealistSource._build_location({
            "city": "Berlin",
            "stateStr": None,
            "country": "DE",
            "remoteZone": "COUNTRY",
            "remoteCountry": "DE",
        })
        assert loc == "Berlin, DE · Remote (DE)"

    def test_no_data(self):
        assert IdealistSource._build_location({}) == "Remote"

    def test_city_state_country(self):
        loc = IdealistSource._build_location({
            "city": "Portland",
            "stateStr": "Oregon",
            "country": "US",
            "remoteZone": "COUNTRY",
            "remoteCountry": "US",
        })
        assert loc == "Portland, Oregon, US · Remote (US)"


class TestBuildSalary:
    """Tests for _build_salary static method."""

    def test_range(self):
        assert IdealistSource._build_salary({
            "salaryCurrency": "USD",
            "salaryMinimum": 60000,
            "salaryMaximum": 80000,
            "salaryPeriod": "YEAR",
        }) == "$60,000 – $80,000/yr"

    def test_gbp(self):
        assert IdealistSource._build_salary({
            "salaryCurrency": "GBP",
            "salaryMinimum": 40000,
            "salaryMaximum": 60000,
            "salaryPeriod": "YEAR",
        }) == "£40,000 – £60,000/yr"

    def test_unknown_currency(self):
        result = IdealistSource._build_salary({
            "salaryCurrency": "CHF",
            "salaryMinimum": 100000,
            "salaryMaximum": 150000,
            "salaryPeriod": "YEAR",
        })
        assert result == "CHF 100,000 – CHF 150,000/yr"

    def test_none_values(self):
        assert IdealistSource._build_salary({}) is None


class TestClassifyRemoteScope:
    """Tests for _classify_remote_scope mapping from Algolia remoteZone."""

    def test_world_zone(self):
        """remoteZone=WORLD → worldwide."""
        assert IdealistSource._classify_remote_scope(
            {"remoteZone": "WORLD", "remoteCountry": None}
        ) == "worldwide"

    def test_country_eu_de(self):
        """remoteZone=COUNTRY + EU country code → eu."""
        assert IdealistSource._classify_remote_scope(
            {"remoteZone": "COUNTRY", "remoteCountry": "DE"}
        ) == "eu"

    def test_country_eu_fr(self):
        assert IdealistSource._classify_remote_scope(
            {"remoteZone": "COUNTRY", "remoteCountry": "FR"}
        ) == "eu"

    def test_country_eu_nl(self):
        assert IdealistSource._classify_remote_scope(
            {"remoteZone": "COUNTRY", "remoteCountry": "NL"}
        ) == "eu"

    def test_country_eu_es(self):
        assert IdealistSource._classify_remote_scope(
            {"remoteZone": "COUNTRY", "remoteCountry": "ES"}
        ) == "eu"

    def test_country_non_eu_us(self):
        """remoteZone=COUNTRY + non-EU code → restricted (country-locked)."""
        assert IdealistSource._classify_remote_scope(
            {"remoteZone": "COUNTRY", "remoteCountry": "US"}
        ) == "restricted"

    def test_country_non_eu_ng(self):
        assert IdealistSource._classify_remote_scope(
            {"remoteZone": "COUNTRY", "remoteCountry": "NG"}
        ) == "restricted"

    def test_country_non_eu_br(self):
        assert IdealistSource._classify_remote_scope(
            {"remoteZone": "COUNTRY", "remoteCountry": "BR"}
        ) == "restricted"

    def test_state_zone(self):
        """remoteZone=STATE → restricted (geo-locked to a region)."""
        assert IdealistSource._classify_remote_scope(
            {"remoteZone": "STATE", "remoteCountry": "US"}
        ) == "restricted"

    def test_city_zone(self):
        """remoteZone=CITY → restricted."""
        assert IdealistSource._classify_remote_scope(
            {"remoteZone": "CITY", "remoteCountry": "US"}
        ) == "restricted"

    def test_missing_zone(self):
        """No remoteZone → worldwide (Idealist default)."""
        assert IdealistSource._classify_remote_scope({}) == "worldwide"

    def test_none_zone(self):
        assert IdealistSource._classify_remote_scope(
            {"remoteZone": None, "remoteCountry": None}
        ) == "worldwide"

    def test_empty_string_zone(self):
        assert IdealistSource._classify_remote_scope(
            {"remoteZone": "", "remoteCountry": ""}
        ) == "worldwide"

    def test_case_insensitive_zone(self):
        """Zone value is uppercased internally."""
        assert IdealistSource._classify_remote_scope(
            {"remoteZone": "world", "remoteCountry": None}
        ) == "worldwide"

    def test_case_insensitive_country(self):
        assert IdealistSource._classify_remote_scope(
            {"remoteZone": "COUNTRY", "remoteCountry": "de"}
        ) == "eu"

    def test_parsed_job_has_scope_worldwide(self):
        """A parsed Job from a WORLD-zone hit gets remote_scope=worldwide."""
        source = IdealistSource()
        job = source._parse_hit(_make_hit({"remoteZone": "WORLD"}))
        assert job.remote_scope == "worldwide"

    def test_parsed_job_has_scope_eu(self):
        """A parsed Job from a COUNTRY/DE hit gets remote_scope=eu."""
        source = IdealistSource()
        job = source._parse_hit(_make_hit({
            "remoteZone": "COUNTRY", "remoteCountry": "DE"
        }))
        assert job.remote_scope == "eu"

    def test_parsed_job_us_country_gets_restricted(self):
        """US-only remote on Idealist maps to restricted — will be rejected."""
        source = IdealistSource()
        job = source._parse_hit(_make_hit({
            "remoteZone": "COUNTRY", "remoteCountry": "US"
        }))
        assert job.remote_scope == "restricted"
