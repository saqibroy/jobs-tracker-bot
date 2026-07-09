#!/usr/bin/env python3
"""Discover, validate, preview, and optionally promote ATS company boards.

The regular bot intentionally uses an explicit `companies.toml` registry. This
tool automates the boring part around that registry:

1. collect candidate ATS boards from env seed lists, text files, URLs, domains,
   and optional Google Custom Search;
2. validate each public board endpoint;
3. run a no-write eligibility/profile preview through the same filters the bot
   uses in production;
4. write candidate reports; and
5. optionally append passing boards to `companies.toml`.

Promotion is explicit (`--promote`) and threshold-gated by default. That keeps
discovery automated without letting stale dorks poison production.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import httpx
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 local venv
    import tomli as tomllib

from filters.language import passes_language_filter
from filters.location import apply_eligibility
from filters.match import compute_match_score
from filters.role import passes_role_filter
from models.job import Job
from sources.ashby import AshbySource
from sources.greenhouse import GreenhouseSource
from sources.jsonld import JsonLdCareerSource
from sources.lever import LeverSource
from sources.personio import PersonioSource
from sources.registry import CompanyBoard, load_company_boards
from sources.workable import WorkableSource


DIRECT_PROVIDERS = {"ashby", "greenhouse", "personio", "lever", "workable", "jsonld"}
UNSUPPORTED_DISCOVERED_PROVIDERS = {"teamtailor", "bamboohr"}

ENV_KEYS = {
    "ASHBY_COMPANIES": "ashby",
    "PERSONIO_COMPANIES": "personio",
    "GREENHOUSE_COMPANIES": "greenhouse",
    "LEVER_COMPANIES": "lever",
    "WORKABLE_COMPANIES": "workable",
    "TEAMTAILOR_COMPANIES": "teamtailor",
    "BAMBOOHR_COMPANIES": "bamboohr",
}

ATS_URL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ashby", re.compile(r"https?://jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)", re.I)),
    ("personio", re.compile(r"https?://([A-Za-z0-9-]+)\.jobs\.personio\.de", re.I)),
    ("greenhouse", re.compile(r"https?://(?:boards|job-boards)\.greenhouse\.io/([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"https?://jobs\.lever\.co/([A-Za-z0-9_-]+)", re.I)),
    ("workable", re.compile(r"https?://apply\.workable\.com/([A-Za-z0-9_-]+)", re.I)),
    ("teamtailor", re.compile(r"https?://([A-Za-z0-9-]+)\.teamtailor\.com", re.I)),
    ("bamboohr", re.compile(r"https?://([A-Za-z0-9-]+)\.bamboohr\.com/careers", re.I)),
)

JOIN_URL_RE = re.compile(r"https?://(?:www\.)?join\.com/(?:companies/)?[^\s\"')<>]+", re.I)
URL_RE = re.compile(r"https?://[^\s\"')<>]+", re.I)
DOMAIN_RE = re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}\b", re.I)

CAREER_PATHS = (
    "/careers", "/career", "/jobs", "/join-us", "/open-positions",
    "/en/careers", "/en/jobs", "/de/careers", "/de/jobs",
)

SEARCH_QUERIES = {
    "ashby": [
        'site:jobs.ashbyhq.com ("Berlin" OR "Germany") ("frontend" OR "full-stack" OR "backend")',
        'site:jobs.ashbyhq.com "remote" ("Germany" OR "Europe" OR "EMEA") engineer',
    ],
    "personio": [
        'site:jobs.personio.de ("Berlin" OR "Germany") ("frontend" OR "full-stack" OR "backend")',
        'site:jobs.personio.de "remote" "Germany" developer',
    ],
    "greenhouse": [
        'site:boards.greenhouse.io ("Berlin" OR "Germany") ("frontend" OR "full-stack" OR "backend")',
        'site:job-boards.greenhouse.io ("Berlin" OR "Germany") "software engineer"',
    ],
    "lever": [
        'site:jobs.lever.co ("Berlin" OR "Germany") ("frontend" OR "full-stack" OR "backend")',
        'site:jobs.lever.co "remote" ("Germany" OR "Europe" OR "EMEA") engineer',
    ],
    "workable": [
        'site:apply.workable.com ("Berlin" OR "Germany") ("frontend" OR "full-stack" OR "backend")',
    ],
    "join": [
        'site:join.com/companies ("Berlin" OR "Germany") ("frontend" OR "full stack" OR "software engineer")',
    ],
}


@dataclass(frozen=True)
class Candidate:
    provider: str
    slug: str
    company: str = ""
    url: str = ""
    region: str = "global"
    discovered_from: str = "unknown"

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.provider, self.slug.lower(), self.url.lower())


@dataclass
class Preview:
    raw_jobs: int = 0
    eligible: int = 0
    role_matches: int = 0
    immediate: int = 0
    digest: int = 0
    top_jobs: list[dict] = field(default_factory=list)


@dataclass
class ValidationResult:
    candidate: Candidate
    status: str
    reason: str = ""
    preview: Preview = field(default_factory=Preview)

    @property
    def promotable(self) -> bool:
        return self.status == "ok"


def _guess_company(slug: str) -> str:
    value = slug.strip().strip("/")
    value = re.sub(r"\.(com|de|io|ai|co|org|net)$", "", value, flags=re.I)
    value = value.replace("_", "-").replace(".", "-")
    return " ".join(part.capitalize() for part in value.split("-") if part) or slug


def _normalize_slug(provider: str, slug: str) -> str:
    slug = slug.strip().strip("/").strip()
    if provider == "ashby":
        # Ashby slugs are sometimes mixed case or include dots; both are valid.
        return slug
    return slug.lower()


def _candidate(provider: str, slug: str, *, company: str = "", url: str = "", source: str = "unknown", region: str = "global") -> Candidate:
    slug = _normalize_slug(provider, slug)
    return Candidate(
        provider=provider,
        slug=slug,
        company=company or _guess_company(slug),
        url=url,
        region=region,
        discovered_from=source,
    )


def _dedupe(candidates: Iterable[Candidate]) -> list[Candidate]:
    by_key: dict[tuple[str, str, str], Candidate] = {}
    for candidate in candidates:
        if not candidate.slug and not candidate.url:
            continue
        by_key.setdefault(candidate.key, candidate)
    return sorted(by_key.values(), key=lambda c: (c.provider, c.company.lower(), c.slug.lower(), c.url))


def candidates_from_env(path: Path) -> list[Candidate]:
    if not path.exists():
        return []
    return candidates_from_env_text(path.read_text(encoding="utf-8", errors="ignore"), source_prefix="env")


def candidates_from_env_text(text: str, *, source_prefix: str = "env") -> list[Candidate]:
    candidates: list[Candidate] = []
    for key, provider in ENV_KEYS.items():
        match = re.search(rf"^{re.escape(key)}=(.*)$", text, re.MULTILINE)
        if not match:
            continue
        for slug in (part.strip() for part in match.group(1).split(",")):
            if slug:
                candidates.append(_candidate(provider, slug, source=f"{source_prefix}:{key}"))
    return candidates


def candidates_from_text(path: Path) -> tuple[list[Candidate], list[str]]:
    if not path.exists():
        return [], []
    text = path.read_text(encoding="utf-8", errors="ignore")
    candidates: list[Candidate] = []
    domains: set[str] = set()

    # Parse env-style snippets pasted into a text file.
    candidates.extend(candidates_from_env_text(text, source_prefix=str(path)))

    for provider, pattern in ATS_URL_PATTERNS:
        for match in pattern.finditer(text):
            candidates.append(_candidate(provider, match.group(1), url=match.group(0), source=str(path)))

    for match in JOIN_URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;")
        slug = hashlib.sha1(url.encode()).hexdigest()[:12]
        candidates.append(_candidate("jsonld", slug, company=_guess_company(urlparse(url).path.split("/")[2] if "/companies/" in url else "JOIN"), url=url, source=str(path)))

    for match in DOMAIN_RE.finditer(text):
        domain = match.group(0).lower().strip(".,;")
        if not _looks_like_provider_domain(domain):
            domains.add(domain)
    return candidates, sorted(domains)


def _looks_like_provider_domain(domain: str) -> bool:
    return any(token in domain for token in (
        "ashbyhq.com", "personio.de", "greenhouse.io", "lever.co",
        "workable.com", "teamtailor.com", "bamboohr.com", "join.com",
    ))


async def candidates_from_google(api_key: str, cx: str, providers: set[str], per_query: int, client: httpx.AsyncClient) -> list[Candidate]:
    if not api_key or not cx:
        return []
    found: list[Candidate] = []
    for provider, queries in SEARCH_QUERIES.items():
        if provider not in providers and provider != "join":
            continue
        for query in queries:
            response = await client.get(
                "https://www.googleapis.com/customsearch/v1",
                params={"key": api_key, "cx": cx, "q": query, "num": min(max(per_query, 1), 10)},
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            for item in payload.get("items", []):
                url = item.get("link") or ""
                found.extend(candidates_from_urls([url], source=f"google:{provider}"))
    return found


def candidates_from_urls(urls: Iterable[str], *, source: str) -> list[Candidate]:
    text = "\n".join(urls)
    candidates: list[Candidate] = []
    for provider, pattern in ATS_URL_PATTERNS:
        for match in pattern.finditer(text):
            candidates.append(_candidate(provider, match.group(1), url=match.group(0), source=source))
    for match in JOIN_URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;")
        slug = hashlib.sha1(url.encode()).hexdigest()[:12]
        candidates.append(_candidate("jsonld", slug, company="JOIN", url=url, source=source))
    return candidates


async def detect_ats_for_domain(domain: str, client: httpx.AsyncClient) -> list[Candidate]:
    if _looks_like_provider_domain(domain):
        return []
    found: list[Candidate] = []
    urls = [f"https://{domain}{path}" for path in CAREER_PATHS]
    urls.extend([f"https://careers.{domain}", f"https://jobs.{domain}"])
    for url in urls:
        try:
            response = await client.get(url, timeout=10, follow_redirects=True)
        except httpx.HTTPError:
            continue
        if response.status_code >= 400:
            continue
        final_url = str(response.url)
        found.extend(candidates_from_urls([final_url], source=f"domain:{domain}"))
        # Many company career pages link to the ATS without redirecting.
        hrefs = re.findall(r'href=["\']([^"\']+)["\']', response.text[:250_000], flags=re.I)
        found.extend(candidates_from_urls(hrefs, source=f"domain:{domain}"))
        if found:
            break
    return found


def existing_registry() -> set[tuple[str, str, str]]:
    return {
        (board.provider, board.slug.lower(), board.url.lower())
        for board in load_company_boards(include_disabled=True)
    }


async def fetch_jobs(candidate: Candidate) -> list[Job]:
    board = CompanyBoard(
        company=candidate.company,
        provider=candidate.provider,
        slug=candidate.slug,
        region=candidate.region,
        url=candidate.url,
    )
    if candidate.provider == "ashby":
        return await AshbySource()._fetch_company(board)
    if candidate.provider == "personio":
        return await PersonioSource()._fetch_company(board)
    if candidate.provider == "greenhouse":
        return await GreenhouseSource()._fetch_board(board)
    if candidate.provider == "lever":
        return await LeverSource()._fetch_board(board)
    if candidate.provider == "workable":
        return await WorkableSource()._fetch_board(board)
    if candidate.provider == "jsonld":
        return await JsonLdCareerSource()._fetch_board(board)
    raise ValueError(f"unsupported provider: {candidate.provider}")


def preview_jobs(jobs: list[Job], top_n: int = 5) -> Preview:
    preview = Preview(raw_jobs=len(jobs))
    scored: list[Job] = []
    for job in jobs:
        if not apply_eligibility(job):
            continue
        preview.eligible += 1
        if not passes_language_filter(job):
            continue
        if not passes_role_filter(job):
            continue
        preview.role_matches += 1
        job.match_score = compute_match_score(job)
        if job.notification_tier == "immediate":
            preview.immediate += 1
        elif job.notification_tier == "digest":
            preview.digest += 1
        scored.append(job)

    scored.sort(key=lambda job: job.match_score, reverse=True)
    preview.top_jobs = [
        {
            "title": job.title,
            "location": job.location,
            "workplace_type": job.workplace_type,
            "score": job.match_score,
            "tier": job.notification_tier,
            "url": job.url,
        }
        for job in scored[:top_n]
    ]
    return preview


async def validate_candidate(candidate: Candidate, *, top_n: int) -> ValidationResult:
    if candidate.provider in UNSUPPORTED_DISCOVERED_PROVIDERS:
        return ValidationResult(candidate, "unsupported", f"{candidate.provider} adapter is not enabled yet")
    if candidate.provider not in DIRECT_PROVIDERS:
        return ValidationResult(candidate, "unsupported", f"unknown provider {candidate.provider}")
    try:
        jobs = await asyncio.wait_for(fetch_jobs(candidate), timeout=45)
    except Exception as exc:
        return ValidationResult(candidate, "failed", f"{type(exc).__name__}: {exc}")
    preview = preview_jobs(jobs, top_n=top_n)
    return ValidationResult(candidate, "ok", preview=preview)


def promotion_allowed(result: ValidationResult, *, min_jobs: int, min_eligible: int, min_matches: int) -> bool:
    if result.status != "ok":
        return False
    if result.candidate.key in existing_registry():
        return False
    preview = result.preview
    return (
        preview.raw_jobs >= min_jobs
        and preview.eligible >= min_eligible
        and preview.role_matches >= min_matches
    )


def toml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def board_to_toml(candidate: Candidate) -> str:
    lines = [
        "[[companies]]",
        f"company = {toml_quote(candidate.company)}",
        f"provider = {toml_quote(candidate.provider)}",
        f"slug = {toml_quote(candidate.slug)}",
    ]
    if candidate.region != "global":
        lines.append(f"region = {toml_quote(candidate.region)}")
    if candidate.url:
        lines.append(f"url = {toml_quote(candidate.url)}")
    lines.append("enabled = true")
    return "\n".join(lines)


def write_candidates_toml(results: list[ValidationResult], path: Path, *, promotable: set[tuple[str, str, str]]) -> None:
    lines = [
        "# Generated by scripts/discover_companies.py",
        f"# generated_at = {datetime.now(timezone.utc).isoformat()}",
        "# Review before promotion unless using --promote with thresholds.",
        "",
    ]
    for result in results:
        candidate = result.candidate
        preview = result.preview
        lines.extend([
            "[[candidates]]",
            f"company = {toml_quote(candidate.company)}",
            f"provider = {toml_quote(candidate.provider)}",
            f"slug = {toml_quote(candidate.slug)}",
        ])
        if candidate.url:
            lines.append(f"url = {toml_quote(candidate.url)}")
        if candidate.region != "global":
            lines.append(f"region = {toml_quote(candidate.region)}")
        lines.extend([
            f"status = {toml_quote(result.status)}",
            f"reason = {toml_quote(result.reason)}",
            f"discovered_from = {toml_quote(candidate.discovered_from)}",
            f"raw_jobs = {preview.raw_jobs}",
            f"eligible_jobs = {preview.eligible}",
            f"role_matches = {preview.role_matches}",
            f"immediate = {preview.immediate}",
            f"digest = {preview.digest}",
            f"promotable = {'true' if candidate.key in promotable else 'false'}",
            "",
        ])
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_jsonl(results: list[ValidationResult], path: Path, *, promotable: set[tuple[str, str, str]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for result in results:
            payload = asdict(result)
            payload["promotable"] = result.candidate.key in promotable
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def promote(results: list[ValidationResult], companies_path: Path, *, promotable: set[tuple[str, str, str]]) -> int:
    selected = [result.candidate for result in results if result.candidate.key in promotable]
    if not selected:
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    block = [
        "",
        f"# Auto-promoted by scripts/discover_companies.py on {stamp}.",
        "# Promotion thresholds were applied before writing these boards.",
        "",
    ]
    block.extend(board_to_toml(candidate) + "\n" for candidate in selected)
    with companies_path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(block).rstrip() + "\n")
    # Clear registry cache for same-process follow-up tests.
    load_company_boards.cache_clear()
    return len(selected)


def print_summary(results: list[ValidationResult], *, promotable: set[tuple[str, str, str]]) -> None:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    print("\nDiscovery summary")
    print("=================")
    print(f"candidates: {len(results)}")
    print("statuses: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())) if counts else "statuses: none")
    print(f"promotable: {len(promotable)}")
    print("")
    for result in sorted(results, key=lambda r: (r.candidate.key not in promotable, r.candidate.provider, -r.preview.role_matches, -r.preview.eligible, -r.preview.raw_jobs, r.candidate.company.lower()))[:50]:
        marker = "✅" if result.candidate.key in promotable else "•"
        p = result.preview
        print(
            f"{marker} {result.candidate.provider:<10} {result.candidate.slug:<28} "
            f"{result.status:<11} raw={p.raw_jobs:<4} eligible={p.eligible:<3} "
            f"matches={p.role_matches:<3} immediate={p.immediate:<2} digest={p.digest:<2} "
            f"{result.reason}"
        )


async def run(args: argparse.Namespace) -> int:
    logger.remove()
    logger.add(sys.stderr, level=args.log_level)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    candidates: list[Candidate] = []
    domains: set[str] = set()

    if args.from_env:
        candidates.extend(candidates_from_env(Path(args.env_file)))

    for path_value in args.seed_file:
        file_candidates, file_domains = candidates_from_text(Path(path_value))
        candidates.extend(file_candidates)
        domains.update(file_domains)

    for url in args.url:
        candidates.extend(candidates_from_urls([url], source="cli:url"))

    for domain in args.domain:
        domains.add(domain.lower())

    providers = set(args.provider or DIRECT_PROVIDERS)
    timeout = httpx.Timeout(connect=8, read=20, write=8, pool=8)
    limits = httpx.Limits(max_connections=max(2, args.concurrency), max_keepalive_connections=max(1, args.concurrency // 2))
    async with httpx.AsyncClient(headers={"User-Agent": "job-bot-company-discovery/1.0"}, timeout=timeout, limits=limits) as client:
        if args.google:
            candidates.extend(await candidates_from_google(
                os.getenv("GOOGLE_API_KEY", ""),
                os.getenv("GOOGLE_CX", ""),
                providers,
                args.google_results_per_query,
                client,
            ))

        if args.detect_domains and domains:
            semaphore = asyncio.Semaphore(max(1, args.concurrency))

            async def detect(domain: str) -> list[Candidate]:
                async with semaphore:
                    return await detect_ats_for_domain(domain, client)

            for detected in await asyncio.gather(*(detect(domain) for domain in sorted(domains))):
                candidates.extend(detected)

    candidates = [
        candidate for candidate in _dedupe(candidates)
        if candidate.provider in providers or candidate.provider in UNSUPPORTED_DISCOVERED_PROVIDERS
    ]

    if args.new_only:
        existing = existing_registry()
        candidates = [candidate for candidate in candidates if candidate.key not in existing]

    if args.limit:
        candidates = candidates[:args.limit]

    print(f"Discovered {len(candidates)} candidate boards")
    semaphore = asyncio.Semaphore(max(1, args.concurrency))

    async def validate(candidate: Candidate) -> ValidationResult:
        async with semaphore:
            result = await validate_candidate(candidate, top_n=args.top_jobs)
            p = result.preview
            print(
                f"{candidate.provider:<10} {candidate.slug:<28} {result.status:<11} "
                f"raw={p.raw_jobs:<4} eligible={p.eligible:<3} matches={p.role_matches:<3} {result.reason}"
            )
            return result

    results = await asyncio.gather(*(validate(candidate) for candidate in candidates))
    results = list(results)

    promotable = {
        result.candidate.key
        for result in results
        if promotion_allowed(
            result,
            min_jobs=args.min_jobs,
            min_eligible=args.min_eligible,
            min_matches=args.min_matches,
        )
    }

    write_jsonl(results, output_dir / "discovered_companies.jsonl", promotable=promotable)
    write_candidates_toml(results, output_dir / "companies.candidates.toml", promotable=promotable)

    promoted = 0
    if args.promote:
        promoted = promote(results, Path(args.companies_file), promotable=promotable)

    print_summary(results, promotable=promotable)
    print(f"\nWrote: {output_dir / 'discovered_companies.jsonl'}")
    print(f"Wrote: {output_dir / 'companies.candidates.toml'}")
    if args.promote:
        print(f"Promoted {promoted} boards into {args.companies_file}")
    else:
        print("Promotion skipped. Re-run with --promote to append passing boards to companies.toml.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-env", action="store_true", help="Use *_COMPANIES values from .env as seed candidates")
    parser.add_argument("--env-file", default=".env", help="Env file to read when --from-env is set")
    parser.add_argument("--seed-file", action="append", default=[], help="Text file containing URLs, domains, or env-style company lists")
    parser.add_argument("--url", action="append", default=[], help="Specific ATS/JOIN URL to inspect")
    parser.add_argument("--domain", action="append", default=[], help="Company domain to inspect for career-page ATS links")
    parser.add_argument("--detect-domains", action="store_true", help="Visit discovered/provided domains and detect ATS redirects/links")
    parser.add_argument("--google", action="store_true", help="Use Google Custom Search via GOOGLE_API_KEY and GOOGLE_CX")
    parser.add_argument("--google-results-per-query", type=int, default=10)
    parser.add_argument("--provider", action="append", choices=sorted(DIRECT_PROVIDERS | UNSUPPORTED_DISCOVERED_PROVIDERS), help="Restrict providers")
    parser.add_argument("--new-only", action="store_true", help="Skip boards already present in companies.toml")
    parser.add_argument("--limit", type=int, default=0, help="Validate at most N candidates after discovery/dedupe")
    parser.add_argument("--concurrency", type=int, default=3, help="Concurrent validation/detection requests")
    parser.add_argument("--top-jobs", type=int, default=5, help="Top matching jobs stored in JSONL preview")
    parser.add_argument("--min-jobs", type=int, default=1, help="Promotion threshold: minimum raw jobs")
    parser.add_argument("--min-eligible", type=int, default=1, help="Promotion threshold: minimum Germany/Berlin-eligible jobs")
    parser.add_argument("--min-matches", type=int, default=0, help="Promotion threshold: minimum eligible role/profile matches")
    parser.add_argument("--output-dir", default="data/discovery", help="Directory for generated reports")
    parser.add_argument("--companies-file", default="companies.toml", help="Registry file to append to with --promote")
    parser.add_argument("--promote", action="store_true", help="Append passing, non-duplicate boards to companies.toml")
    parser.add_argument("--log-level", default="WARNING", help="Log level for provider/filter internals")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
