"""Passive ATS-board discovery from aggregator job URLs.

This module performs cheap URL parsing only. It does not validate boards and it
does not write to ``companies.toml``. Newly-seen boards are appended to a seed
file so ``scripts/discover_companies.py`` can validate/promote them later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from loguru import logger

from models.job import Job
from sources.registry import CompanyBoard, load_company_boards


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SNIFFED_SEED_FILE = ROOT / "data" / "discovery" / "sniffed_from_jobs.txt"

DIRECT_ATS_SOURCES = {
    "ashby",
    "greenhouse",
    "personio",
    "lever",
    "workable",
    "jsonld",
    "bamboohr",
}

AGGREGATOR_SOURCES = {
    "linkedin",
    "stepstone",
    "remotive",
    "arbeitnow",
    "himalayas",
    "remoteok",
    "weworkremotely",
    "idealist",
    "reliefweb",
    "techjobsforgood",
    "eurobrussels",
    "hours80k",
    "goodjobs",
    "devex",
    "nofluffjobs",
    "landingjobs",
    "themuse",
}

_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class CandidateCompany:
    company: str
    provider: str
    slug: str
    board_url: str
    detected_provider: str = ""
    source_job_source: str = ""
    source_job_title: str = ""
    source_job_url: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.provider.lower(), self.slug.lower())

    def seed_line(self) -> str:
        return f"{self.provider}:{self.slug} {self.board_url}"


def _display_name(slug: str) -> str:
    cleaned = re.sub(r"\.(com|de|io|ai|co|org|net)$", "", slug.strip(), flags=re.I)
    cleaned = cleaned.replace("_", "-").replace(".", "-")
    return " ".join(part.capitalize() for part in cleaned.split("-") if part) or slug


def _path_slug(path: str) -> str | None:
    for part in path.split("/"):
        part = unquote(part.strip())
        if part:
            return part
    return None


def _valid_slug(slug: str | None) -> bool:
    return bool(slug and _SLUG_RE.match(slug))


def _candidate(
    *,
    job: Job,
    provider: str,
    slug: str,
    board_url: str,
    detected_provider: str | None = None,
) -> CandidateCompany:
    return CandidateCompany(
        company=_display_name(slug),
        provider=provider,
        slug=slug,
        board_url=board_url.rstrip("/"),
        detected_provider=detected_provider or provider,
        source_job_source=job.source,
        source_job_title=job.title,
        source_job_url=job.url,
    )


def sniff_ats_company(job: Job) -> CandidateCompany | None:
    """Return an ATS candidate parsed from an aggregator job URL, if any."""
    if not job.url:
        return None

    try:
        parsed = urlparse(job.url)
    except ValueError:
        return None

    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    slug: str | None

    if host == "jobs.ashbyhq.com":
        slug = _path_slug(parsed.path)
        if _valid_slug(slug):
            return _candidate(
                job=job,
                provider="ashby",
                slug=slug,  # type: ignore[arg-type]
                board_url=f"https://jobs.ashbyhq.com/{slug}",
            )

    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        slug = _path_slug(parsed.path)
        if _valid_slug(slug):
            return _candidate(
                job=job,
                provider="greenhouse",
                slug=slug.lower(),  # type: ignore[union-attr]
                board_url=f"https://boards.greenhouse.io/{slug.lower()}",
            )

    if host.endswith(".jobs.personio.de"):
        slug = host.removesuffix(".jobs.personio.de")
        if _valid_slug(slug):
            return _candidate(
                job=job,
                provider="personio",
                slug=slug.lower(),
                board_url=f"https://{slug.lower()}.jobs.personio.de",
            )

    if host == "jobs.lever.co":
        slug = _path_slug(parsed.path)
        if _valid_slug(slug):
            return _candidate(
                job=job,
                provider="lever",
                slug=slug.lower(),  # type: ignore[union-attr]
                board_url=f"https://jobs.lever.co/{slug.lower()}",
            )

    if host == "apply.workable.com":
        slug = _path_slug(parsed.path)
        if _valid_slug(slug):
            return _candidate(
                job=job,
                provider="workable",
                slug=slug.lower(),  # type: ignore[union-attr]
                board_url=f"https://apply.workable.com/{slug.lower()}",
            )

    if host.endswith(".join.com") and host != "join.com":
        slug = host.removesuffix(".join.com")
        if _valid_slug(slug):
            return _candidate(
                job=job,
                provider="jsonld",
                slug=slug.lower(),
                board_url=f"https://{slug.lower()}.join.com",
                detected_provider="join",
            )

    if host == "join.com":
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0].lower() == "companies" and _valid_slug(parts[1]):
            slug = parts[1].lower()
            return _candidate(
                job=job,
                provider="jsonld",
                slug=slug,
                board_url=f"https://join.com/companies/{slug}",
                detected_provider="join",
            )

    return None


def existing_company_keys(boards: tuple[CompanyBoard, ...] | None = None) -> set[tuple[str, str]]:
    """Return case-insensitive ``(provider, slug)`` keys from companies.toml."""
    if boards is None:
        boards = load_company_boards(include_disabled=True)
    return {(board.provider.lower(), board.slug.lower()) for board in boards}


def is_known_company(
    candidate: CandidateCompany,
    *,
    existing_keys: set[tuple[str, str]] | None = None,
) -> bool:
    if existing_keys is None:
        existing_keys = existing_company_keys()
    return candidate.key in existing_keys


def _existing_seed_lines(path: Path) -> set[str]:
    if not path.exists():
        return set()
    lines: set[str] = set()
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.add(line.lower())
    return lines


def append_sniffed_candidates(
    jobs: list[Job],
    *,
    path: Path = DEFAULT_SNIFFED_SEED_FILE,
    existing_keys: set[tuple[str, str]] | None = None,
    now: datetime | None = None,
) -> int:
    """Append new ATS candidates from aggregator jobs to the discovery seed file.

    Returns the number of newly appended candidates. The output remains
    compatible with ``scripts/discover_companies.py --seed-file``.
    """
    if existing_keys is None:
        existing_keys = existing_company_keys()

    now = now or datetime.now(timezone.utc)
    seen_lines = _existing_seed_lines(path)
    appended: list[str] = []
    seen_candidates: set[tuple[str, str]] = set()

    for job in jobs:
        source = (job.source or "").lower()
        if source in DIRECT_ATS_SOURCES or source not in AGGREGATOR_SOURCES:
            continue

        candidate = sniff_ats_company(job)
        if candidate is None:
            continue
        if candidate.key in existing_keys or candidate.key in seen_candidates:
            continue

        seed_line = candidate.seed_line()
        if seed_line.lower() in seen_lines:
            continue

        seen_candidates.add(candidate.key)
        seen_lines.add(seed_line.lower())
        appended.extend([
            (
                f"# first_seen={now.isoformat(timespec='seconds')} "
                f"source={candidate.source_job_source} "
                f"title={candidate.source_job_title!r} "
                f"job_url={candidate.source_job_url}"
            ),
            seed_line,
        ])

    if not appended:
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = ""
    if path.exists() and path.read_text(encoding="utf-8", errors="ignore").strip():
        prefix = "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(prefix + "\n".join(appended).rstrip() + "\n")
    logger.info("ATS sniffing appended {} new candidate boards to {}", len(appended) // 2, path)
    return len(appended) // 2
