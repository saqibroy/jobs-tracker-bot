"""Deterministic tests for automated company discovery plumbing."""

from __future__ import annotations

from pathlib import Path

from scripts.discover_companies import (
    Candidate,
    Preview,
    ValidationResult,
    candidates_from_env_text,
    candidates_from_text,
    promotion_allowed,
    promote,
)


def test_discovery_parses_env_seed_lists_without_network():
    candidates = candidates_from_env_text(
        "ASHBY_COMPANIES=taktile,n8n\n"
        "PERSONIO_COMPANIES=pitch,egym\n"
        "GREENHOUSE_COMPANIES=n26\n"
    )
    keys = {(candidate.provider, candidate.slug) for candidate in candidates}
    assert ("ashby", "taktile") in keys
    assert ("ashby", "n8n") in keys
    assert ("personio", "pitch") in keys
    assert ("greenhouse", "n26") in keys


def test_discovery_parses_ats_urls_domains_and_join_jsonld(tmp_path: Path):
    seed = tmp_path / "seed.txt"
    seed.write_text(
        "https://jobs.ashbyhq.com/rootglobal\n"
        "https://pitch.jobs.personio.de\n"
        "https://boards.greenhouse.io/parloa\n"
        "https://join.com/companies/example/jobs/123-frontend-engineer\n"
        "n26.com, contentful.com\n",
        encoding="utf-8",
    )
    candidates, domains = candidates_from_text(seed)
    keys = {(candidate.provider, candidate.slug) for candidate in candidates}
    assert ("ashby", "rootglobal") in keys
    assert ("personio", "pitch") in keys
    assert ("greenhouse", "parloa") in keys
    assert any(candidate.provider == "jsonld" and "join.com" in candidate.url for candidate in candidates)
    assert {"n26.com", "contentful.com"} <= set(domains)


def test_discovery_parses_provider_slug_seed_hints(tmp_path: Path):
    seed = tmp_path / "sniffed.txt"
    seed.write_text(
        "# first_seen=2026-07-11 source=linkedin\n"
        "# comment-only domains like ignored.example and job URLs are ignored\n"
        "# job_url=https://de.linkedin.com/jobs/view/example\n"
        "ashby:acme https://jobs.ashbyhq.com/acme\n"
        "jsonld:join-example https://join.com/companies/join-example\n",
        encoding="utf-8",
    )
    candidates, domains = candidates_from_text(seed)
    keys = {(candidate.provider, candidate.slug, candidate.url) for candidate in candidates}
    assert ("ashby", "acme", "https://jobs.ashbyhq.com/acme") in keys
    assert ("jsonld", "join-example", "https://join.com/companies/join-example") in keys
    assert domains == []


def test_promotion_thresholds_default_to_live_board_not_eligibility():
    candidate = Candidate(provider="ashby", slug="fresh-board-for-test", company="Fresh Board")
    failed = ValidationResult(candidate, "failed", "404")
    assert not promotion_allowed(failed, min_jobs=1, min_eligible=0, min_matches=0)

    no_eligible = ValidationResult(candidate, "ok", preview=Preview(raw_jobs=5, eligible=0))
    assert promotion_allowed(no_eligible, min_jobs=1, min_eligible=0, min_matches=0)
    assert not promotion_allowed(no_eligible, min_jobs=1, min_eligible=1, min_matches=0)

    good = ValidationResult(candidate, "ok", preview=Preview(raw_jobs=5, eligible=1, role_matches=0))
    assert promotion_allowed(good, min_jobs=1, min_eligible=1, min_matches=0)
    assert not promotion_allowed(good, min_jobs=1, min_eligible=1, min_matches=1)


def test_promote_appends_company_board(tmp_path: Path):
    companies = tmp_path / "companies.toml"
    companies.write_text("", encoding="utf-8")
    candidate = Candidate(provider="jsonld", slug="join-example", company="Example", url="https://join.com/companies/example/jobs/1")
    result = ValidationResult(candidate, "ok", preview=Preview(raw_jobs=1, eligible=1))
    promoted = promote([result], companies, promotable={candidate.key})
    text = companies.read_text(encoding="utf-8")
    assert promoted == 1
    assert 'provider = "jsonld"' in text
    assert 'url = "https://join.com/companies/example/jobs/1"' in text
