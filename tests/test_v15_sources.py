"""Tests for v1.5 Part B sources: NoFluffJobs, Himalayas, Landing.jobs, The Muse.

Covers:
  - Each source: API response parsing, filtering, edge-cases
  - Remote scope inference
  - Salary formatting
  - Deduplication
  - Error handling
  - Source registration in main.py (17 sources total)
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models.job import Job


# ═══════════════════════════════════════════════════════════════════════════
#  Shared helpers
# ═══════════════════════════════════════════════════════════════════════════

# Recent timestamp (1 day ago) for test fixtures — epoch ms
_RECENT_TS_MS = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp() * 1000)
_RECENT_TS_MS_2 = int((datetime.now(timezone.utc) - timedelta(days=2)).timestamp() * 1000)
_OLD_TS_MS = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000)


# ═══════════════════════════════════════════════════════════════════════════
#  Shared helpers
# ═══════════════════════════════════════════════════════════════════════════

def _mock_response(json_data=None, status_code: int = 200):
    """Create a mock httpx response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = ""
    resp.json = MagicMock(return_value=json_data or {})
    return resp


# ═══════════════════════════════════════════════════════════════════════════
#  NoFluffJobs
# ═══════════════════════════════════════════════════════════════════════════

SAMPLE_NOFLUFF_RESPONSE = {
    "postings": [
        {
            "id": "senior-backend-dev-acme-remote",
            "name": "Acme Corp",
            "title": "Senior Backend Developer",
            "url": "senior-backend-dev-acme-remote",
            "category": "backend",
            "seniority": ["Senior"],
            "technology": "Python",
            "fullyRemote": True,
            "regions": ["pl"],
            "posted": _RECENT_TS_MS,  # recent — should pass age filter
            "salary": {
                "from": 18000.0,
                "to": 25000.0,
                "type": "b2b",
                "currency": "PLN",
            },
            "location": {
                "places": [
                    {"city": "Remote", "url": "senior-backend-dev-acme-remote"},
                    {
                        "country": {"code": "POL", "name": "Poland"},
                        "city": "Warszawa",
                        "url": "senior-backend-dev-acme-warszawa",
                    },
                ],
                "fullyRemote": True,
            },
        },
        {
            "id": "devops-engineer-beta-krakow",
            "name": "Beta GmbH",
            "title": "DevOps Engineer",
            "url": "devops-engineer-beta-krakow",
            "category": "devops",
            "seniority": ["Mid"],
            "technology": "Kubernetes",
            "fullyRemote": True,
            "regions": ["pl", "de"],
            "posted": _RECENT_TS_MS_2,
            "salary": None,
            "location": {
                "places": [
                    {
                        "country": {"code": "POL", "name": "Poland"},
                        "city": "Kraków",
                        "url": "devops-engineer-beta-krakow",
                    },
                ],
                "fullyRemote": True,
            },
        },
        {
            # NOT remote — should be filtered out
            "id": "qa-engineer-gamma-onsite",
            "name": "Gamma Sp",
            "title": "QA Engineer",
            "url": "qa-engineer-gamma-onsite",
            "category": "testing",
            "seniority": ["Junior"],
            "fullyRemote": False,
            "regions": ["pl"],
            "posted": _RECENT_TS_MS,
            "salary": None,
            "location": {"places": [], "fullyRemote": False},
        },
        {
            # Wrong category (sales) — should be filtered out
            "id": "sales-manager-delta",
            "name": "Delta",
            "title": "Sales Manager",
            "url": "sales-manager-delta",
            "category": "sales",
            "seniority": ["Senior"],
            "fullyRemote": True,
            "regions": ["pl"],
            "posted": _RECENT_TS_MS,
            "salary": None,
            "location": {"places": [], "fullyRemote": True},
        },
    ],
    "totalCount": 4,
}


class TestNoFluffJobsSource:
    def setup_method(self):
        from sources.nofluffjobs import NoFluffJobsSource
        self.source = NoFluffJobsSource()

    # ── Source metadata ────────────────────────────────────────────────

    def test_source_name(self):
        assert self.source.name == "nofluffjobs"

    # ── Parsing ────────────────────────────────────────────────────────

    def test_parse_posting_title(self):
        posting = SAMPLE_NOFLUFF_RESPONSE["postings"][0]
        job = self.source._parse_posting(posting)
        assert job.title == "Senior Backend Developer"

    def test_parse_posting_company(self):
        posting = SAMPLE_NOFLUFF_RESPONSE["postings"][0]
        job = self.source._parse_posting(posting)
        assert job.company == "Acme Corp"

    def test_parse_posting_url(self):
        posting = SAMPLE_NOFLUFF_RESPONSE["postings"][0]
        job = self.source._parse_posting(posting)
        assert job.url == "https://nofluffjobs.com/job/senior-backend-dev-acme-remote"

    def test_parse_posting_location_city(self):
        posting = SAMPLE_NOFLUFF_RESPONSE["postings"][0]
        job = self.source._parse_posting(posting)
        assert "Warszawa" in job.location

    def test_parse_posting_is_remote(self):
        posting = SAMPLE_NOFLUFF_RESPONSE["postings"][0]
        job = self.source._parse_posting(posting)
        assert job.is_remote is True

    def test_parse_posting_salary(self):
        posting = SAMPLE_NOFLUFF_RESPONSE["postings"][0]
        job = self.source._parse_posting(posting)
        assert job.salary is not None
        assert "18,000" in job.salary
        assert "25,000" in job.salary
        assert "PLN" in job.salary

    def test_parse_posting_tags(self):
        posting = SAMPLE_NOFLUFF_RESPONSE["postings"][0]
        job = self.source._parse_posting(posting)
        assert "Senior" in job.tags
        assert "Python" in job.tags

    def test_parse_posting_posted_at(self):
        posting = SAMPLE_NOFLUFF_RESPONSE["postings"][0]
        job = self.source._parse_posting(posting)
        assert job.posted_at is not None

    def test_parse_posting_source(self):
        posting = SAMPLE_NOFLUFF_RESPONSE["postings"][0]
        job = self.source._parse_posting(posting)
        assert job.source == "nofluffjobs"

    def test_parse_posting_empty_title_returns_none(self):
        posting = {"title": "", "url": "x", "name": "X"}
        assert self.source._parse_posting(posting) is None

    def test_parse_posting_no_url_returns_none(self):
        posting = {"title": "Dev", "url": "", "id": "", "name": "X"}
        assert self.source._parse_posting(posting) is None

    def test_parse_posting_missing_company_defaults(self):
        posting = dict(SAMPLE_NOFLUFF_RESPONSE["postings"][0])
        posting["name"] = None
        job = self.source._parse_posting(posting)
        assert job.company == "Unknown"

    # ── Remote scope ───────────────────────────────────────────────────

    def test_remote_scope_single_poland(self):
        """Polish-only remote jobs default to EU scope."""
        scope = self.source._infer_remote_scope(["pl"], "Warszawa")
        assert scope == "eu"

    def test_remote_scope_multi_region(self):
        """Multiple regions → EU."""
        scope = self.source._infer_remote_scope(["pl", "de", "cz"], "Remote")
        assert scope == "eu"

    def test_remote_scope_germany(self):
        scope = self.source._infer_remote_scope(["de"], "Berlin")
        assert scope == "germany"

    def test_remote_scope_worldwide(self):
        scope = self.source._infer_remote_scope([], "Worldwide")
        assert scope == "worldwide"

    # ── Salary formatting ──────────────────────────────────────────────

    def test_format_salary_full_range(self):
        sal = self.source._format_salary({"from": 15000, "to": 25000, "currency": "PLN", "type": "b2b"})
        assert "15,000" in sal
        assert "25,000" in sal
        assert "PLN" in sal
        assert "b2b" in sal

    def test_format_salary_none(self):
        assert self.source._format_salary(None) is None

    def test_format_salary_no_amounts(self):
        assert self.source._format_salary({"from": None, "to": None, "currency": "PLN"}) is None

    # ── Build location ─────────────────────────────────────────────────

    def test_build_location_with_cities(self):
        posting = SAMPLE_NOFLUFF_RESPONSE["postings"][0]
        loc = self.source._build_location(posting)
        assert "Warszawa" in loc

    def test_build_location_remote_only(self):
        posting = {"location": {"places": [{"city": "Remote"}]}}
        loc = self.source._build_location(posting)
        # "Remote" is filtered out → should show country or fallback
        assert loc  # non-empty

    def test_build_location_empty(self):
        loc = self.source._build_location({"location": {"places": []}})
        assert loc == "Remote"

    # ── Fetch (mocked API) ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fetch_filters_by_category_and_remote(self):
        """Only remote + wanted-category postings should pass."""
        mock_resp = _mock_response(json_data=SAMPLE_NOFLUFF_RESPONSE)
        with patch.object(self.source, "_post", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        # 4 postings: 2 remote+wanted category, 1 not remote, 1 wrong category
        assert len(jobs) == 2
        titles = {j.title for j in jobs}
        assert "Senior Backend Developer" in titles
        assert "DevOps Engineer" in titles

    @pytest.mark.asyncio
    async def test_fetch_deduplicates_by_id(self):
        dup_response = {
            "postings": [
                SAMPLE_NOFLUFF_RESPONSE["postings"][0],
                SAMPLE_NOFLUFF_RESPONSE["postings"][0],  # duplicate
            ],
            "totalCount": 2,
        }
        mock_resp = _mock_response(json_data=dup_response)
        with patch.object(self.source, "_post", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert len(jobs) == 1

    @pytest.mark.asyncio
    async def test_fetch_handles_api_error(self):
        mock_resp = _mock_response(status_code=500)
        with patch.object(self.source, "_post", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_fetch_handles_network_error(self):
        with patch.object(self.source, "_post", new_callable=AsyncMock, side_effect=Exception("timeout")):
            jobs = await self.source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_fetch_empty_postings(self):
        mock_resp = _mock_response(json_data={"postings": [], "totalCount": 0})
        with patch.object(self.source, "_post", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert jobs == []


# ═══════════════════════════════════════════════════════════════════════════
#  Himalayas
# ═══════════════════════════════════════════════════════════════════════════

SAMPLE_HIMALAYAS_JOB = {
    "title": "Senior Software Engineer",
    "companyName": "RemoteCo",
    "companySlug": "remoteco",
    "employmentType": "Full Time",
    "minSalary": 80000,
    "maxSalary": 120000,
    "currency": "USD",
    "seniority": ["Senior"],
    "locationRestrictions": ["Germany", "Netherlands"],
    "timezoneRestrictions": [1, 2],
    "categories": ["Software-Engineering", "Backend-Development"],
    "parentCategories": [],
    "description": "Build scalable backend systems.",
    "excerpt": "Build scalable backend systems.",
    "pubDate": 1710000000,
    "expiryDate": 1715000000,
    "applicationLink": "https://himalayas.app/companies/remoteco/jobs/senior-swe",
    "guid": "https://himalayas.app/companies/remoteco/jobs/senior-swe",
}

SAMPLE_HIMALAYAS_JOB_WORLDWIDE = {
    "title": "DevOps Engineer",
    "companyName": "GlobalTech",
    "companySlug": "globaltech",
    "employmentType": "Full Time",
    "minSalary": 90000,
    "maxSalary": 140000,
    "currency": "USD",
    "seniority": ["Mid"],
    "locationRestrictions": [],  # No restrictions = worldwide
    "categories": ["DevOps", "Infrastructure-Engineering"],
    "excerpt": "Cloud infrastructure at scale.",
    "pubDate": 1710100000,
    "applicationLink": "https://himalayas.app/companies/globaltech/jobs/devops",
    "guid": "https://himalayas.app/companies/globaltech/jobs/devops",
}

SAMPLE_HIMALAYAS_JOB_US_ONLY = {
    "title": "Frontend Developer",
    "companyName": "USOnlyCo",
    "companySlug": "usonlyco",
    "employmentType": "Full Time",
    "minSalary": 70000,
    "maxSalary": 100000,
    "currency": "USD",
    "seniority": ["Mid"],
    "locationRestrictions": ["United States"],  # US-only
    "categories": ["Frontend-Development"],
    "excerpt": "React development.",
    "pubDate": 1710200000,
    "applicationLink": "https://himalayas.app/companies/usonlyco/jobs/frontend",
    "guid": "https://himalayas.app/companies/usonlyco/jobs/frontend",
}

SAMPLE_HIMALAYAS_JOB_NON_TECH = {
    "title": "Sales Manager",
    "companyName": "SalesCo",
    "companySlug": "salesco",
    "employmentType": "Full Time",
    "minSalary": 50000,
    "maxSalary": 80000,
    "currency": "USD",
    "seniority": ["Senior"],
    "locationRestrictions": ["Germany"],
    "categories": ["Sales-Operations", "Account-Management"],
    "excerpt": "Manage sales pipeline.",
    "pubDate": 1710300000,
    "applicationLink": "https://himalayas.app/companies/salesco/jobs/sales",
    "guid": "https://himalayas.app/companies/salesco/jobs/sales",
}


class TestHimalayasSource:
    def setup_method(self):
        from sources.himalayas import HimalayasSource
        self.source = HimalayasSource()

    # ── Source metadata ────────────────────────────────────────────────

    def test_source_name(self):
        assert self.source.name == "himalayas"

    # ── Job parsing ────────────────────────────────────────────────────

    def test_parse_job_title(self):
        job = self.source._parse_job(SAMPLE_HIMALAYAS_JOB)
        assert job.title == "Senior Software Engineer"

    def test_parse_job_company(self):
        job = self.source._parse_job(SAMPLE_HIMALAYAS_JOB)
        assert job.company == "RemoteCo"

    def test_parse_job_url(self):
        job = self.source._parse_job(SAMPLE_HIMALAYAS_JOB)
        assert "himalayas.app" in job.url

    def test_parse_job_location_from_restrictions(self):
        job = self.source._parse_job(SAMPLE_HIMALAYAS_JOB)
        assert "Germany" in job.location
        assert "Netherlands" in job.location

    def test_parse_job_worldwide_location(self):
        job = self.source._parse_job(SAMPLE_HIMALAYAS_JOB_WORLDWIDE)
        assert "Worldwide" in job.location or "Remote" in job.location

    def test_parse_job_salary(self):
        job = self.source._parse_job(SAMPLE_HIMALAYAS_JOB)
        assert job.salary is not None
        assert "80,000" in job.salary
        assert "120,000" in job.salary
        assert "USD" in job.salary

    def test_parse_job_tags(self):
        job = self.source._parse_job(SAMPLE_HIMALAYAS_JOB)
        assert len(job.tags) > 0
        assert any("Software" in t for t in job.tags)

    def test_parse_job_posted_at(self):
        job = self.source._parse_job(SAMPLE_HIMALAYAS_JOB)
        assert job.posted_at is not None

    def test_parse_job_is_remote(self):
        job = self.source._parse_job(SAMPLE_HIMALAYAS_JOB)
        assert job.is_remote is True

    def test_parse_job_source(self):
        job = self.source._parse_job(SAMPLE_HIMALAYAS_JOB)
        assert job.source == "himalayas"

    def test_parse_job_description(self):
        job = self.source._parse_job(SAMPLE_HIMALAYAS_JOB)
        assert job.description is not None

    def test_parse_job_empty_title_returns_none(self):
        item = dict(SAMPLE_HIMALAYAS_JOB)
        item["title"] = ""
        assert self.source._parse_job(item) is None

    def test_parse_job_no_url_returns_none(self):
        item = dict(SAMPLE_HIMALAYAS_JOB)
        item["applicationLink"] = ""
        item["guid"] = ""
        assert self.source._parse_job(item) is None

    # ── Remote scope ───────────────────────────────────────────────────

    def test_remote_scope_eu(self):
        job = self.source._parse_job(SAMPLE_HIMALAYAS_JOB)
        assert job.remote_scope == "eu"

    def test_remote_scope_worldwide(self):
        job = self.source._parse_job(SAMPLE_HIMALAYAS_JOB_WORLDWIDE)
        assert job.remote_scope == "worldwide"

    def test_infer_remote_scope_germany(self):
        scope = self.source._infer_remote_scope(["Germany"])
        assert scope == "germany"

    def test_infer_remote_scope_empty(self):
        scope = self.source._infer_remote_scope([])
        assert scope == "worldwide"

    # ── Category filtering ─────────────────────────────────────────────

    def test_is_wanted_category_software(self):
        assert self.source._is_wanted_category(SAMPLE_HIMALAYAS_JOB) is True

    def test_is_wanted_category_devops(self):
        assert self.source._is_wanted_category(SAMPLE_HIMALAYAS_JOB_WORLDWIDE) is True

    def test_is_wanted_category_sales(self):
        assert self.source._is_wanted_category(SAMPLE_HIMALAYAS_JOB_NON_TECH) is False

    def test_is_wanted_category_title_fallback(self):
        """Job with no matching category but 'engineer' in title should pass."""
        item = {"title": "Platform Engineer", "categories": ["Unknown-Category"]}
        assert self.source._is_wanted_category(item) is True

    # ── EU accessibility filtering ─────────────────────────────────────

    def test_is_eu_accessible_eu_countries(self):
        assert self.source._is_eu_accessible(SAMPLE_HIMALAYAS_JOB) is True

    def test_is_eu_accessible_worldwide(self):
        assert self.source._is_eu_accessible(SAMPLE_HIMALAYAS_JOB_WORLDWIDE) is True

    def test_is_eu_accessible_us_only(self):
        assert self.source._is_eu_accessible(SAMPLE_HIMALAYAS_JOB_US_ONLY) is False

    # ── Salary formatting ──────────────────────────────────────────────

    def test_format_salary_range(self):
        sal = self.source._format_salary(SAMPLE_HIMALAYAS_JOB)
        assert "80,000" in sal
        assert "120,000" in sal

    def test_format_salary_none(self):
        item = {"minSalary": None, "maxSalary": None, "currency": "USD"}
        assert self.source._format_salary(item) is None

    def test_format_salary_zero(self):
        item = {"minSalary": 0, "maxSalary": 0, "currency": "USD"}
        assert self.source._format_salary(item) is None

    # ── Fetch (mocked API) ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fetch_filters_and_collects(self):
        """EU tech jobs pass, US-only and non-tech are filtered."""
        page1_data = {
            "jobs": [
                SAMPLE_HIMALAYAS_JOB,
                SAMPLE_HIMALAYAS_JOB_WORLDWIDE,
                SAMPLE_HIMALAYAS_JOB_US_ONLY,
                SAMPLE_HIMALAYAS_JOB_NON_TECH,
            ],
            "totalCount": 4,
        }
        page2_data = {"jobs": [], "totalCount": 4}

        mock_resp1 = _mock_response(json_data=page1_data)
        mock_resp2 = _mock_response(json_data=page2_data)

        with patch.object(
            self.source, "_get", new_callable=AsyncMock,
            side_effect=[mock_resp1, mock_resp2],
        ):
            jobs = await self.source.fetch()

        # EU tech + worldwide tech pass; US-only and non-tech filtered
        assert len(jobs) == 2
        titles = {j.title for j in jobs}
        assert "Senior Software Engineer" in titles
        assert "DevOps Engineer" in titles

    @pytest.mark.asyncio
    async def test_fetch_deduplicates_by_url(self):
        dup_data = {
            "jobs": [SAMPLE_HIMALAYAS_JOB, SAMPLE_HIMALAYAS_JOB],
            "totalCount": 2,
        }
        mock_resp1 = _mock_response(json_data=dup_data)
        mock_resp2 = _mock_response(json_data={"jobs": []})

        with patch.object(
            self.source, "_get", new_callable=AsyncMock,
            side_effect=[mock_resp1, mock_resp2],
        ):
            jobs = await self.source.fetch()
        assert len(jobs) == 1

    @pytest.mark.asyncio
    async def test_fetch_handles_api_error(self):
        mock_resp = _mock_response(status_code=500)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_fetch_handles_network_error(self):
        with patch.object(self.source, "_get", new_callable=AsyncMock, side_effect=Exception("timeout")):
            jobs = await self.source.fetch()
        assert jobs == []


# ═══════════════════════════════════════════════════════════════════════════
#  Landing.jobs
# ═══════════════════════════════════════════════════════════════════════════

SAMPLE_LANDING_JOB = {
    "id": 19001,
    "company_id": 7766,
    "currency_code": "EUR",
    "expires_at": "2026-06-01",
    "main_requirements": "<div>Python, AWS, Docker</div>",
    "title": "DevOps Engineer",
    "created_at": "2026-03-01T10:00:00.000Z",
    "published_at": "2026-03-03T11:55:01.551Z",
    "type": "Full-time",
    "remote": True,
    "gross_salary_low": 50000,
    "gross_salary_high": 73000,
    "tags": ["Python", "CI/CD", "Docker", "AWS"],
    "url": "https://landing.jobs/at/cliftonlarsonallen/devops-engineer-in-lisbon-2025-1",
    "locations": [{"city": "Lisbon", "country_code": "PT"}],
}

SAMPLE_LANDING_JOB_DE = {
    "id": 19002,
    "title": "Senior Backend Developer",
    "published_at": "2026-03-10T09:00:00.000Z",
    "type": "Full-time",
    "remote": True,
    "gross_salary_low": 65000,
    "gross_salary_high": 90000,
    "currency_code": "EUR",
    "tags": ["Java", "Spring Boot", "Kubernetes"],
    "url": "https://landing.jobs/at/techgmbh/senior-backend-dev-berlin",
    "locations": [{"city": "Berlin", "country_code": "DE"}],
}

SAMPLE_LANDING_JOB_NO_SALARY = {
    "id": 19003,
    "title": "Frontend Developer",
    "published_at": "2026-03-15T14:00:00.000Z",
    "type": "Full-time",
    "remote": False,
    "gross_salary_low": None,
    "gross_salary_high": None,
    "currency_code": "EUR",
    "tags": ["React", "TypeScript"],
    "url": "https://landing.jobs/at/webco/frontend-dev-amsterdam",
    "locations": [{"city": "Amsterdam", "country_code": "NL"}],
}


class TestLandingJobsSource:
    def setup_method(self):
        from sources.landingjobs import LandingJobsSource
        self.source = LandingJobsSource()

    # ── Source metadata ────────────────────────────────────────────────

    def test_source_name(self):
        assert self.source.name == "landingjobs"

    # ── Parsing ────────────────────────────────────────────────────────

    def test_parse_posting_title(self):
        job = self.source._parse_posting(SAMPLE_LANDING_JOB)
        assert job.title == "DevOps Engineer"

    def test_parse_posting_company_from_url(self):
        job = self.source._parse_posting(SAMPLE_LANDING_JOB)
        assert "Cliftonlarsonallen" in job.company

    def test_parse_posting_url(self):
        job = self.source._parse_posting(SAMPLE_LANDING_JOB)
        assert "landing.jobs" in job.url

    def test_parse_posting_location(self):
        job = self.source._parse_posting(SAMPLE_LANDING_JOB)
        assert "Lisbon" in job.location
        assert "PT" in job.location

    def test_parse_posting_is_remote(self):
        job = self.source._parse_posting(SAMPLE_LANDING_JOB)
        assert job.is_remote is True

    def test_parse_posting_not_remote(self):
        job = self.source._parse_posting(SAMPLE_LANDING_JOB_NO_SALARY)
        assert job.is_remote is False

    def test_parse_posting_salary(self):
        job = self.source._parse_posting(SAMPLE_LANDING_JOB)
        assert job.salary is not None
        assert "50,000" in job.salary
        assert "73,000" in job.salary
        assert "EUR" in job.salary

    def test_parse_posting_no_salary(self):
        job = self.source._parse_posting(SAMPLE_LANDING_JOB_NO_SALARY)
        assert job.salary is None

    def test_parse_posting_tags(self):
        job = self.source._parse_posting(SAMPLE_LANDING_JOB)
        assert "Python" in job.tags
        assert "Docker" in job.tags

    def test_parse_posting_posted_at(self):
        job = self.source._parse_posting(SAMPLE_LANDING_JOB)
        assert job.posted_at is not None

    def test_parse_posting_source(self):
        job = self.source._parse_posting(SAMPLE_LANDING_JOB)
        assert job.source == "landingjobs"

    def test_parse_posting_empty_title_returns_none(self):
        posting = dict(SAMPLE_LANDING_JOB)
        posting["title"] = ""
        assert self.source._parse_posting(posting) is None

    def test_parse_posting_no_url_returns_none(self):
        posting = dict(SAMPLE_LANDING_JOB)
        posting["url"] = ""
        assert self.source._parse_posting(posting) is None

    # ── Remote scope ───────────────────────────────────────────────────

    def test_remote_scope_germany(self):
        job = self.source._parse_posting(SAMPLE_LANDING_JOB_DE)
        assert job.remote_scope == "germany"

    def test_remote_scope_eu_default(self):
        job = self.source._parse_posting(SAMPLE_LANDING_JOB)
        assert job.remote_scope == "eu"

    # ── Company extraction ─────────────────────────────────────────────

    def test_extract_company_from_url(self):
        company = self.source._extract_company(
            "https://landing.jobs/at/cool-company/some-job"
        )
        assert company == "Cool Company"

    def test_extract_company_fallback(self):
        company = self.source._extract_company("https://other.com/jobs/123")
        assert company == "Unknown"

    # ── Salary formatting ──────────────────────────────────────────────

    def test_format_salary_range(self):
        sal = self.source._format_salary(SAMPLE_LANDING_JOB)
        assert "50,000" in sal
        assert "73,000" in sal
        assert "/year" in sal

    def test_format_salary_none(self):
        sal = self.source._format_salary({"gross_salary_low": None, "gross_salary_high": None})
        assert sal is None

    # ── Build location ─────────────────────────────────────────────────

    def test_build_location_city_country(self):
        loc = self.source._build_location([{"city": "Berlin", "country_code": "DE"}])
        assert "Berlin" in loc
        assert "DE" in loc

    def test_build_location_empty(self):
        loc = self.source._build_location([])
        assert "EU" in loc

    # ── Fetch (mocked API) ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fetch_parses_all_postings(self):
        mock_resp = _mock_response(json_data=[SAMPLE_LANDING_JOB, SAMPLE_LANDING_JOB_DE])
        # Override json() to return a list (not wrapped in dict)
        mock_resp.json.return_value = [SAMPLE_LANDING_JOB, SAMPLE_LANDING_JOB_DE]

        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert len(jobs) == 2

    @pytest.mark.asyncio
    async def test_fetch_deduplicates_by_url(self):
        mock_resp = _mock_response()
        mock_resp.json.return_value = [SAMPLE_LANDING_JOB, SAMPLE_LANDING_JOB]

        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert len(jobs) == 1

    @pytest.mark.asyncio
    async def test_fetch_handles_api_error(self):
        mock_resp = _mock_response(status_code=500)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_fetch_handles_network_error(self):
        with patch.object(self.source, "_get", new_callable=AsyncMock, side_effect=Exception("timeout")):
            jobs = await self.source.fetch()
        assert jobs == []


# ═══════════════════════════════════════════════════════════════════════════
#  The Muse
# ═══════════════════════════════════════════════════════════════════════════

SAMPLE_MUSE_RESULT = {
    "id": 55001,
    "name": "Senior Software Engineer",
    "type": "external",
    "publication_date": "2026-03-15T10:00:00Z",
    "short_name": "senior-swe-55001",
    "model_type": "jobs",
    "locations": [
        {"name": "Flexible / Remote"},
        {"name": "Berlin, Germany"},
    ],
    "categories": [{"name": "Software Engineering"}],
    "levels": [{"name": "Senior Level", "short_name": "senior"}],
    "tags": [],
    "refs": {
        "landing_page": "https://www.themuse.com/jobs/techco/senior-swe-55001",
    },
    "company": {
        "id": 1001,
        "name": "TechCo",
        "short_name": "techco",
    },
    "contents": "Build amazing software...",
}

SAMPLE_MUSE_RESULT_WORLDWIDE = {
    "id": 55002,
    "name": "DevOps Engineer",
    "type": "external",
    "publication_date": "2026-03-16T14:00:00Z",
    "short_name": "devops-55002",
    "model_type": "jobs",
    "locations": [{"name": "Flexible / Remote"}],
    "categories": [{"name": "IT"}],
    "levels": [{"name": "Mid Level", "short_name": "mid"}],
    "tags": [],
    "refs": {
        "landing_page": "https://www.themuse.com/jobs/cloudcorp/devops-55002",
    },
    "company": {"id": 1002, "name": "CloudCorp"},
}


class TestTheMuseSource:
    def setup_method(self):
        from sources.themuse import TheMuseSource
        self.source = TheMuseSource()

    # ── Source metadata ────────────────────────────────────────────────

    def test_source_name(self):
        assert self.source.name == "themuse"

    # ── Parsing ────────────────────────────────────────────────────────

    def test_parse_result_title(self):
        job = self.source._parse_result(SAMPLE_MUSE_RESULT)
        assert job.title == "Senior Software Engineer"

    def test_parse_result_company(self):
        job = self.source._parse_result(SAMPLE_MUSE_RESULT)
        assert job.company == "TechCo"

    def test_parse_result_url(self):
        job = self.source._parse_result(SAMPLE_MUSE_RESULT)
        assert "themuse.com" in job.url

    def test_parse_result_location_filters_remote(self):
        """'Flexible / Remote' should be filtered from location display."""
        job = self.source._parse_result(SAMPLE_MUSE_RESULT)
        assert "Berlin" in job.location

    def test_parse_result_worldwide_location(self):
        """Job with only remote location → 'Remote'."""
        job = self.source._parse_result(SAMPLE_MUSE_RESULT_WORLDWIDE)
        assert job.location == "Remote"

    def test_parse_result_is_remote(self):
        job = self.source._parse_result(SAMPLE_MUSE_RESULT)
        assert job.is_remote is True

    def test_parse_result_tags(self):
        job = self.source._parse_result(SAMPLE_MUSE_RESULT)
        assert "Software Engineering" in job.tags
        assert "Senior Level" in job.tags

    def test_parse_result_posted_at(self):
        job = self.source._parse_result(SAMPLE_MUSE_RESULT)
        assert job.posted_at is not None

    def test_parse_result_source(self):
        job = self.source._parse_result(SAMPLE_MUSE_RESULT)
        assert job.source == "themuse"

    def test_parse_result_empty_title_returns_none(self):
        item = dict(SAMPLE_MUSE_RESULT)
        item["name"] = ""
        assert self.source._parse_result(item) is None

    def test_parse_result_no_url_returns_none(self):
        item = dict(SAMPLE_MUSE_RESULT)
        item["refs"] = {"landing_page": ""}
        assert self.source._parse_result(item) is None

    def test_parse_result_missing_company_defaults(self):
        item = dict(SAMPLE_MUSE_RESULT)
        item["company"] = {"name": None}
        job = self.source._parse_result(item)
        assert job.company == "Unknown"

    # ── Remote scope ───────────────────────────────────────────────────

    def test_remote_scope_germany(self):
        job = self.source._parse_result(SAMPLE_MUSE_RESULT)
        assert job.remote_scope == "germany"  # Berlin, Germany

    def test_remote_scope_worldwide(self):
        job = self.source._parse_result(SAMPLE_MUSE_RESULT_WORLDWIDE)
        assert job.remote_scope == "worldwide"

    def test_infer_remote_scope_eu(self):
        scope = self.source._infer_remote_scope([
            {"name": "Flexible / Remote"},
            {"name": "Amsterdam, Netherlands"},
        ])
        assert scope == "eu"

    # ── Build location ─────────────────────────────────────────────────

    def test_build_location_filters_remote(self):
        loc = self.source._build_location([
            {"name": "Flexible / Remote"},
            {"name": "London, UK"},
        ])
        assert loc == "London, UK"

    def test_build_location_remote_only(self):
        loc = self.source._build_location([{"name": "Flexible / Remote"}])
        assert loc == "Remote"

    def test_build_location_empty(self):
        loc = self.source._build_location([])
        assert loc == "Remote"

    # ── Fetch (mocked API) ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fetch_queries_multiple_categories(self):
        """Should make requests for each category."""
        api_response = {
            "page": 1,
            "page_count": 1,
            "total": 2,
            "results": [SAMPLE_MUSE_RESULT, SAMPLE_MUSE_RESULT_WORLDWIDE],
        }
        mock_resp = _mock_response(json_data=api_response)

        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            return mock_resp

        with patch.object(self.source, "_get", side_effect=mock_get):
            jobs = await self.source.fetch()

        # 4 categories × at least 1 page each
        assert call_count >= 4
        # Dedup across categories
        assert len(jobs) == 2

    @pytest.mark.asyncio
    async def test_fetch_deduplicates_across_categories(self):
        """Same job appearing in multiple categories should be deduped."""
        api_response = {
            "page": 1,
            "page_count": 1,
            "total": 1,
            "results": [SAMPLE_MUSE_RESULT],
        }
        mock_resp = _mock_response(json_data=api_response)

        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()

        # Same job returned for each of 4 categories, but ID-based dedup → 1
        assert len(jobs) == 1

    @pytest.mark.asyncio
    async def test_fetch_handles_api_error(self):
        mock_resp = _mock_response(status_code=500)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_fetch_handles_network_error(self):
        with patch.object(self.source, "_get", new_callable=AsyncMock, side_effect=Exception("timeout")):
            jobs = await self.source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_fetch_paginates(self):
        """Should follow page_count for pagination."""
        page1 = {
            "page": 1,
            "page_count": 2,
            "total": 2,
            "results": [SAMPLE_MUSE_RESULT],
        }
        page2 = {
            "page": 2,
            "page_count": 2,
            "total": 2,
            "results": [SAMPLE_MUSE_RESULT_WORLDWIDE],
        }
        resp1 = _mock_response(json_data=page1)
        resp2 = _mock_response(json_data=page2)
        # For 4 categories: first category gets 2 pages, rest get page1 (deduped)
        empty = _mock_response(json_data={"page": 1, "page_count": 1, "total": 0, "results": []})

        responses = [resp1, resp2, empty, empty, empty, empty, empty, empty]

        with patch.object(
            self.source, "_get", new_callable=AsyncMock,
            side_effect=responses,
        ):
            jobs = await self.source.fetch()

        assert len(jobs) == 2


# ═══════════════════════════════════════════════════════════════════════════
#  Source Registration — verify all 17 sources in ALL_SOURCES
# ═══════════════════════════════════════════════════════════════════════════

class TestSourceRegistrationV15:
    """Verify new sources are registered in main.py."""

    def test_total_source_count(self):
        import main
        assert len(main.ALL_SOURCES) == 17

    def test_nofluffjobs_registered(self):
        import main
        assert "nofluffjobs" in main.ALL_SOURCES

    def test_himalayas_registered(self):
        import main
        assert "himalayas" in main.ALL_SOURCES

    def test_landingjobs_registered(self):
        import main
        assert "landingjobs" in main.ALL_SOURCES

    def test_themuse_registered(self):
        import main
        assert "themuse" in main.ALL_SOURCES

    def test_all_source_names_unique(self):
        import main
        names = [cls().name for cls in main.ALL_SOURCES.values()]
        assert len(names) == len(set(names))

    def test_all_sources_instantiate(self):
        import main
        for name, cls in main.ALL_SOURCES.items():
            src = cls()
            assert src.name == name

    def test_all_sources_have_fetch(self):
        import main
        for cls in main.ALL_SOURCES.values():
            assert hasattr(cls, "fetch")
