from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from models.job import Job
from sources.ats_url_sniffer import (
    CandidateCompany,
    append_sniffed_candidates,
    existing_company_keys,
    is_known_company,
    sniff_ats_company,
)
from sources.registry import CompanyBoard


def _job(url: str, *, source: str = "linkedin") -> Job:
    return Job(
        title="Senior Frontend Engineer",
        company="Aggregator Company",
        location="Berlin",
        url=url,
        source=source,
    )


def test_sniffs_ashby_url():
    candidate = sniff_ats_company(_job("https://jobs.ashbyhq.com/acme/123"))
    assert candidate is not None
    assert candidate.provider == "ashby"
    assert candidate.slug == "acme"
    assert candidate.board_url == "https://jobs.ashbyhq.com/acme"


def test_sniffs_greenhouse_urls():
    first = sniff_ats_company(_job("https://boards.greenhouse.io/acme/jobs/123"))
    second = sniff_ats_company(_job("https://job-boards.greenhouse.io/acme/jobs/123"))
    assert first is not None
    assert second is not None
    assert first.provider == "greenhouse"
    assert second.provider == "greenhouse"
    assert first.slug == "acme"
    assert second.slug == "acme"


def test_sniffs_personio_url():
    candidate = sniff_ats_company(_job("https://acme.jobs.personio.de/job/123"))
    assert candidate is not None
    assert candidate.provider == "personio"
    assert candidate.slug == "acme"
    assert candidate.board_url == "https://acme.jobs.personio.de"


def test_sniffs_lever_url():
    candidate = sniff_ats_company(_job("https://jobs.lever.co/acme/abc-123"))
    assert candidate is not None
    assert candidate.provider == "lever"
    assert candidate.slug == "acme"
    assert candidate.board_url == "https://jobs.lever.co/acme"


def test_sniffs_workable_url():
    candidate = sniff_ats_company(_job("https://apply.workable.com/acme/j/ABCDEF/"))
    assert candidate is not None
    assert candidate.provider == "workable"
    assert candidate.slug == "acme"
    assert candidate.board_url == "https://apply.workable.com/acme"


def test_sniffs_join_subdomain_as_jsonld_candidate():
    candidate = sniff_ats_company(_job("https://acme.join.com/jobs/123"))
    assert candidate is not None
    assert candidate.provider == "jsonld"
    assert candidate.detected_provider == "join"
    assert candidate.slug == "acme"
    assert candidate.board_url == "https://acme.join.com"


def test_sniffs_join_company_url_as_jsonld_candidate():
    candidate = sniff_ats_company(_job("https://join.com/companies/acme/jobs/123"))
    assert candidate is not None
    assert candidate.provider == "jsonld"
    assert candidate.detected_provider == "join"
    assert candidate.slug == "acme"
    assert candidate.board_url == "https://join.com/companies/acme"


def test_non_matching_url_returns_none():
    assert sniff_ats_company(_job("https://example.com/jobs/123")) is None


def test_existing_company_keys_are_case_insensitive():
    boards = (
        CompanyBoard(company="Acme", provider="Ashby", slug="Acme"),
        CompanyBoard(company="Pitch", provider="personio", slug="pitch"),
    )
    keys = existing_company_keys(boards)
    assert ("ashby", "acme") in keys
    assert ("personio", "pitch") in keys


def test_is_known_company_dedupes_against_provider_slug():
    candidate = CandidateCompany(
        company="Acme",
        provider="ashby",
        slug="ACME",
        board_url="https://jobs.ashbyhq.com/ACME",
    )
    assert is_known_company(candidate, existing_keys={("ashby", "acme")})


def test_append_sniffed_candidates_skips_direct_sources_and_existing_companies(tmp_path: Path):
    path = tmp_path / "sniffed_from_jobs.txt"
    jobs = [
        _job("https://jobs.ashbyhq.com/newco/123", source="linkedin"),
        _job("https://jobs.ashbyhq.com/existing/123", source="remoteok"),
        _job("https://jobs.ashbyhq.com/direct-source/123", source="ashby"),
        _job("https://example.com/jobs/123", source="linkedin"),
    ]

    appended = append_sniffed_candidates(
        jobs,
        path=path,
        existing_keys={("ashby", "existing")},
        now=datetime(2026, 7, 11, tzinfo=timezone.utc),
    )

    text = path.read_text(encoding="utf-8")
    assert appended == 1
    assert "ashby:newco https://jobs.ashbyhq.com/newco" in text
    assert "existing" not in text
    assert "direct-source" not in text

    appended_again = append_sniffed_candidates(
        jobs,
        path=path,
        existing_keys={("ashby", "existing")},
        now=datetime(2026, 7, 11, tzinfo=timezone.utc),
    )
    assert appended_again == 0
