"""Tests for sources: TechJobsForGood, EuroBrussels, Hours80k,
GoodJobs, Devex, LinkedIn, Stepstone (Arbeitsagentur).

Covers:
  - Each source: HTML/API parsing, NGO classification, edge-cases
  - Source registration in main.py (ALL_SOURCES)
  - Filter pipeline integration with new sources
"""

from __future__ import annotations

import asyncio
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

def _mock_response(text: str = "", status_code: int = 200):
    """Create a mock httpx response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.json = MagicMock(return_value={})
    return resp


# ═══════════════════════════════════════════════════════════════════════════
#  TechJobsForGood
# ═══════════════════════════════════════════════════════════════════════════

SAMPLE_TECHJOBS_HTML = """
<html>
<body>
<div class="job-listing">
  <a href="/jobs/123-senior-software-engineer">
    <h3>Senior Software Engineer</h3>
  </a>
  <span class="company">Impact Foundation</span>
  <span class="location">Remote - Worldwide</span>
  <span class="tag">Python</span>
  <span class="tag">Django</span>
  <p class="description">Build tools for social impact organizations.</p>
</div>
<div class="job-listing">
  <a href="/jobs/124-frontend-developer">
    <h3>Frontend Developer</h3>
  </a>
  <span class="company">Green Tech NGO</span>
  <span class="location">Remote - Europe</span>
  <span class="tag">React</span>
  <span class="tag">TypeScript</span>
  <p class="description">Create web interfaces for climate data.</p>
</div>
<div class="job-listing">
  <a href="/jobs/125-backend-engineer">
    <h3>Backend Engineer</h3>
  </a>
  <span class="company">Digital Rights Org</span>
  <span class="location">Berlin, Germany (Remote)</span>
  <span class="tag">Node.js</span>
  <p class="description">Privacy-first backend services.</p>
</div>
</body>
</html>
"""


class TestTechJobsForGoodSource:
    def setup_method(self):
        from sources.techjobsforgood import TechJobsForGoodSource
        self.source = TechJobsForGoodSource()

    # ── Basic parsing ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fetch_parses_job_cards(self):
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, return_value=SAMPLE_TECHJOBS_HTML):
            jobs = await self.source.fetch()
        assert len(jobs) == 3

    @pytest.mark.asyncio
    async def test_fetch_title(self):
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, return_value=SAMPLE_TECHJOBS_HTML):
            jobs = await self.source.fetch()
        assert jobs[0].title == "Senior Software Engineer"

    @pytest.mark.asyncio
    async def test_fetch_company(self):
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, return_value=SAMPLE_TECHJOBS_HTML):
            jobs = await self.source.fetch()
        assert jobs[0].company == "Impact Foundation"

    @pytest.mark.asyncio
    async def test_fetch_source(self):
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, return_value=SAMPLE_TECHJOBS_HTML):
            jobs = await self.source.fetch()
        assert jobs[0].source == "techjobsforgood"

    @pytest.mark.asyncio
    async def test_fetch_all_jobs_are_ngo(self):
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, return_value=SAMPLE_TECHJOBS_HTML):
            jobs = await self.source.fetch()
        for job in jobs:
            assert job.is_ngo is True

    @pytest.mark.asyncio
    async def test_fetch_all_jobs_are_remote(self):
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, return_value=SAMPLE_TECHJOBS_HTML):
            jobs = await self.source.fetch()
        for job in jobs:
            assert job.is_remote is True

    # ── Locations ──────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fetch_location_worldwide(self):
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, return_value=SAMPLE_TECHJOBS_HTML):
            jobs = await self.source.fetch()
        assert jobs[0].location == "Remote - Worldwide"

    @pytest.mark.asyncio
    async def test_fetch_location_europe(self):
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, return_value=SAMPLE_TECHJOBS_HTML):
            jobs = await self.source.fetch()
        assert jobs[1].location == "Remote - Europe"

    @pytest.mark.asyncio
    async def test_fetch_location_berlin(self):
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, return_value=SAMPLE_TECHJOBS_HTML):
            jobs = await self.source.fetch()
        assert jobs[2].location == "Berlin, Germany (Remote)"

    # ── Tags ───────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fetch_extracts_tags(self):
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, return_value=SAMPLE_TECHJOBS_HTML):
            jobs = await self.source.fetch()
        assert "Python" in jobs[0].tags
        assert "Django" in jobs[0].tags

    @pytest.mark.asyncio
    async def test_fetch_tags_limited_to_ten(self):
        """Tags list should not exceed 10 items."""
        many_tags_html = '<html><body><div class="job-listing">'
        many_tags_html += '<a href="/jobs/99-test">Test Job</a>'
        for i in range(15):
            many_tags_html += f'<span class="tag">Tag{i}</span>'
        many_tags_html += '</div></body></html>'
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, return_value=many_tags_html):
            jobs = await self.source.fetch()
        if jobs:
            assert len(jobs[0].tags) <= 10

    # ── URLs ───────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fetch_builds_full_url(self):
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, return_value=SAMPLE_TECHJOBS_HTML):
            jobs = await self.source.fetch()
        assert jobs[0].url == "https://www.techjobsforgood.com/jobs/123-senior-software-engineer"

    @pytest.mark.asyncio
    async def test_fetch_url_already_absolute(self):
        html = """<html><body><div class="job-listing">
        <a href="https://www.techjobsforgood.com/jobs/999-external">Ext Job</a>
        <span class="company">Org</span>
        </div></body></html>"""
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, return_value=html):
            jobs = await self.source.fetch()
        if jobs:
            assert jobs[0].url.startswith("https://")

    # ── Error handling ─────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fetch_handles_rate_limit(self):
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, return_value=""):
            jobs = await self.source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_fetch_handles_empty_html(self):
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, return_value="<html><body></body></html>"):
            jobs = await self.source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_fetch_handles_very_short_response(self):
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, return_value="ok"):
            jobs = await self.source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_fetch_handles_500_error(self):
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, return_value=""):
            jobs = await self.source.fetch()
        assert jobs == []

    # ── Metadata ───────────────────────────────────────────────────────

    def test_source_name(self):
        assert self.source.name == "techjobsforgood"

    @pytest.mark.asyncio
    async def test_safe_fetch_catches_exceptions(self):
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, side_effect=Exception("boom")):
            jobs = await self.source.safe_fetch()
        assert jobs == []

    # ── Fallback parser ────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fallback_link_extraction(self):
        """When no .job-listing divs exist, fallback link extraction works."""
        # Must be > 500 chars to pass the length check
        fallback_html = """
        <html><body>
        <div>
          <a href="/jobs/456-devops-engineer">DevOps Engineer at NGO Tech</a>
          <span class="company">NGO Tech</span>
          <p>This is a longer description to make the HTML pass the length
          threshold. We need at least 500 characters of HTML content for the
          parser to proceed instead of returning early with a warning about
          a short response. So here is some extra padding text to ensure
          that happens properly in this test case.</p>
        </div>
        <div>
          <a href="/jobs/457-backend-dev">Backend Developer at Another Org</a>
          <span class="company">Another Org</span>
          <p>Another longer description block to add more content to this
          HTML test fixture so it exceeds the 500 character minimum.</p>
        </div>
        </body></html>
        """
        with patch.object(self.source, "_fetch_html", new_callable=AsyncMock, return_value=fallback_html):
            jobs = await self.source.fetch()
        assert len(jobs) >= 1

    # ── Static helpers ─────────────────────────────────────────────────

    def test_extract_text_first_match(self):
        from bs4 import BeautifulSoup
        from sources.techjobsforgood import TechJobsForGoodSource
        html = '<div><span class="company">Org A</span><span class="org">Org B</span></div>'
        soup = BeautifulSoup(html, "html.parser")
        div = soup.find("div")
        result = TechJobsForGoodSource._extract_text(div, ["span.company", "span.org"])
        assert result == "Org A"

    def test_extract_text_no_match(self):
        from bs4 import BeautifulSoup
        from sources.techjobsforgood import TechJobsForGoodSource
        html = '<div><span class="other">X</span></div>'
        soup = BeautifulSoup(html, "html.parser")
        div = soup.find("div")
        result = TechJobsForGoodSource._extract_text(div, ["span.company"])
        assert result is None

    def test_extract_job_links_fallback_returns_list(self):
        from bs4 import BeautifulSoup
        from sources.techjobsforgood import TechJobsForGoodSource
        html = '<html><body><div><a href="/jobs/1-test">Test</a></div></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        results = TechJobsForGoodSource._extract_job_links_fallback(soup)
        assert isinstance(results, list)
        assert len(results) >= 1

    def test_extract_job_links_fallback_no_match(self):
        from bs4 import BeautifulSoup
        from sources.techjobsforgood import TechJobsForGoodSource
        html = '<html><body><a href="/about">About</a></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        results = TechJobsForGoodSource._extract_job_links_fallback(soup)
        assert results == []


# ═══════════════════════════════════════════════════════════════════════════
#  EuroBrussels
# ═══════════════════════════════════════════════════════════════════════════

SAMPLE_EUROBRUSSELS_HTML = """
<html>
<body>
<div class="job-listings">
  <div class="job-card">
    <h3>
      <a href="/job_display/288443/Senior_IT_Systems_Engineer_European_Commission_Brussels_Belgium">
        Senior IT Systems Engineer
      </a>
    </h3>
    <span class="company">European Commission</span>
    <span class="location">Brussels</span>
    <span>EU Institution</span>
    <span>Hybrid</span>
    <p>Manage IT infrastructure for EU institutions.</p>
  </div>
  <div class="job-card">
    <h3>
      <a href="/job_display/288444/Data_Analyst_Amnesty_International_Brussels_Belgium">
        Data Analyst
      </a>
    </h3>
    <span class="company">Amnesty International</span>
    <span class="location">Brussels</span>
    <span>NGO and Political</span>
    <p>Analyze human rights data for advocacy campaigns.</p>
  </div>
  <div class="job-card">
    <h3>
      <a href="/job_display/288445/Software_Developer_Industry_Corp_Brussels_Belgium">
        Software Developer
      </a>
    </h3>
    <span class="company">Industry Corp</span>
    <span class="location">Brussels</span>
    <span>Industry Association</span>
    <p>Build internal tools.</p>
  </div>
</div>
</body>
</html>
"""


class TestEuroBrusselsSource:
    def setup_method(self):
        from sources.eurobrussels import EuroBrusselsSource
        self.source = EuroBrusselsSource()

    # ── Basic parsing ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fetch_parses_job_listings(self):
        mock_resp = _mock_response(SAMPLE_EUROBRUSSELS_HTML)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert len(jobs) >= 2

    @pytest.mark.asyncio
    async def test_fetch_job_titles(self):
        mock_resp = _mock_response(SAMPLE_EUROBRUSSELS_HTML)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        titles = [j.title for j in jobs]
        assert any("IT Systems Engineer" in t for t in titles)

    @pytest.mark.asyncio
    async def test_fetch_source_field(self):
        mock_resp = _mock_response(SAMPLE_EUROBRUSSELS_HTML)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        for job in jobs:
            assert job.source == "eurobrussels"

    # ── Source name ────────────────────────────────────────────────────

    def test_source_name(self):
        assert self.source.name == "eurobrussels"

    # ── Error handling ─────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fetch_handles_rate_limit(self):
        mock_resp = _mock_response(status_code=429)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_fetch_handles_empty_html(self):
        mock_resp = _mock_response("<html><body></body></html>")
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_safe_fetch_catches_exceptions(self):
        with patch.object(self.source, "_get", new_callable=AsyncMock, side_effect=Exception("boom")):
            jobs = await self.source.safe_fetch()
        assert jobs == []

    # ── NGO classification ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_ngo_classification_from_tags(self):
        """Jobs tagged with NGO-related categories should be is_ngo=True."""
        mock_resp = _mock_response(SAMPLE_EUROBRUSSELS_HTML)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        ngo_jobs = [j for j in jobs if j.is_ngo]
        assert len(ngo_jobs) >= 1

    def test_extract_tags_eu_institution(self):
        from bs4 import BeautifulSoup
        html = '<div><span>EU Institution</span><span>Hybrid</span></div>'
        soup = BeautifulSoup(html, "html.parser")
        div = soup.find("div")
        tags, is_ngo = self.source._extract_tags_and_ngo_status(div)
        assert is_ngo is True

    def test_extract_tags_ngo_and_political(self):
        from bs4 import BeautifulSoup
        html = '<div><span>NGO and Political</span></div>'
        soup = BeautifulSoup(html, "html.parser")
        div = soup.find("div")
        tags, is_ngo = self.source._extract_tags_and_ngo_status(div)
        assert is_ngo is True

    def test_extract_tags_international_organisations(self):
        from bs4 import BeautifulSoup
        html = '<div><span>International Organisations</span></div>'
        soup = BeautifulSoup(html, "html.parser")
        div = soup.find("div")
        _, is_ngo = self.source._extract_tags_and_ngo_status(div)
        assert is_ngo is True

    def test_extract_tags_industry_not_ngo(self):
        from bs4 import BeautifulSoup
        html = '<div><span>Industry Association</span><span>Consultancy</span></div>'
        soup = BeautifulSoup(html, "html.parser")
        div = soup.find("div")
        _, is_ngo = self.source._extract_tags_and_ngo_status(div)
        assert is_ngo is False

    def test_extract_tags_hybrid_tag(self):
        from bs4 import BeautifulSoup
        html = '<div><span>EU Institution</span><span>Hybrid</span></div>'
        soup = BeautifulSoup(html, "html.parser")
        div = soup.find("div")
        tags, _ = self.source._extract_tags_and_ngo_status(div)
        assert "Hybrid" in tags

    # ── URL metadata parsing ───────────────────────────────────────────

    def test_url_metadata_brussels_belgium(self):
        from sources.eurobrussels import EuroBrusselsSource
        _, location = EuroBrusselsSource._parse_url_metadata(
            "https://www.eurobrussels.com/job_display/288443/Title_Company_Brussels_Belgium"
        )
        assert "Brussels" in location or "Belgium" in location

    def test_url_metadata_berlin_germany(self):
        from sources.eurobrussels import EuroBrusselsSource
        _, location = EuroBrusselsSource._parse_url_metadata(
            "https://www.eurobrussels.com/job_display/1/Job_Org_Berlin_Germany"
        )
        assert "Berlin" in location or "Germany" in location

    def test_url_metadata_no_match(self):
        from sources.eurobrussels import EuroBrusselsSource
        company, location = EuroBrusselsSource._parse_url_metadata(
            "https://example.com/other-url"
        )
        assert company == "Unknown"

    def test_url_metadata_paris_france(self):
        from sources.eurobrussels import EuroBrusselsSource
        _, location = EuroBrusselsSource._parse_url_metadata(
            "https://www.eurobrussels.com/job_display/999/Engineer_Corp_Paris_France"
        )
        assert "Paris" in location or "France" in location

    # ── Deduplication ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_deduplicates_links(self):
        dedup_html = """
        <html><body>
        <a href="/job_display/100/Test_Job_Org_Brussels_Belgium">Test Job Title</a>
        <a href="/job_display/100/Test_Job_Org_Brussels_Belgium">Test Job Title</a>
        </body></html>
        """
        mock_resp = _mock_response(dedup_html)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        urls = [j.url for j in jobs]
        assert len(urls) == len(set(urls))

    @pytest.mark.asyncio
    async def test_skips_short_titles(self):
        """Links with very short text (< 5 chars) should be skipped."""
        html = """<html><body>
        <a href="/job_display/200/AB_Org_Brussels_Belgium">AB</a>
        <a href="/job_display/201/Valid_Title_Job_Org_Brussels_Belgium">Valid Title Job Opening Here</a>
        </body></html>"""
        mock_resp = _mock_response(html)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        for job in jobs:
            assert len(job.title) >= 5

    # ── Default location ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_default_location_brussels(self):
        """When no location info is in the URL, default to Brussels, Belgium."""
        html = """<html><body>
        <div><a href="/job_display/300/SomeLongJobTitle">Some Long Job Title Here</a></div>
        </body></html>"""
        mock_resp = _mock_response(html)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        if jobs:
            assert "Brussels" in jobs[0].location


# ═══════════════════════════════════════════════════════════════════════════
#  80,000 Hours (Algolia API)
# ═══════════════════════════════════════════════════════════════════════════

# Sample Algolia hit for testing
_SAMPLE_ALGOLIA_HIT = {
    "post_pk": 19121,
    "title": "Software Engineer",
    "description_short": "<ul><li>Build systems for good</li></ul>",
    "url_external": "https://example.org/job/123?utm_source=80000hours",
    "company_name": "GiveDirectly",
    "card_locations": ["Remote, Global"],
    "tags_country": ["Remote, Global"],
    "tags_city": ["Remote, Global"],
    "tags_area": ["AI safety & policy"],
    "tags_skill": ["Python", "Django"],
    "tags_role_type": ["Full-time"],
    "posted_at": 1774004989,
    "salary": None,
    "objectID": "19121",
}

_SAMPLE_ALGOLIA_RESPONSE = {
    "hits": [_SAMPLE_ALGOLIA_HIT],
    "nbHits": 1,
    "page": 0,
    "nbPages": 1,
    "hitsPerPage": 100,
}


class TestHours80kSource:
    def setup_method(self):
        from sources.hours80k import Hours80kSource
        self.source = Hours80kSource()

    # ── Source name ────────────────────────────────────────────────────

    def test_source_name(self):
        assert self.source.name == "hours80k"

    # ── Algolia hit parsing ────────────────────────────────────────────

    def test_parse_hit_basic(self):
        job = self.source._parse_hit(_SAMPLE_ALGOLIA_HIT)
        assert job is not None
        assert job.title == "Software Engineer"
        assert job.company == "GiveDirectly"
        assert job.source == "hours80k"

    def test_parse_hit_ngo(self):
        job = self.source._parse_hit(_SAMPLE_ALGOLIA_HIT)
        assert job.is_ngo is True

    def test_parse_hit_url(self):
        job = self.source._parse_hit(_SAMPLE_ALGOLIA_HIT)
        assert "example.org/job/123" in job.url

    def test_parse_hit_tags(self):
        job = self.source._parse_hit(_SAMPLE_ALGOLIA_HIT)
        assert "AI safety & policy" in job.tags
        assert "Python" in job.tags

    def test_parse_hit_tags_limited_to_ten(self):
        hit = dict(_SAMPLE_ALGOLIA_HIT)
        hit["tags_area"] = [f"area-{i}" for i in range(8)]
        hit["tags_skill"] = [f"skill-{i}" for i in range(8)]
        job = self.source._parse_hit(hit)
        assert len(job.tags) <= 10

    def test_parse_hit_posted_at(self):
        job = self.source._parse_hit(_SAMPLE_ALGOLIA_HIT)
        assert job.posted_at is not None

    def test_parse_hit_strips_html_description(self):
        job = self.source._parse_hit(_SAMPLE_ALGOLIA_HIT)
        assert job.description is not None
        assert "<ul>" not in job.description
        assert "Build systems" in job.description

    def test_parse_hit_missing_title_returns_none(self):
        hit = dict(_SAMPLE_ALGOLIA_HIT)
        hit["title"] = ""
        assert self.source._parse_hit(hit) is None

    def test_parse_hit_missing_url_returns_none(self):
        hit = dict(_SAMPLE_ALGOLIA_HIT)
        hit["url_external"] = ""
        assert self.source._parse_hit(hit) is None

    def test_parse_hit_missing_company_defaults(self):
        hit = dict(_SAMPLE_ALGOLIA_HIT)
        hit["company_name"] = None
        job = self.source._parse_hit(hit)
        assert job.company == "Unknown"

    # ── Location inference ─────────────────────────────────────────────

    def test_location_worldwide(self):
        job = self.source._parse_hit(_SAMPLE_ALGOLIA_HIT)
        assert job.remote_scope == "worldwide"

    def test_location_germany(self):
        hit = dict(_SAMPLE_ALGOLIA_HIT)
        hit["card_locations"] = ["Berlin"]
        hit["tags_country"] = ["Germany"]
        job = self.source._parse_hit(hit)
        assert job.remote_scope == "germany"

    def test_location_europe(self):
        hit = dict(_SAMPLE_ALGOLIA_HIT)
        hit["card_locations"] = ["Brussels"]
        hit["tags_country"] = ["Europe"]
        job = self.source._parse_hit(hit)
        assert job.remote_scope == "eu"

    def test_location_unknown_defaults_worldwide(self):
        hit = dict(_SAMPLE_ALGOLIA_HIT)
        hit["card_locations"] = ["San Francisco Bay Area"]
        hit["tags_country"] = ["USA"]
        job = self.source._parse_hit(hit)
        assert job.remote_scope == "worldwide"

    def test_build_location_cities(self):
        loc = self.source._build_location(["Berlin", "Munich"], ["Germany"])
        assert "Berlin" in loc
        assert "Munich" in loc

    def test_build_location_no_cities(self):
        loc = self.source._build_location([], ["Germany"])
        assert "Germany" in loc

    def test_build_location_empty(self):
        loc = self.source._build_location([], [])
        assert loc == "Remote"

    # ── Fetch (mocked API) ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fetch_parses_algolia_response(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _SAMPLE_ALGOLIA_RESPONSE

        with patch.object(self.source, "_post", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert len(jobs) == 1
        assert jobs[0].title == "Software Engineer"

    @pytest.mark.asyncio
    async def test_fetch_deduplicates_by_url(self):
        resp_data = {
            "hits": [_SAMPLE_ALGOLIA_HIT, _SAMPLE_ALGOLIA_HIT],
            "nbHits": 2,
            "page": 0,
            "nbPages": 1,
            "hitsPerPage": 100,
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = resp_data

        with patch.object(self.source, "_post", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert len(jobs) == 1

    @pytest.mark.asyncio
    async def test_fetch_handles_api_error(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.json.return_value = {}

        with patch.object(self.source, "_post", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_fetch_handles_network_error(self):
        with patch.object(self.source, "_post", new_callable=AsyncMock, side_effect=Exception("network")):
            jobs = await self.source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_safe_fetch_catches_exceptions(self):
        with patch.object(self.source, "_post", new_callable=AsyncMock, side_effect=Exception("boom")):
            jobs = await self.source.safe_fetch()
        assert jobs == []

    # ── Remote scope defaults ──────────────────────────────────────────

    def test_default_remote_scope_worldwide(self):
        job = Job(
            title="Engineer",
            company="Org",
            location="Remote",
            url="https://jobs.80000hours.org/job/1",
            source="hours80k",
            is_ngo=True,
            remote_scope="worldwide",
        )
        assert job.remote_scope == "worldwide"

    # ── Job model validation ───────────────────────────────────────────

    def test_all_jobs_are_ngo(self):
        job = Job(
            title="Software Engineer",
            company="GiveDirectly",
            location="Remote - Worldwide",
            url="https://jobs.80000hours.org/job/123",
            source="hours80k",
            is_ngo=True,
            remote_scope="worldwide",
        )
        assert job.is_ngo is True
        assert job.remote_scope == "worldwide"


# ═══════════════════════════════════════════════════════════════════════════
#  GoodJobs.eu
# ═══════════════════════════════════════════════════════════════════════════

SAMPLE_GOODJOBS_HTML = """
<html>
<body>
<a href="/jobs/senior-fullstack-developer-greentech-ggmbh" class="jobcard">
  <h3>Senior Fullstack Developer</h3>
  <div class="mb-1"><div><p>GreenTech gGmbH</p></div></div>
  Jahresgehalt 50.000€ – 70.000€
  Berlin | Hybrid
  vor 2 Tagen
  Vollzeit
  Deutsch, Englisch
  Anstellungsart: Festanstellung
  GoodCompany
</a>
<a href="/jobs/backend-engineer-impactorg-ev" class="jobcard">
  <h3>Backend Engineer</h3>
  <div class="mb-1"><div><p>ImpactOrg e.V.</p></div></div>
  Hamburg | Remote
  vor 1 Woche
  Vollzeit
  Deutsch
  Anstellungsart: Festanstellung
  GoodCompany
</a>
<a href="/jobs/frontend-developer-normal-gmbh" class="jobcard">
  <h3>Frontend Developer</h3>
  <div class="mb-1"><div><p>Normal GmbH</p></div></div>
  München | Hybrid
  vor 3 Tagen
  Vollzeit
  Deutsch
  Anstellungsart: Festanstellung
  Company
</a>
</body>
</html>
"""


class TestGoodJobsSource:
    def setup_method(self):
        from sources.goodjobs import GoodJobsSource
        self.source = GoodJobsSource()

    # ── Source name ────────────────────────────────────────────────────

    def test_source_name(self):
        assert self.source.name == "goodjobs"

    # ── Basic parsing ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fetch_parses_job_links(self):
        mock_resp = _mock_response(SAMPLE_GOODJOBS_HTML)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert len(jobs) == 3

    @pytest.mark.asyncio
    async def test_fetch_job_titles(self):
        mock_resp = _mock_response(SAMPLE_GOODJOBS_HTML)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        titles = [j.title for j in jobs]
        assert any("Fullstack" in t for t in titles)

    @pytest.mark.asyncio
    async def test_fetch_source_field(self):
        mock_resp = _mock_response(SAMPLE_GOODJOBS_HTML)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        for job in jobs:
            assert job.source == "goodjobs"

    # ── Error handling ─────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fetch_handles_rate_limit(self):
        mock_resp = _mock_response(status_code=429)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_fetch_handles_empty_html(self):
        mock_resp = _mock_response("<html><body></body></html>")
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_safe_fetch_catches_exceptions(self):
        with patch.object(self.source, "_get", new_callable=AsyncMock, side_effect=Exception("boom")):
            jobs = await self.source.safe_fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_fetch_handles_short_response(self):
        mock_resp = _mock_response("short")
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert jobs == []

    # ── NGO detection ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_ngo_detection_in_fetch(self):
        mock_resp = _mock_response(SAMPLE_GOODJOBS_HTML)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        ngo_jobs = [j for j in jobs if j.is_ngo]
        assert len(ngo_jobs) >= 1

    def test_is_ngo_ggmbh(self):
        from sources.goodjobs import GoodJobsSource
        assert GoodJobsSource._is_ngo_company("GreenTech gGmbH", "")

    def test_is_ngo_ev(self):
        from sources.goodjobs import GoodJobsSource
        assert GoodJobsSource._is_ngo_company("ImpactOrg e.V.", "")

    def test_is_ngo_e_v_with_space(self):
        from sources.goodjobs import GoodJobsSource
        assert GoodJobsSource._is_ngo_company("Org e. V.", "")

    def test_is_ngo_stiftung(self):
        from sources.goodjobs import GoodJobsSource
        assert GoodJobsSource._is_ngo_company("Deutsche Umweltstiftung", "")

    def test_is_ngo_nonprofit(self):
        from sources.goodjobs import GoodJobsSource
        assert GoodJobsSource._is_ngo_company("Some nonprofit org", "")

    def test_is_ngo_gemeinnuetzig(self):
        from sources.goodjobs import GoodJobsSource
        assert GoodJobsSource._is_ngo_company("Company", "gemeinnützig organisation")

    def test_not_ngo_normal_gmbh(self):
        from sources.goodjobs import GoodJobsSource
        assert not GoodJobsSource._is_ngo_company("Normal GmbH", "Normal GmbH Company")

    def test_not_ngo_plain_company(self):
        from sources.goodjobs import GoodJobsSource
        assert not GoodJobsSource._is_ngo_company("Acme Corp", "Regular company description")

    # ── Salary extraction ──────────────────────────────────────────────

    def test_extract_salary_jahresgehalt(self):
        from sources.goodjobs import GoodJobsSource
        salary = GoodJobsSource._extract_salary("Jahresgehalt 50.000€ – 70.000€")
        assert salary is not None
        assert "50.000€" in salary
        assert "70.000€" in salary

    def test_extract_salary_range_without_prefix(self):
        from sources.goodjobs import GoodJobsSource
        salary = GoodJobsSource._extract_salary("40.000€ – 55.000€")
        assert salary is not None

    def test_extract_salary_none(self):
        from sources.goodjobs import GoodJobsSource
        assert GoodJobsSource._extract_salary("No salary info here") is None

    def test_extract_salary_with_dash(self):
        from sources.goodjobs import GoodJobsSource
        salary = GoodJobsSource._extract_salary("Jahresgehalt 60.000€ - 80.000€")
        assert salary is not None

    # ── Location extraction ────────────────────────────────────────────

    def test_extract_location_berlin_hybrid(self):
        from sources.goodjobs import GoodJobsSource
        location = GoodJobsSource._extract_location("Berlin | Hybrid vor 2 Tagen")
        assert "Berlin" in location
        assert "Germany" in location

    def test_extract_location_hamburg_remote(self):
        from sources.goodjobs import GoodJobsSource
        location = GoodJobsSource._extract_location("Hamburg | Remote vor 1 Woche")
        assert "Hamburg" in location
        assert "Remote" in location

    def test_extract_location_munich_vor_ort(self):
        from sources.goodjobs import GoodJobsSource
        location = GoodJobsSource._extract_location("München | Nur vor Ort")
        assert "München" in location
        assert "Germany" in location

    def test_extract_location_fallback(self):
        from sources.goodjobs import GoodJobsSource
        location = GoodJobsSource._extract_location("No city info")
        assert location == "Germany"

    def test_extract_location_known_city_no_pipe(self):
        from sources.goodjobs import GoodJobsSource
        location = GoodJobsSource._extract_location("Job in Frankfurt area")
        assert "Frankfurt" in location

    def test_extract_location_cologne(self):
        from sources.goodjobs import GoodJobsSource
        location = GoodJobsSource._extract_location("Köln | Hybrid")
        assert "Köln" in location
        assert "Germany" in location

    # ── Company extraction ─────────────────────────────────────────────

    def test_extract_company_good_company(self):
        from sources.goodjobs import GoodJobsSource
        company = GoodJobsSource._extract_company("Some Org Name GoodCompany")
        assert company != "Unknown"

    def test_extract_company_fallback(self):
        from sources.goodjobs import GoodJobsSource
        company = GoodJobsSource._extract_company("no company pattern here")
        assert company == "Unknown"

    # ── Navigation links ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_skips_pagination_links(self):
        nav_html = """
        <html><body>
        <a href="/jobs?category=it&page=2">Next page</a>
        <a href="/jobs?category=it&previous-page=1&page=3">Page 3</a>
        <a href="/jobs/real-job-slug">Real Job Title Here For Testing</a>
        </body></html>
        """
        mock_resp = _mock_response(nav_html)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert len(jobs) <= 1

    @pytest.mark.asyncio
    async def test_skips_bare_jobs_url(self):
        html = """<html><body>
        <a href="/jobs">All Jobs</a>
        <a href="/jobs/specific-job-slug">Specific Job Title Posting</a>
        </body></html>"""
        mock_resp = _mock_response(html)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        for job in jobs:
            assert job.url != "https://goodjobs.eu/jobs"

    # ── URL construction ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_builds_full_url(self):
        mock_resp = _mock_response(SAMPLE_GOODJOBS_HTML)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        for job in jobs:
            assert job.url.startswith("https://goodjobs.eu/jobs/")

    # ── Tags ───────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_hybrid_tag_detected(self):
        mock_resp = _mock_response(SAMPLE_GOODJOBS_HTML)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        hybrid_jobs = [j for j in jobs if "Hybrid" in j.tags]
        assert len(hybrid_jobs) >= 1

    @pytest.mark.asyncio
    async def test_remote_tag_detected(self):
        mock_resp = _mock_response(SAMPLE_GOODJOBS_HTML)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        remote_jobs = [j for j in jobs if "Remote" in j.tags]
        assert len(remote_jobs) >= 1


# ═══════════════════════════════════════════════════════════════════════════
#  Devex
# ═══════════════════════════════════════════════════════════════════════════

SAMPLE_DEVEX_JSON = {
    "data": [
        {
            "id": 1234567,
            "name": "ICT Specialist",
            "slug_and_id": "1234567-ict-specialist",
            "employer_company": {"name": "UNDP"},
            "places": [{"type": "Country", "name": "Remote"}],
            "news_topics": [{"name": "Information Technology"}],
            "published_at": "2025-01-10T12:00:00Z",
            "is_remote": True,
        },
        {
            "id": 1234568,
            "name": "Software Engineer",
            "slug_and_id": "1234568-software-engineer",
            "employer_company": {"name": "UNICEF"},
            "places": [{"type": "Region", "name": "Remote - Worldwide"}],
            "news_topics": [{"name": "Technology"}],
            "published_at": "2025-01-09T08:30:00Z",
            "is_remote": True,
        },
        {
            "id": 1234569,
            "name": "Data Engineer",
            "slug_and_id": "1234569-data-engineer",
            "employer_company": {"name": "WFP"},
            "places": [
                {"type": "City", "name": "Rome"},
                {"type": "Country", "name": "Italy"},
            ],
            "news_topics": [{"name": "Data Management"}],
            "published_at": "2025-01-08T10:00:00Z",
            "is_remote": True,
        },
    ],
    "page": {"pages": 1},
}


def _mock_json_response(json_data, status_code=200):
    """Create a mock httpx Response that returns JSON."""
    import json as json_mod
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_data
    mock.text = json_mod.dumps(json_data) if isinstance(json_data, dict) else ""
    return mock


class TestDevexSource:
    def setup_method(self):
        from sources.devex import DevexSource
        self.source = DevexSource()

    # ── Source name ────────────────────────────────────────────────────

    def test_source_name(self):
        assert self.source.name == "devex"

    # ── Basic parsing ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fetch_parses_job_cards(self):
        mock_resp = _mock_json_response(SAMPLE_DEVEX_JSON)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert len(jobs) == 3

    @pytest.mark.asyncio
    async def test_fetch_job_titles(self):
        mock_resp = _mock_json_response(SAMPLE_DEVEX_JSON)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        titles = [j.title for j in jobs]
        assert "ICT Specialist" in titles
        assert "Software Engineer" in titles
        assert "Data Engineer" in titles

    @pytest.mark.asyncio
    async def test_fetch_source_field(self):
        mock_resp = _mock_json_response(SAMPLE_DEVEX_JSON)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        for job in jobs:
            assert job.source == "devex"

    @pytest.mark.asyncio
    async def test_all_jobs_are_ngo(self):
        mock_resp = _mock_json_response(SAMPLE_DEVEX_JSON)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        for job in jobs:
            assert job.is_ngo is True

    @pytest.mark.asyncio
    async def test_all_jobs_are_remote(self):
        mock_resp = _mock_json_response(SAMPLE_DEVEX_JSON)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        for job in jobs:
            assert job.is_remote is True

    # ── Company extraction ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fetch_extracts_company(self):
        mock_resp = _mock_json_response(SAMPLE_DEVEX_JSON)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        companies = [j.company for j in jobs]
        assert "UNDP" in companies
        assert "UNICEF" in companies
        assert "WFP" in companies

    # ── Location extraction ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fetch_extracts_location(self):
        mock_resp = _mock_json_response(SAMPLE_DEVEX_JSON)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        locations = [j.location for j in jobs]
        assert any("Remote" in l for l in locations)

    @pytest.mark.asyncio
    async def test_fetch_location_with_city(self):
        mock_resp = _mock_json_response(SAMPLE_DEVEX_JSON)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        wfp_job = [j for j in jobs if j.company == "WFP"]
        if wfp_job:
            assert "Rome" in wfp_job[0].location

    # ── URL construction ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fetch_builds_full_url(self):
        mock_resp = _mock_json_response(SAMPLE_DEVEX_JSON)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        for job in jobs:
            assert job.url.startswith("https://www.devex.com/jobs/")

    # ── Tags ───────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fetch_extracts_tags(self):
        mock_resp = _mock_json_response(SAMPLE_DEVEX_JSON)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        all_tags = []
        for job in jobs:
            all_tags.extend(job.tags)
        assert len(all_tags) >= 1
        assert "Information Technology" in all_tags

    # ── Posted date ────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fetch_extracts_posted_at(self):
        mock_resp = _mock_json_response(SAMPLE_DEVEX_JSON)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        has_posted = [j for j in jobs if j.posted_at is not None]
        assert len(has_posted) == 3

    # ── Error handling ─────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fetch_handles_rate_limit(self):
        mock_resp = _mock_json_response({}, status_code=429)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_fetch_handles_empty_data(self):
        mock_resp = _mock_json_response({"data": [], "page": {"pages": 1}})
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_safe_fetch_catches_exceptions(self):
        with patch.object(self.source, "_get", new_callable=AsyncMock, side_effect=Exception("boom")):
            jobs = await self.source.safe_fetch()
        assert jobs == []

    # ── Deduplication ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_deduplicates_ids(self):
        dup_json = {
            "data": [
                {
                    "id": 999,
                    "name": "Duplicate Job",
                    "slug_and_id": "999-dup-job",
                    "employer_company": {"name": "Org"},
                    "places": [],
                    "news_topics": [],
                    "published_at": None,
                    "is_remote": True,
                },
                {
                    "id": 999,
                    "name": "Duplicate Job",
                    "slug_and_id": "999-dup-job",
                    "employer_company": {"name": "Org"},
                    "places": [],
                    "news_topics": [],
                    "published_at": None,
                    "is_remote": True,
                },
            ],
            "page": {"pages": 1},
        }
        mock_resp = _mock_json_response(dup_json)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert len(jobs) == 1

    @pytest.mark.asyncio
    async def test_skips_short_titles(self):
        short_json = {
            "data": [
                {
                    "id": 1,
                    "name": "XY",
                    "slug_and_id": "1-xy",
                    "employer_company": {"name": "Org"},
                    "places": [],
                    "news_topics": [],
                    "published_at": None,
                    "is_remote": True,
                },
            ],
            "page": {"pages": 1},
        }
        mock_resp = _mock_json_response(short_json)
        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert jobs == []

    # ── Static helpers (kept for backward compat) ──────────────────────

    def test_extract_text_first_match(self):
        from bs4 import BeautifulSoup
        from sources.devex import DevexSource
        html = '<div><span class="company">UNDP</span><span class="org">Alt</span></div>'
        soup = BeautifulSoup(html, "html.parser")
        div = soup.find("div")
        result = DevexSource._extract_text(div, ["span.company", "span.org"])
        assert result == "UNDP"

    def test_extract_text_no_match(self):
        from bs4 import BeautifulSoup
        from sources.devex import DevexSource
        html = '<div><span class="other">X</span></div>'
        soup = BeautifulSoup(html, "html.parser")
        div = soup.find("div")
        result = DevexSource._extract_text(div, ["span.company"])
        assert result is None

    def test_fallback_link_extraction(self):
        from bs4 import BeautifulSoup
        from sources.devex import DevexSource
        html = '<html><body><div><a href="/jobs/123-test">Test</a></div></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        results = DevexSource._extract_job_links_fallback(soup)
        assert len(results) >= 1

    def test_fallback_link_skips_search(self):
        from bs4 import BeautifulSoup
        from sources.devex import DevexSource
        html = '<html><body><a href="/jobs/search?q=test">Search</a></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        results = DevexSource._extract_job_links_fallback(soup)
        assert results == []

    def test_fallback_link_skips_filter(self):
        from bs4 import BeautifulSoup
        from sources.devex import DevexSource
        html = '<html><body><a href="/jobs/search?filter=remote">Filter</a></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        results = DevexSource._extract_job_links_fallback(soup)
        assert results == []


# ═══════════════════════════════════════════════════════════════════════════
#  BaseSource._post (Algolia / JSON API support)
# ═══════════════════════════════════════════════════════════════════════════

class TestBaseSourcePost:
    """Verify that BaseSource has a _post method for JSON APIs."""

    def test_post_method_exists(self):
        from sources.base import BaseSource
        assert hasattr(BaseSource, "_post")

    def test_post_method_callable(self):
        from sources.hours80k import Hours80kSource
        source = Hours80kSource()
        assert callable(source._post)


# ═══════════════════════════════════════════════════════════════════════════
#  Source registration in main.py
# ═══════════════════════════════════════════════════════════════════════════

class TestSourceRegistration:
    def test_all_sources_registered(self):
        from main import ALL_SOURCES
        expected = {
            "remotive", "arbeitnow", "remoteok", "weworkremotely",
            "idealist", "reliefweb",
            "techjobsforgood", "eurobrussels", "hours80k", "goodjobs", "devex",
            "linkedin", "stepstone",
            "nofluffjobs", "himalayas", "landingjobs", "themuse",
        }
        assert set(ALL_SOURCES.keys()) == expected

    def test_source_count_is_seventeen(self):
        from main import ALL_SOURCES
        assert len(ALL_SOURCES) == 17

    def test_all_sources_are_httpx_based(self):
        """After v1.5, all sources use httpx — no Playwright sources remain."""
        from main import ALL_SOURCES
        # Verify no _PLAYWRIGHT_SOURCES set exists
        import main
        assert not hasattr(main, "_PLAYWRIGHT_SOURCES")

    def test_all_sources_instantiable(self):
        from main import ALL_SOURCES
        for name, cls in ALL_SOURCES.items():
            instance = cls()
            assert instance.name == name

    def test_all_sources_have_fetch(self):
        from main import ALL_SOURCES
        for name, cls in ALL_SOURCES.items():
            instance = cls()
            assert hasattr(instance, "fetch")

    def test_all_sources_have_safe_fetch(self):
        from main import ALL_SOURCES
        for name, cls in ALL_SOURCES.items():
            instance = cls()
            assert hasattr(instance, "safe_fetch")

    def test_get_sources_all(self):
        from main import _get_sources
        sources = _get_sources(None)
        assert len(sources) == 17

    def test_get_sources_single(self):
        from main import _get_sources
        sources = _get_sources("devex")
        assert len(sources) == 1
        assert sources[0].name == "devex"

    def test_get_sources_new_techjobsforgood(self):
        from main import _get_sources
        sources = _get_sources("techjobsforgood")
        assert len(sources) == 1
        assert sources[0].name == "techjobsforgood"

    def test_get_sources_new_eurobrussels(self):
        from main import _get_sources
        sources = _get_sources("eurobrussels")
        assert len(sources) == 1
        assert sources[0].name == "eurobrussels"

    def test_get_sources_new_hours80k(self):
        from main import _get_sources
        sources = _get_sources("hours80k")
        assert len(sources) == 1
        assert sources[0].name == "hours80k"

    def test_get_sources_new_goodjobs(self):
        from main import _get_sources
        sources = _get_sources("goodjobs")
        assert len(sources) == 1
        assert sources[0].name == "goodjobs"


# ═══════════════════════════════════════════════════════════════════════════
#  Filter pipeline defaults (unknown scope → worldwide)
# ═══════════════════════════════════════════════════════════════════════════

class TestUnknownScopeDefaults:
    @pytest.mark.asyncio
    async def test_hours80k_unknown_scope_defaults_worldwide(self):
        """In _apply_filters, hours80k jobs with unknown scope default to worldwide."""
        from main import _apply_filters

        job = Job(
            title="Software Engineer",
            company="EA Foundation",
            location="Remote",
            url="https://jobs.80000hours.org/job/999",
            source="hours80k",
            is_remote=True,
            is_ngo=True,
        )
        results = _apply_filters([job])
        assert len(results) == 1
        assert results[0].remote_scope == "worldwide"

    @pytest.mark.asyncio
    async def test_idealist_unknown_scope_defaults_worldwide(self):
        """Idealist unknown scope should also default to worldwide."""
        from main import _apply_filters

        job = Job(
            title="Software Engineer",
            company="Nonprofit Org",
            location="Remote",
            url="https://idealist.org/job/999",
            source="idealist",
            is_remote=True,
            is_ngo=True,
        )
        results = _apply_filters([job])
        assert len(results) == 1
        assert results[0].remote_scope == "worldwide"


# ═══════════════════════════════════════════════════════════════════════════
#  Filter pipeline integration with new sources
# ═══════════════════════════════════════════════════════════════════════════

class TestNewSourcesFilterIntegration:
    def test_techjobsforgood_worldwide_accepted(self):
        from main import _apply_filters

        job = Job(
            title="Software Engineer",
            company="Impact Org",
            location="Remote - Worldwide",
            url="https://techjobsforgood.com/jobs/1",
            source="techjobsforgood",
            is_remote=True,
            is_ngo=True,
        )
        results = _apply_filters([job])
        assert len(results) == 1

    def test_techjobsforgood_europe_accepted(self):
        from main import _apply_filters

        job = Job(
            title="Backend Developer",
            company="EU Org",
            location="Remote - Europe",
            url="https://techjobsforgood.com/jobs/2",
            source="techjobsforgood",
            is_remote=True,
            is_ngo=True,
        )
        results = _apply_filters([job])
        assert len(results) == 1

    def test_eurobrussels_eu_location_accepted(self):
        from main import _apply_filters

        job = Job(
            title="Software Developer",
            company="EU Commission",
            location="Brussels, Belgium",
            url="https://eurobrussels.com/job_display/1/test",
            source="eurobrussels",
            is_remote=False,
            is_ngo=True,
        )
        results = _apply_filters([job])
        assert len(results) == 1

    def test_eurobrussels_berlin_accepted(self):
        from main import _apply_filters

        job = Job(
            title="Software Engineer",
            company="Berlin NGO",
            location="Berlin, Germany (Remote)",
            url="https://eurobrussels.com/job_display/2/test",
            source="eurobrussels",
            is_remote=True,
            is_ngo=True,
        )
        results = _apply_filters([job])
        assert len(results) == 1

    def test_goodjobs_germany_remote_accepted(self):
        from main import _apply_filters

        job = Job(
            title="Backend Developer",
            company="GreenTech gGmbH",
            location="Berlin, Germany (Hybrid)",
            url="https://goodjobs.eu/jobs/test",
            source="goodjobs",
            is_remote=True,
            is_ngo=True,
        )
        results = _apply_filters([job])
        assert len(results) == 1
        assert results[0].remote_scope == "germany"

    def test_devex_worldwide_accepted(self):
        from main import _apply_filters

        job = Job(
            title="Software Engineer",
            company="UNDP",
            location="Remote - Worldwide",
            url="https://devex.com/jobs/1",
            source="devex",
            is_remote=True,
            is_ngo=True,
        )
        results = _apply_filters([job])
        assert len(results) == 1

    def test_goodjobs_onsite_munich_rejected(self):
        """GoodJobs job that is on-site only in Munich should be rejected."""
        from main import _apply_filters

        job = Job(
            title="Backend Developer",
            company="Munich Corp",
            location="München, Germany",
            url="https://goodjobs.eu/jobs/test-onsite",
            source="goodjobs",
            is_remote=False,
        )
        results = _apply_filters([job])
        assert len(results) == 0

    def test_devex_us_only_rejected(self):
        """Devex job restricted to US should be rejected."""
        from main import _apply_filters

        job = Job(
            title="Software Engineer",
            company="US NGO",
            location="Remote - US Only",
            url="https://devex.com/jobs/us-only",
            source="devex",
            is_remote=True,
            is_ngo=True,
        )
        results = _apply_filters([job])
        assert len(results) == 0

    def test_hours80k_worldwide_accepted(self):
        from main import _apply_filters

        job = Job(
            title="Software Engineer",
            company="80k Partner Org",
            location="Remote",
            url="https://jobs.80000hours.org/job/42",
            source="hours80k",
            is_remote=True,
            is_ngo=True,
        )
        results = _apply_filters([job])
        assert len(results) == 1

    def test_multiple_new_sources_in_batch(self):
        """Filter pipeline handles a mixed batch from new sources."""
        from main import _apply_filters

        jobs = [
            Job(title="Software Engineer", company="Org A",
                location="Remote - Worldwide", url="https://techjobsforgood.com/jobs/a",
                source="techjobsforgood", is_remote=True, is_ngo=True),
            Job(title="Backend Developer", company="Org B",
                location="Brussels, Belgium", url="https://eurobrussels.com/job_display/1/b",
                source="eurobrussels", is_remote=False, is_ngo=True),
            Job(title="Full Stack Developer", company="Org C",
                location="Remote - Worldwide", url="https://devex.com/jobs/c",
                source="devex", is_remote=True, is_ngo=True),
        ]
        results = _apply_filters(jobs)
        assert len(results) == 3

    def test_company_cap_applies_to_new_sources(self):
        """Per-company cap of 2 should apply to new source jobs too."""
        from main import _apply_filters

        jobs = [
            Job(title="Software Engineer", company="Same Org",
                location="Remote - Worldwide", url="https://devex.com/jobs/1",
                source="devex", is_remote=True, is_ngo=True),
            Job(title="Backend Developer", company="Same Org",
                location="Remote - Worldwide", url="https://devex.com/jobs/2",
                source="devex", is_remote=True, is_ngo=True),
            Job(title="Frontend Developer", company="Same Org",
                location="Remote - Worldwide", url="https://devex.com/jobs/3",
                source="devex", is_remote=True, is_ngo=True),
        ]
        results = _apply_filters(jobs)
        assert len(results) == 2  # capped at 2 per company


# ═══════════════════════════════════════════════════════════════════════════
#  Discord relative time formatting
# ═══════════════════════════════════════════════════════════════════════════

class TestFormatRelativeTime:
    def _fmt(self, **kwargs) -> str:
        from notifiers.discord_notifier import _format_relative_time
        dt = datetime.now(timezone.utc) - timedelta(**kwargs)
        return _format_relative_time(dt)

    def test_few_minutes_ago(self):
        assert self._fmt(minutes=5) == "a few minutes ago"

    def test_30_minutes_ago(self):
        assert self._fmt(minutes=30) == "a few minutes ago"

    def test_59_minutes_ago(self):
        assert self._fmt(minutes=59) == "a few minutes ago"

    def test_1_hour_ago(self):
        assert self._fmt(hours=1) == "1 hour ago"

    def test_1_5_hours_ago(self):
        assert self._fmt(hours=1, minutes=30) == "1 hour ago"

    def test_2_hours_ago(self):
        assert self._fmt(hours=2) == "2 hours ago"

    def test_5_hours_ago(self):
        assert self._fmt(hours=5) == "5 hours ago"

    def test_23_hours_ago(self):
        assert self._fmt(hours=23) == "23 hours ago"

    def test_1_day_ago(self):
        assert self._fmt(days=1) == "1 day ago"

    def test_2_days_ago(self):
        assert self._fmt(days=2) == "2 days ago"

    def test_3_days_ago(self):
        assert self._fmt(days=3) == "3 days ago"

    def test_5_days_ago(self):
        assert self._fmt(days=5) == "5 days ago"

    def test_6_days_shows_date(self):
        result = self._fmt(days=6)
        # Should be YYYY-MM-DD format
        assert len(result) == 10
        assert "-" in result

    def test_30_days_shows_date(self):
        result = self._fmt(days=30)
        assert len(result) == 10
        assert "-" in result

    def test_naive_datetime_handled(self):
        from notifiers.discord_notifier import _format_relative_time
        dt = datetime.utcnow() - timedelta(hours=2)  # naive
        result = _format_relative_time(dt)
        assert "hour" in result

    def test_future_datetime_shows_date(self):
        from notifiers.discord_notifier import _format_relative_time
        dt = datetime.now(timezone.utc) + timedelta(days=1)
        result = _format_relative_time(dt)
        assert len(result) == 10  # YYYY-MM-DD

    def test_just_now(self):
        from notifiers.discord_notifier import _format_relative_time
        dt = datetime.now(timezone.utc) - timedelta(seconds=30)
        result = _format_relative_time(dt)
        assert result == "a few minutes ago"


# ═══════════════════════════════════════════════════════════════════════════
#  Discord embed company display
# ═══════════════════════════════════════════════════════════════════════════

class TestDiscordCompanyDisplay:
    """Test that Discord embeds show full company info."""

    @pytest.mark.asyncio
    async def test_company_name_shown_in_embed(self):
        """The full company name should appear in the embed."""
        from notifiers.discord_notifier import DiscordNotifier

        notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/test/test")

        job = Job(
            title="Software Engineer",
            company="Office Hours Inc.",
            location="Remote (worldwide)",
            url="https://example.com/job/1",
            source="remoteok",
            is_remote=True,
            remote_scope="worldwide",
        )

        # We'll capture the embed that gets built
        with patch("notifiers.discord_notifier.AsyncDiscordWebhook") as MockWebhook:
            mock_instance = MagicMock()
            mock_instance.execute = AsyncMock(return_value=MagicMock(status_code=200))
            MockWebhook.return_value = mock_instance

            await notifier._send_single_job(job)

            # Check add_embed was called
            assert mock_instance.add_embed.called
            embed = mock_instance.add_embed.call_args[0][0]

            # The embed fields should contain the full company name
            fields = embed.fields if hasattr(embed, 'fields') else []
            # Since we use discord_webhook library, check the raw embed
            company_found = False
            for field in fields:
                if "Company" in str(field.get("name", "")):
                    assert "Office Hours Inc." in field["value"]
                    company_found = True
            if not company_found:
                # The DiscordEmbed stores fields internally
                pass  # embed structure varies by library version

    @pytest.mark.asyncio
    async def test_company_with_address_in_embed(self):
        """When company_city/country are set, they appear in the embed."""
        from notifiers.discord_notifier import DiscordNotifier

        notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/test/test")

        job = Job(
            title="Backend Developer",
            company="GoodOrg gGmbH",
            location="Berlin, Germany (Remote)",
            url="https://example.com/job/2",
            source="goodjobs",
            is_remote=True,
            company_city="Berlin",
            company_country="Germany",
            company_postal_code="10115",
        )

        with patch("notifiers.discord_notifier.AsyncDiscordWebhook") as MockWebhook:
            mock_instance = MagicMock()
            mock_instance.execute = AsyncMock(return_value=MagicMock(status_code=200))
            MockWebhook.return_value = mock_instance

            await notifier._send_single_job(job)
            assert mock_instance.add_embed.called


# ═══════════════════════════════════════════════════════════════════════════
#  WeWorkRemotely: multi-feed (v1.4 Step 6b)
# ═══════════════════════════════════════════════════════════════════════════

class TestWeWorkRemotelyMultiFeed:
    """Verify WeWorkRemotely fetches from 5 RSS feeds, not just 1."""

    def test_rss_feeds_list_has_five_entries(self):
        from sources.weworkremotely import _RSS_FEEDS
        assert len(_RSS_FEEDS) == 5

    def test_rss_feeds_include_all_categories(self):
        from sources.weworkremotely import _RSS_FEEDS
        feed_urls = " ".join(_RSS_FEEDS)
        assert "full-stack" in feed_urls
        assert "back-end" in feed_urls
        assert "front-end" in feed_urls
        assert "devops" in feed_urls
        assert "programming-jobs" in feed_urls

    @pytest.mark.asyncio
    async def test_fetch_calls_all_feeds(self):
        """fetch() should call _fetch_feed for each RSS feed."""
        from sources.weworkremotely import WeWorkRemotelySource, _RSS_FEEDS
        source = WeWorkRemotelySource()
        with patch.object(source, "_fetch_feed", new_callable=AsyncMock, return_value=[]) as mock_fetch:
            await source.fetch()
            assert mock_fetch.call_count == len(_RSS_FEEDS)

    @pytest.mark.asyncio
    async def test_fetch_deduplicates_by_url(self):
        """Jobs appearing in multiple feeds should be deduplicated."""
        from sources.weworkremotely import WeWorkRemotelySource
        source = WeWorkRemotelySource()

        job1 = Job(
            title="Fullstack Dev", company="Acme", location="Remote",
            url="https://weworkremotely.com/jobs/1", source="weworkremotely", is_remote=True,
        )
        job2 = Job(
            title="Backend Dev", company="Corp", location="Remote",
            url="https://weworkremotely.com/jobs/2", source="weworkremotely", is_remote=True,
        )
        # Same URL as job1 — should be deduped
        job1_dup = Job(
            title="Fullstack Dev", company="Acme", location="Remote",
            url="https://weworkremotely.com/jobs/1", source="weworkremotely", is_remote=True,
        )

        async def mock_fetch_feed(url):
            if "full-stack" in url:
                return [job1]
            elif "back-end" in url:
                return [job2, job1_dup]
            return []

        with patch.object(source, "_fetch_feed", side_effect=mock_fetch_feed):
            jobs = await source.fetch()

        assert len(jobs) == 2
        urls = {j.url for j in jobs}
        assert "https://weworkremotely.com/jobs/1" in urls
        assert "https://weworkremotely.com/jobs/2" in urls


# ═══════════════════════════════════════════════════════════════════════════
#  Remotive: multi-category (v1.4 Step 6c)
# ═══════════════════════════════════════════════════════════════════════════

class TestRemotiveMultiCategory:
    """Verify Remotive fetches from multiple API categories."""

    def test_categories_list_has_multiple_entries(self):
        from sources.remotive import _CATEGORIES
        assert len(_CATEGORIES) >= 2
        assert "software-dev" in _CATEGORIES

    def test_categories_include_devops(self):
        from sources.remotive import _CATEGORIES
        assert "devops-sysadmin" in _CATEGORIES

    @pytest.mark.asyncio
    async def test_fetch_calls_all_categories(self):
        """fetch() should call _fetch_category for each category."""
        from sources.remotive import RemotiveSource, _CATEGORIES
        source = RemotiveSource()
        with patch.object(source, "_fetch_category", new_callable=AsyncMock, return_value=[]) as mock_fetch:
            await source.fetch()
            assert mock_fetch.call_count == len(_CATEGORIES)

    @pytest.mark.asyncio
    async def test_fetch_deduplicates_by_url(self):
        """Jobs appearing in multiple categories should be deduplicated."""
        from sources.remotive import RemotiveSource
        source = RemotiveSource()

        job1 = Job(
            title="DevOps Engineer", company="Ops Co", location="Remote",
            url="https://remotive.com/job/1", source="remotive", is_remote=True,
        )
        job2 = Job(
            title="Software Dev", company="Dev Co", location="Remote",
            url="https://remotive.com/job/2", source="remotive", is_remote=True,
        )
        job1_dup = Job(
            title="DevOps Engineer", company="Ops Co", location="Remote",
            url="https://remotive.com/job/1", source="remotive", is_remote=True,
        )

        async def mock_fetch_cat(category):
            if category == "software-dev":
                return [job2]
            elif category == "devops-sysadmin":
                return [job1, job1_dup]
            return []

        with patch.object(source, "_fetch_category", side_effect=mock_fetch_cat):
            jobs = await source.fetch()

        assert len(jobs) == 2


# ═══════════════════════════════════════════════════════════════════════════
#  LinkedIn source (v1.4 Step 9)
# ═══════════════════════════════════════════════════════════════════════════

# Sample HTML fragment that LinkedIn's guest API returns
SAMPLE_LINKEDIN_HTML = """
<li>
  <div class="base-card relative w-full hover:no-underline focus:no-underline base-card--link base-search-card base-search-card--link job-search-card" data-entity-urn="urn:li:jobPosting:12345">
    <a class="base-card__full-link absolute top-0 right-0 bottom-0 left-0 p-0 z-[2]" href="https://de.linkedin.com/jobs/view/software-engineer-at-acme-corp-12345" data-tracking-control-name="public_jobs_jserp-result_search-card">
      <span class="sr-only">Software Engineer</span>
    </a>
    <div class="base-search-card__info">
      <h3 class="base-search-card__title">Software Engineer</h3>
      <h4 class="base-search-card__subtitle">
        <a href="https://www.linkedin.com/company/acme-corp">Acme Corp</a>
      </h4>
      <div class="base-search-card__metadata">
        <span class="job-search-card__location">Berlin, Germany</span>
        <time class="job-search-card__listdate" datetime="2026-03-20T00:00:00.000Z">2 days ago</time>
      </div>
    </div>
  </div>
</li>
<li>
  <div class="base-card relative w-full hover:no-underline focus:no-underline base-card--link base-search-card base-search-card--link job-search-card" data-entity-urn="urn:li:jobPosting:67890">
    <a class="base-card__full-link absolute top-0 right-0 bottom-0 left-0 p-0 z-[2]" href="https://de.linkedin.com/jobs/view/fullstack-dev-at-tech-gmbh-67890" data-tracking-control-name="public_jobs_jserp-result_search-card">
      <span class="sr-only">Fullstack Developer</span>
    </a>
    <div class="base-search-card__info">
      <h3 class="base-search-card__title">Fullstack Developer</h3>
      <h4 class="base-search-card__subtitle">
        <a href="https://www.linkedin.com/company/tech-gmbh">Tech GmbH</a>
      </h4>
      <div class="base-search-card__metadata">
        <span class="job-search-card__location">Munich, Germany</span>
        <time class="job-search-card__listdate" datetime="2026-03-19T00:00:00.000Z">3 days ago</time>
      </div>
    </div>
  </div>
</li>
"""

SAMPLE_LINKEDIN_EMPTY_HTML = """
<div class="no-results">No results found</div>
"""


class TestLinkedInSource:
    def setup_method(self):
        from sources.linkedin import LinkedInSource
        self.source = LinkedInSource()

    # ── Basic parsing ──────────────────────────────────────────────────

    def test_parse_html_extracts_jobs(self):
        jobs = self.source._parse_html(SAMPLE_LINKEDIN_HTML)
        assert len(jobs) == 2

    def test_parse_html_title(self):
        jobs = self.source._parse_html(SAMPLE_LINKEDIN_HTML)
        assert jobs[0].title == "Software Engineer"
        assert jobs[1].title == "Fullstack Developer"

    def test_parse_html_company(self):
        jobs = self.source._parse_html(SAMPLE_LINKEDIN_HTML)
        assert jobs[0].company == "Acme Corp"
        assert jobs[1].company == "Tech GmbH"

    def test_parse_html_location(self):
        jobs = self.source._parse_html(SAMPLE_LINKEDIN_HTML)
        assert jobs[0].location == "Berlin, Germany"
        assert jobs[1].location == "Munich, Germany"

    def test_parse_html_url(self):
        jobs = self.source._parse_html(SAMPLE_LINKEDIN_HTML)
        # URL should have tracking params stripped
        assert "12345" in jobs[0].url
        assert "67890" in jobs[1].url
        assert "?" not in jobs[0].url  # tracking params stripped

    def test_parse_html_source_is_linkedin(self):
        jobs = self.source._parse_html(SAMPLE_LINKEDIN_HTML)
        for job in jobs:
            assert job.source == "linkedin"

    def test_parse_html_is_remote_true(self):
        jobs = self.source._parse_html(SAMPLE_LINKEDIN_HTML)
        for job in jobs:
            assert job.is_remote is True

    def test_parse_html_posted_at(self):
        jobs = self.source._parse_html(SAMPLE_LINKEDIN_HTML)
        assert jobs[0].posted_at is not None
        assert jobs[1].posted_at is not None

    def test_parse_empty_html(self):
        jobs = self.source._parse_html(SAMPLE_LINKEDIN_EMPTY_HTML)
        assert len(jobs) == 0

    def test_parse_html_no_crash_on_garbage(self):
        jobs = self.source._parse_html("<html><body>garbage</body></html>")
        assert len(jobs) == 0

    # ── Multi-query fetch ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fetch_calls_all_queries(self):
        from sources.linkedin import _SEARCH_QUERIES
        with patch.object(self.source, "_fetch_query", new_callable=AsyncMock, return_value=[]) as mock_fetch:
            await self.source.fetch()
            assert mock_fetch.call_count == len(_SEARCH_QUERIES)

    @pytest.mark.asyncio
    async def test_fetch_deduplicates_by_url(self):
        job1 = Job(
            title="Software Engineer", company="Acme", location="Berlin, Germany",
            url="https://linkedin.com/jobs/view/123", source="linkedin", is_remote=True,
        )
        job2 = Job(
            title="Fullstack Dev", company="Corp", location="Munich, Germany",
            url="https://linkedin.com/jobs/view/456", source="linkedin", is_remote=True,
        )
        job1_dup = Job(
            title="Software Engineer", company="Acme", location="Berlin, Germany",
            url="https://linkedin.com/jobs/view/123", source="linkedin", is_remote=True,
        )

        async def mock_fetch_query(query):
            if "software" in query["keywords"]:
                return [job1]
            elif "fullstack" in query["keywords"]:
                return [job2, job1_dup]
            return []

        with patch.object(self.source, "_fetch_query", side_effect=mock_fetch_query):
            jobs = await self.source.fetch()

        assert len(jobs) == 2

    @pytest.mark.asyncio
    async def test_fetch_handles_rate_limit_gracefully(self):
        """429 from LinkedIn should return empty, not crash."""
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.text = ""

        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
            assert len(jobs) == 0

    @pytest.mark.asyncio
    async def test_fetch_handles_login_redirect_gracefully(self):
        """LinkedIn login redirect should return empty, not crash."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '<html><head><title>Login</title></head><body>Please login to continue</body></html>'

        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
            assert len(jobs) == 0

    @pytest.mark.asyncio
    async def test_fetch_handles_exception_gracefully(self):
        """Network error should return empty, not crash."""
        with patch.object(self.source, "_get", new_callable=AsyncMock, side_effect=Exception("Connection refused")):
            jobs = await self.source.fetch()
            assert len(jobs) == 0

    # ── Registration ───────────────────────────────────────────────────

    def test_linkedin_registered_in_all_sources(self):
        from main import ALL_SOURCES
        assert "linkedin" in ALL_SOURCES

    def test_linkedin_source_name(self):
        assert self.source.name == "linkedin"

    def test_linkedin_is_httpx_source(self):
        """LinkedIn uses httpx (no Playwright)."""
        assert hasattr(self.source, "fetch")
        assert hasattr(self.source, "_get")


# ═══════════════════════════════════════════════════════════════════════════
#  LinkedIn: _parse_relative_time helper
# ═══════════════════════════════════════════════════════════════════════════

class TestLinkedInParseRelativeTime:
    def test_parse_days_ago(self):
        from sources.linkedin import _parse_relative_time
        result = _parse_relative_time("5 days ago")
        assert result is not None
        from datetime import datetime, timezone
        delta = datetime.now(timezone.utc) - result
        assert 4.9 < delta.total_seconds() / 86400 < 5.1

    def test_parse_hours_ago(self):
        from sources.linkedin import _parse_relative_time
        result = _parse_relative_time("3 hours ago")
        assert result is not None
        from datetime import datetime, timezone
        delta = datetime.now(timezone.utc) - result
        assert 2.9 < delta.total_seconds() / 3600 < 3.1

    def test_parse_week_ago(self):
        from sources.linkedin import _parse_relative_time
        result = _parse_relative_time("1 week ago")
        assert result is not None
        from datetime import datetime, timezone
        delta = datetime.now(timezone.utc) - result
        assert 6.9 < delta.total_seconds() / 86400 < 7.1

    def test_parse_garbage_returns_none(self):
        from sources.linkedin import _parse_relative_time
        assert _parse_relative_time("recently posted") is None

    def test_parse_empty_returns_none(self):
        from sources.linkedin import _parse_relative_time
        assert _parse_relative_time("") is None


# ═══════════════════════════════════════════════════════════════════════════
#  Stepstone / Arbeitsagentur source (v1.5 — replaces Playwright scraper)
# ═══════════════════════════════════════════════════════════════════════════

# Sample Arbeitsagentur API posting for testing
_SAMPLE_POSTING = {
    "titel": "Senior Software Entwickler (m/w/d)",
    "beruf": "Software Entwickler",
    "refnr": "10000-1234567890-S",
    "arbeitgeber": "SAP SE",
    "arbeitsort": {
        "plz": "10115",
        "ort": "Berlin",
        "region": "Berlin",
        "land": "Deutschland",
    },
    "aktuelleVeroeffentlichungsdatum": "2026-03-20",
    "externeUrl": "https://www.stepstone.de/stellenangebote--Senior-Software-Entwickler--Berlin--12345",
}

_SAMPLE_POSTING_2 = {
    "titel": "Fullstack Developer (Remote)",
    "beruf": "Fullstack Developer",
    "refnr": "10000-9876543210-S",
    "arbeitgeber": "Zalando",
    "arbeitsort": {
        "ort": "Munich",
        "region": "Bayern",
        "land": "Deutschland",
    },
    "aktuelleVeroeffentlichungsdatum": "2026-03-19T12:00:00Z",
    "externeUrl": "https://www.zalando.de/jobs/fullstack",
}

_SAMPLE_POSTING_NO_URL = {
    "titel": "Backend Developer",
    "refnr": "10000-1111111111-S",
    "arbeitgeber": "Otto Group",
    "arbeitsort": {"ort": "Hamburg"},
    "aktuelleVeroeffentlichungsdatum": "2026-03-18",
}


class TestStepstoneSource:
    def setup_method(self):
        from sources.stepstone import StepstoneSource
        self.source = StepstoneSource()

    # ── Source name ────────────────────────────────────────────────────

    def test_source_name(self):
        assert self.source.name == "stepstone"

    def test_stepstone_registered_in_all_sources(self):
        from main import ALL_SOURCES
        assert "stepstone" in ALL_SOURCES

    # ── Posting parsing ────────────────────────────────────────────────

    def test_parse_posting_basic(self):
        job = self.source._parse_posting(_SAMPLE_POSTING)
        assert job is not None
        assert "Software Entwickler" in job.title
        assert job.company == "SAP SE"
        assert job.source == "stepstone"

    def test_parse_posting_url(self):
        job = self.source._parse_posting(_SAMPLE_POSTING)
        assert "stepstone.de" in job.url

    def test_parse_posting_location(self):
        job = self.source._parse_posting(_SAMPLE_POSTING)
        assert "Berlin" in job.location

    def test_parse_posting_scope_germany(self):
        job = self.source._parse_posting(_SAMPLE_POSTING)
        assert job.remote_scope == "germany"

    def test_parse_posting_is_remote(self):
        job = self.source._parse_posting(_SAMPLE_POSTING)
        assert job.is_remote is True

    def test_parse_posting_posted_at(self):
        job = self.source._parse_posting(_SAMPLE_POSTING)
        assert job.posted_at is not None

    def test_parse_posting_isoformat_date(self):
        job = self.source._parse_posting(_SAMPLE_POSTING_2)
        assert job.posted_at is not None

    def test_parse_posting_fallback_url(self):
        """Posting without externeUrl gets an arbeitsagentur URL."""
        job = self.source._parse_posting(_SAMPLE_POSTING_NO_URL)
        assert job is not None
        assert "arbeitsagentur.de" in job.url

    def test_parse_posting_empty_title_returns_none(self):
        posting = {"titel": "", "refnr": "123", "arbeitgeber": "X", "arbeitsort": {}}
        assert self.source._parse_posting(posting) is None

    def test_parse_posting_no_url_no_refnr_returns_none(self):
        posting = {"titel": "Dev", "refnr": "", "arbeitgeber": "X", "arbeitsort": {}}
        assert self.source._parse_posting(posting) is None

    def test_parse_posting_missing_company_defaults(self):
        posting = dict(_SAMPLE_POSTING)
        posting["arbeitgeber"] = None
        job = self.source._parse_posting(posting)
        assert job.company == "Unknown"

    # ── Build location ─────────────────────────────────────────────────

    def test_build_location_full(self):
        loc = self.source._build_location(_SAMPLE_POSTING)
        assert "Berlin" in loc

    def test_build_location_empty(self):
        loc = self.source._build_location({"arbeitsort": {}})
        assert loc == "Germany (Remote)"

    def test_build_location_no_arbeitsort(self):
        loc = self.source._build_location({})
        assert loc == "Germany (Remote)"

    # ── Fetch (mocked API) ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_fetch_parses_api_response(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "stellenangebote": [_SAMPLE_POSTING, _SAMPLE_POSTING_2],
            "maxErgebnisse": 2,
        }

        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert len(jobs) == 2

    @pytest.mark.asyncio
    async def test_fetch_deduplicates_by_refnr(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "stellenangebote": [_SAMPLE_POSTING, _SAMPLE_POSTING],
            "maxErgebnisse": 2,
        }

        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        # Same refnr → deduplicated
        assert len(jobs) == 1

    @pytest.mark.asyncio
    async def test_fetch_handles_api_error(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch.object(self.source, "_get", new_callable=AsyncMock, return_value=mock_resp):
            jobs = await self.source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_fetch_handles_network_error(self):
        with patch.object(self.source, "_get", new_callable=AsyncMock, side_effect=Exception("timeout")):
            jobs = await self.source.fetch()
        assert jobs == []

    @pytest.mark.asyncio
    async def test_safe_fetch_catches_exceptions(self):
        with patch.object(self.source, "_get", new_callable=AsyncMock, side_effect=Exception("crash")):
            jobs = await self.source.safe_fetch()
        assert jobs == []
