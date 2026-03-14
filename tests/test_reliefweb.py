"""Tests for ReliefWeb source (RSS-based)."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import feedparser
import pytest

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources.reliefweb import ReliefWebSource, _rss_url, _CATEGORY_QUERIES


# ── helpers ────────────────────────────────────────────────────────────────

_SAMPLE_RSS_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>ReliefWeb Jobs</title>
  {items}
</channel>
</rss>
"""

def _make_rss_item(
    title="ICT Systems Administrator",
    link="https://reliefweb.int/node/4200001",
    author="UNICEF",
    pub_date="Mon, 10 Mar 2026 08:00:00 +0000",
    country="Kenya",
    summary_country=None,
    categories=None,
    description="We are looking for an ICT Systems Administrator.",
):
    """Build an RSS <item> XML string."""
    summary_country = summary_country or country
    cats = categories or [country, author, "Information and Communications Technology"]
    cat_xml = "\n    ".join(f"<category>{c}</category>" for c in cats)
    return f"""\
  <item>
    <title>{title}</title>
    <link>{link}</link>
    <author>{author}</author>
    <pubDate>{pub_date}</pubDate>
    <description><![CDATA[
      <div class="tag country">Country: {summary_country}</div>
      <div class="tag source">Organization: {author}</div>
      <p>{description}</p>
    ]]></description>
    {cat_xml}
  </item>"""


def _make_rss_feed(*items):
    """Wrap item XML strings into a full RSS feed."""
    return _SAMPLE_RSS_TEMPLATE.format(items="\n".join(items))


def _parse_feed(rss_xml):
    """Parse RSS XML into feedparser entries."""
    return feedparser.parse(rss_xml).entries


def _mock_response(rss_xml):
    """Create a mock httpx response with the given RSS text."""
    resp = MagicMock()
    resp.status_code = 200
    resp.text = rss_xml
    return resp


# ═══════════════════════════════════════════════════════════════════════════
#  _rss_url
# ═══════════════════════════════════════════════════════════════════════════

class TestRssUrl:
    def test_ict_url(self):
        url = _rss_url("Information and Communications Technology")
        assert "reliefweb.int/jobs/rss.xml" in url
        assert "career_categories.exact" in url
        assert "Information" in url

    def test_url_encodes_quotes(self):
        url = _rss_url("Program/Project Management")
        # Quotes should be percent-encoded
        assert "%22" in url


# ═══════════════════════════════════════════════════════════════════════════
#  Parsing (_parse_entry)
# ═══════════════════════════════════════════════════════════════════════════

class TestReliefWebParseEntry:
    def test_basic_fields(self):
        source = ReliefWebSource()
        entry = _parse_feed(_make_rss_feed(_make_rss_item()))[0]
        job = source._parse_entry(entry)
        assert job is not None
        assert job.title == "ICT Systems Administrator"
        assert job.company == "UNICEF"
        assert job.source == "reliefweb"
        assert job.is_ngo is True
        assert job.url == "https://reliefweb.int/node/4200001"

    def test_location_from_html(self):
        source = ReliefWebSource()
        entry = _parse_feed(_make_rss_feed(
            _make_rss_item(country="Uganda", summary_country="Uganda")
        ))[0]
        job = source._parse_entry(entry)
        assert job.location == "Uganda"

    def test_posted_at_parsed(self):
        source = ReliefWebSource()
        entry = _parse_feed(_make_rss_feed(_make_rss_item()))[0]
        job = source._parse_entry(entry)
        assert job.posted_at is not None
        assert job.posted_at.year == 2026
        assert job.posted_at.month == 3
        assert job.posted_at.day == 10

    def test_tags_from_categories(self):
        source = ReliefWebSource()
        entry = _parse_feed(_make_rss_feed(
            _make_rss_item(categories=["Kenya", "UNICEF", "ICT"])
        ))[0]
        job = source._parse_entry(entry)
        assert "Kenya" in job.tags
        assert "UNICEF" in job.tags
        assert "ICT" in job.tags

    def test_no_title_returns_none(self):
        source = ReliefWebSource()
        entry = _parse_feed(_make_rss_feed(_make_rss_item(title="")))[0]
        assert source._parse_entry(entry) is None

    def test_no_link_returns_none(self):
        source = ReliefWebSource()
        # Build entry manually since feedparser may auto-assign link
        rss = _make_rss_feed(_make_rss_item(link=""))
        entry = _parse_feed(rss)[0]
        # Override link/id to empty to force None path
        entry["link"] = ""
        entry["id"] = ""
        assert source._parse_entry(entry) is None

    def test_description_truncated(self):
        source = ReliefWebSource()
        long_desc = "A" * 10000
        entry = _parse_feed(_make_rss_feed(_make_rss_item(description=long_desc)))[0]
        job = source._parse_entry(entry)
        assert len(job.description) <= 5000

    def test_is_remote_defaults_false(self):
        source = ReliefWebSource()
        entry = _parse_feed(_make_rss_feed(_make_rss_item()))[0]
        job = source._parse_entry(entry)
        assert job.is_remote is False


# ═══════════════════════════════════════════════════════════════════════════
#  _extract_location
# ═══════════════════════════════════════════════════════════════════════════

class TestExtractLocation:
    def test_country_from_html_div(self):
        entry = _parse_feed(_make_rss_feed(
            _make_rss_item(summary_country="Finland")
        ))[0]
        assert ReliefWebSource._extract_location(entry) == "Finland"

    def test_skips_org_name_in_tags(self):
        """Author name appearing as a tag should be skipped."""
        entry = _parse_feed(_make_rss_feed(
            _make_rss_item(
                author="Videre Est Credere",
                categories=["Videre Est Credere", "Kenya"],
                summary_country="Kenya",
            )
        ))[0]
        loc = ReliefWebSource._extract_location(entry)
        assert loc == "Kenya"

    def test_unspecified_when_no_location(self):
        """Returns Unspecified when no location can be extracted."""
        entry = _parse_feed(_make_rss_feed(
            _make_rss_item(
                summary_country="",
                categories=["UNICEF"],
                author="UNICEF",
            )
        ))[0]
        # Strip the country div from summary
        entry["summary"] = "<p>No location info</p>"
        loc = ReliefWebSource._extract_location(entry)
        assert loc == "Unspecified"


# ═══════════════════════════════════════════════════════════════════════════
#  _has_tech_title
# ═══════════════════════════════════════════════════════════════════════════

class TestHasTechTitle:
    def test_software_engineer(self):
        assert ReliefWebSource._has_tech_title("Software Engineer") is True

    def test_digital_programme_manager(self):
        assert ReliefWebSource._has_tech_title("Digital Programme Manager") is True

    def test_ict_officer(self):
        assert ReliefWebSource._has_tech_title("ICT Officer") is True

    def test_it_systems_lead(self):
        assert ReliefWebSource._has_tech_title("IT Systems Lead") is True

    def test_data_analyst(self):
        assert ReliefWebSource._has_tech_title("Data Analyst") is True

    def test_web_developer(self):
        assert ReliefWebSource._has_tech_title("Web Developer") is True

    def test_platform_engineer(self):
        assert ReliefWebSource._has_tech_title("Platform Engineer") is True

    def test_fullstack_developer(self):
        assert ReliefWebSource._has_tech_title("Fullstack Developer") is True

    def test_field_coordinator_rejected(self):
        assert ReliefWebSource._has_tech_title("Field Coordinator") is False

    def test_logistics_officer_rejected(self):
        assert ReliefWebSource._has_tech_title("Logistics Officer") is False

    def test_finance_manager_rejected(self):
        assert ReliefWebSource._has_tech_title("Finance Manager") is False

    def test_hr_specialist_rejected(self):
        assert ReliefWebSource._has_tech_title("HR Specialist") is False


# ═══════════════════════════════════════════════════════════════════════════
#  Fetch integration (mocked HTTP)
# ═══════════════════════════════════════════════════════════════════════════

class TestReliefWebFetch:
    @pytest.mark.asyncio
    async def test_fetch_parses_ict_items(self):
        source = ReliefWebSource()
        ict_feed = _make_rss_feed(
            _make_rss_item(),
            _make_rss_item(
                title="Data Analyst",
                link="https://reliefweb.int/node/4200002",
            ),
        )
        empty_feed = _make_rss_feed()

        with patch.object(
            source, "_get",
            side_effect=[
                _mock_response(ict_feed),
                _mock_response(empty_feed),
                _mock_response(empty_feed),
            ],
        ):
            jobs = await source.fetch()

        assert len(jobs) == 2
        assert jobs[0].title == "ICT Systems Administrator"
        assert jobs[1].title == "Data Analyst"

    @pytest.mark.asyncio
    async def test_fetch_returns_empty_on_http_failure(self):
        source = ReliefWebSource()

        with patch.object(source, "_get", side_effect=Exception("Connection error")):
            jobs = await source.fetch()

        assert jobs == []

    @pytest.mark.asyncio
    async def test_fetch_handles_rate_limit(self):
        source = ReliefWebSource()
        rate_limited = MagicMock()
        rate_limited.status_code = 429

        with patch.object(source, "_get", return_value=rate_limited):
            jobs = await source.fetch()

        assert jobs == []

    @pytest.mark.asyncio
    async def test_fetch_skips_malformed_items(self):
        source = ReliefWebSource()
        good_item = _make_rss_item()
        bad_item = _make_rss_item(title="", link="")
        ict_feed = _make_rss_feed(good_item, bad_item)
        empty_feed = _make_rss_feed()

        with patch.object(
            source, "_get",
            side_effect=[
                _mock_response(ict_feed),
                _mock_response(empty_feed),
                _mock_response(empty_feed),
            ],
        ):
            jobs = await source.fetch()

        assert len(jobs) == 1

    @pytest.mark.asyncio
    async def test_fetch_ppm_tech_titles_included(self):
        """PPM jobs with tech-related titles are included."""
        source = ReliefWebSource()
        empty_feed = _make_rss_feed()
        ppm_feed = _make_rss_feed(
            _make_rss_item(
                title="Digital Programme Manager",
                link="https://reliefweb.int/node/4300001",
            ),
            _make_rss_item(
                title="IT Systems Engineer",
                link="https://reliefweb.int/node/4300002",
            ),
        )

        with patch.object(
            source, "_get",
            side_effect=[
                _mock_response(empty_feed),   # ICT
                _mock_response(ppm_feed),      # PPM
                _mock_response(empty_feed),    # IM
            ],
        ):
            jobs = await source.fetch()

        assert len(jobs) == 2
        titles = {j.title for j in jobs}
        assert "Digital Programme Manager" in titles
        assert "IT Systems Engineer" in titles

    @pytest.mark.asyncio
    async def test_fetch_ppm_non_tech_titles_excluded(self):
        """PPM jobs without tech keywords are excluded."""
        source = ReliefWebSource()
        empty_feed = _make_rss_feed()
        ppm_feed = _make_rss_feed(
            _make_rss_item(
                title="Field Coordinator",
                link="https://reliefweb.int/node/4300010",
            ),
            _make_rss_item(
                title="Logistics Officer",
                link="https://reliefweb.int/node/4300011",
            ),
        )

        with patch.object(
            source, "_get",
            side_effect=[
                _mock_response(empty_feed),   # ICT
                _mock_response(ppm_feed),      # PPM
                _mock_response(empty_feed),    # IM
            ],
        ):
            jobs = await source.fetch()

        assert len(jobs) == 0

    @pytest.mark.asyncio
    async def test_fetch_deduplicates_across_queries(self):
        """Same job appearing in both ICT and PPM is not duplicated."""
        source = ReliefWebSource()
        shared_item = _make_rss_item(
            title="Software Engineer",
            link="https://reliefweb.int/node/4400001",
        )
        ict_feed = _make_rss_feed(shared_item)
        ppm_feed = _make_rss_feed(shared_item)
        empty_feed = _make_rss_feed()

        with patch.object(
            source, "_get",
            side_effect=[
                _mock_response(ict_feed),
                _mock_response(ppm_feed),
                _mock_response(empty_feed),
            ],
        ):
            jobs = await source.fetch()

        assert len(jobs) == 1

    @pytest.mark.asyncio
    async def test_fetch_im_tech_titles_included(self):
        """Information Management jobs with tech titles are included."""
        source = ReliefWebSource()
        empty_feed = _make_rss_feed()
        im_feed = _make_rss_feed(
            _make_rss_item(
                title="Data Analytics Engineer",
                link="https://reliefweb.int/node/4500001",
            ),
            _make_rss_item(
                title="Assessment Officer",
                link="https://reliefweb.int/node/4500002",
            ),
        )

        with patch.object(
            source, "_get",
            side_effect=[
                _mock_response(empty_feed),    # ICT
                _mock_response(empty_feed),    # PPM
                _mock_response(im_feed),       # IM
            ],
        ):
            jobs = await source.fetch()

        assert len(jobs) == 1
        assert jobs[0].title == "Data Analytics Engineer"
