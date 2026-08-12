#!/usr/bin/env python3
"""Read-only source-yield observations against the production jobs database."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from filters.pipeline import run_filter_pipeline  # noqa: E402
from models.job import Job  # noqa: E402
from models.scan import (  # noqa: E402
    SourceFetchOutcome,
    USABLE_SOURCE_STATUSES,
    sanitize_source_error,
    utc_now,
)
from sources.catalog import SOURCE_BY_NAME  # noqa: E402
from sources.http_budget import SourceHttpBudget  # noqa: E402

SCHEMA_VERSION = 1
MAX_ACCEPTED_PER_OBSERVATION = 200
MAX_REPORT_JOB_DETAILS = 100
MAX_REPORT_OBSERVATIONS = 400
MAX_TRACKED_REPORT_JOBS = 5_000
MAX_OBSERVATION_BYTES = 128 * 1024
MAX_LABEL_CHARS = 120
MAX_SOURCE_CHARS = 80
_TIERS = ("immediate", "digest", "explore", "none")


def _bounded_label(value: object, limit: int = MAX_LABEL_CHARS) -> str:
    return " ".join(str(value or "").split())[:limit]


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


@contextmanager
def open_production_db_readonly(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open an existing SQLite database without creating or migrating it."""

    resolved = Path(path).expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise FileNotFoundError(f"production database is not a file: {resolved}")
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        yield connection
    finally:
        connection.close()


def production_matches(
    database_path: str | Path,
    content_hashes: Sequence[str],
) -> dict[str, tuple[str, ...]]:
    """Return production source names for accepted content hashes."""

    distinct_hashes = tuple(dict.fromkeys(content_hashes))
    matches: dict[str, set[str]] = {}
    with open_production_db_readonly(database_path) as connection:
        if not distinct_hashes:
            connection.execute("SELECT 1 FROM jobs LIMIT 0").fetchall()
        else:
            placeholders = ",".join("?" for _ in distinct_hashes)
            rows = connection.execute(
                f"SELECT content_hash, source FROM jobs "
                f"WHERE content_hash IN ({placeholders})",
                distinct_hashes,
            ).fetchall()
            for content_hash, source in rows:
                if not isinstance(content_hash, str):
                    continue
                safe_source = _bounded_label(source, MAX_SOURCE_CHARS)
                matches.setdefault(content_hash, set())
                if safe_source:
                    matches[content_hash].add(safe_source)
    return {key: tuple(sorted(values)) for key, values in matches.items()}


def build_observation(
    outcome: SourceFetchOutcome,
    accepted_jobs: Sequence[Job],
    rejection_counts: dict[object, int],
    budget: SourceHttpBudget,
    database_path: str | Path,
    *,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Build one bounded observation without mutating application state."""

    matches = production_matches(
        database_path,
        [job.content_hash for job in accepted_jobs],
    )
    accepted: list[dict[str, Any]] = []
    for job in accepted_jobs[:MAX_ACCEPTED_PER_OBSERVATION]:
        production_sources = matches.get(job.content_hash, ())
        accepted.append(
            {
                "content_hash": job.content_hash,
                "company": _bounded_label(job.company),
                "title": _bounded_label(job.title),
                "match_score": max(0, min(100, int(job.match_score))),
                "notification_tier": (
                    job.notification_tier if job.notification_tier in _TIERS else "none"
                ),
                "already_in_production": bool(production_sources),
                "unique_at_observation": not production_sources,
                "production_sources": list(production_sources[:10]),
            }
        )

    timestamp = observed_at or utc_now()
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    safe_error = sanitize_source_error(outcome.sanitized_error)
    observation: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "observed_at": timestamp.astimezone(timezone.utc).isoformat(),
        "source": _bounded_label(outcome.source, MAX_SOURCE_CHARS),
        "status": outcome.status.value,
        "source_error": safe_error,
        "component_issue_count": max(0, int(outcome.issue_count)),
        "http": {
            "configured_limit": budget.configured_limit,
            "observed_peak": budget.observed_peak,
            "attempts": budget.total_attempts,
            "retries": budget.retry_count,
            "rate_limits": budget.rate_limit_count,
        },
        "raw_count": outcome.raw_count,
        "accepted_count": len(accepted_jobs),
        "rejected_count": max(0, outcome.raw_count - len(accepted_jobs)),
        "rejections": {
            str(getattr(code, "value", code)): max(0, int(count))
            for code, count in sorted(
                rejection_counts.items(),
                key=lambda item: str(getattr(item[0], "value", item[0])),
            )
            if count
        },
        "accepted": accepted,
        "accepted_truncated_count": max(
            0, len(accepted_jobs) - MAX_ACCEPTED_PER_OBSERVATION
        ),
    }
    return observation


def _encoded_observation(observation: dict[str, Any]) -> str:
    """Encode one line and defensively preserve the hard output bound."""

    candidate = dict(observation)
    candidate["accepted"] = list(observation.get("accepted", []))
    while True:
        encoded = json.dumps(candidate, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode("utf-8")) <= MAX_OBSERVATION_BYTES:
            return encoded
        accepted = candidate["accepted"]
        if not accepted:
            raise ValueError("observation exceeds the bounded JSONL record size")
        accepted.pop()
        candidate["accepted_truncated_count"] = (
            int(candidate.get("accepted_truncated_count", 0)) + 1
        )


def append_observation(output_path: str | Path, observation: dict[str, Any]) -> None:
    """Append one bounded JSONL record to the separate validation file."""

    path = Path(output_path).expanduser()
    if path.suffix.lower() != ".jsonl":
        raise ValueError("validation output must use the .jsonl suffix")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(_encoded_observation(observation))
        output.write("\n")


def _read_observations(path: str | Path) -> tuple[list[dict[str, Any]], int, int]:
    observations: list[dict[str, Any]] = []
    malformed = 0
    ignored = 0
    with Path(path).expanduser().open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if line_number > MAX_REPORT_OBSERVATIONS:
                ignored += 1
                continue
            if len(line.encode("utf-8")) > MAX_OBSERVATION_BYTES or not line.strip():
                malformed += 1
                continue
            try:
                item = json.loads(line)
            except (json.JSONDecodeError, UnicodeError):
                malformed += 1
                continue
            if not isinstance(item, dict):
                malformed += 1
                continue
            required = ("observed_at", "source", "status", "accepted", "http")
            if any(key not in item for key in required) or not isinstance(
                item.get("accepted"), list
            ):
                malformed += 1
                continue
            try:
                datetime.fromisoformat(str(item["observed_at"]).replace("Z", "+00:00"))
            except ValueError:
                malformed += 1
                continue
            observations.append(item)
    return observations, malformed, ignored


def aggregate_report(path: str | Path) -> dict[str, Any]:
    """Aggregate observations by accepted content hash."""

    observations, malformed, ignored = _read_observations(path)
    jobs: dict[str, dict[str, Any]] = {}
    untracked_distinct_hashes: set[str] = set()
    status_counts: Counter[str] = Counter()
    attempts: list[int] = []
    retries = 0
    rate_limits = 0
    peak = 0

    for observation in observations:
        status = _bounded_label(observation.get("status"), 40)
        status_counts[status] += 1
        http = observation.get("http")
        if isinstance(http, dict):
            attempt_count = _nonnegative_int(http.get("attempts"))
            attempts.append(attempt_count)
            retries += _nonnegative_int(http.get("retries"))
            rate_limits += _nonnegative_int(http.get("rate_limits"))
            peak = max(peak, _nonnegative_int(http.get("observed_peak")))

        seen_this_observation: set[str] = set()
        for item in observation.get("accepted", [])[:MAX_ACCEPTED_PER_OBSERVATION]:
            if not isinstance(item, dict):
                continue
            content_hash = str(item.get("content_hash", ""))
            if len(content_hash) != 64 or any(
                ch not in "0123456789abcdef" for ch in content_hash
            ):
                continue
            if content_hash not in jobs and len(jobs) >= MAX_TRACKED_REPORT_JOBS:
                untracked_distinct_hashes.add(content_hash)
                continue
            record = jobs.setdefault(
                content_hash,
                {
                    "content_hash": content_hash,
                    "company": "",
                    "title": "",
                    "match_score": 0,
                    "notification_tier": "none",
                    "observation_count": 0,
                    "unique_at_observation": False,
                    "already_in_production": False,
                    "production_sources": set(),
                },
            )
            record["company"] = _bounded_label(item.get("company"))
            record["title"] = _bounded_label(item.get("title"))
            record["match_score"] = min(100, _nonnegative_int(item.get("match_score")))
            tier = str(item.get("notification_tier", "none"))
            record["notification_tier"] = tier if tier in _TIERS else "none"
            record["unique_at_observation"] = bool(
                record["unique_at_observation"] or item.get("unique_at_observation")
            )
            record["already_in_production"] = bool(
                record["already_in_production"] or item.get("already_in_production")
            )
            sources = item.get("production_sources")
            if isinstance(sources, list):
                record["production_sources"].update(
                    _bounded_label(source, MAX_SOURCE_CHARS)
                    for source in sources[:10]
                    if _bounded_label(source, MAX_SOURCE_CHARS)
                )
            if content_hash not in seen_this_observation:
                record["observation_count"] += 1
                seen_this_observation.add(content_hash)

    successful = sum(
        status_counts.get(status.value, 0) for status in USABLE_SOURCE_STATUSES
    )
    failed = len(observations) - successful
    ordered_jobs = sorted(
        jobs.values(),
        key=lambda item: (-int(item["match_score"]), str(item["content_hash"])),
    )
    tier_counts = Counter(str(item["notification_tier"]) for item in ordered_jobs)
    companies = {
        str(item["company"]).casefold() for item in ordered_jobs if item["company"]
    }
    repeated = [item for item in ordered_jobs if int(item["observation_count"]) > 1]
    represented = [item for item in ordered_jobs if item["already_in_production"]]

    def public_job(item: dict[str, Any]) -> dict[str, Any]:
        return {
            **{key: value for key, value in item.items() if key != "production_sources"},
            "production_sources": sorted(item["production_sources"]),
        }

    timestamps = [str(item["observed_at"]) for item in observations]
    failure_statuses = {
        status: count
        for status, count in sorted(status_counts.items())
        if status not in {item.value for item in USABLE_SOURCE_STATUSES}
    }
    return {
        "observation_count": len(observations),
        "malformed_line_count": malformed,
        "ignored_observation_count": ignored,
        "first_observation": min(timestamps) if timestamps else None,
        "last_observation": max(timestamps) if timestamps else None,
        "successful_observations": successful,
        "failed_observations": failed,
        "status_counts": dict(sorted(status_counts.items())),
        "union_accepted_jobs": len(jobs) + len(untracked_distinct_hashes),
        "union_unique_at_observation_jobs": sum(
            1 for item in ordered_jobs if item["unique_at_observation"]
        ),
        "unique_companies": len(companies),
        "notification_tiers": {tier: tier_counts.get(tier, 0) for tier in _TIERS},
        "jobs_repeatedly_observed": {
            "count": len(repeated),
            "items": [public_job(item) for item in repeated[:MAX_REPORT_JOB_DETAILS]],
        },
        "jobs_already_represented": {
            "count": len(represented),
            "items": [public_job(item) for item in represented[:MAX_REPORT_JOB_DETAILS]],
        },
        "source_failures": failure_statuses,
        "http": {
            "attempts_total": sum(attempts),
            "attempts_min": min(attempts) if attempts else 0,
            "attempts_max": max(attempts) if attempts else 0,
            "retries_total": retries,
            "rate_limits_total": rate_limits,
            "observed_peak_max": peak,
        },
        "accepted_jobs": [
            public_job(item) for item in ordered_jobs[:MAX_REPORT_JOB_DETAILS]
        ],
        "accepted_job_details_truncated": max(
            0, len(ordered_jobs) - MAX_REPORT_JOB_DETAILS
        ),
        "aggregation_truncated": bool(untracked_distinct_hashes),
    }


async def run_probe(source_name: str, database_path: str | Path) -> dict[str, Any]:
    """Fetch one registered source and compare accepted hashes read-only."""

    definition = SOURCE_BY_NAME.get(source_name)
    if definition is None:
        raise ValueError(f"unknown source: {source_name}")
    source = definition.adapter_class()
    budget = SourceHttpBudget(config.MAX_CONCURRENT_HTTP_REQUESTS)
    source.bind_http_budget(budget)
    try:
        outcome = await source.fetch_outcome()
    finally:
        source.bind_http_budget(None)
    filter_summary = run_filter_pipeline(outcome.jobs, settings=config)
    return build_observation(
        outcome,
        filter_summary.accepted_jobs,
        filter_summary.rejection_counts,
        budget,
        database_path,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--source", choices=sorted(SOURCE_BY_NAME))
    mode.add_argument("--report", type=Path, metavar="JSONL")
    parser.add_argument("--output", type=Path, metavar="JSONL")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(config.DATABASE_PATH),
        help="existing production jobs SQLite file (opened mode=ro)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.report is not None:
        if args.output is not None:
            parser.error("--output is only valid with --source")
        try:
            report = aggregate_report(args.report)
        except (OSError, ValueError) as exc:
            parser.exit(1, f"source-yield report failed: {exc}\n")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    if args.output is None:
        parser.error("--output is required with --source")
    if args.output.expanduser().resolve() == args.database.expanduser().resolve():
        parser.error("validation output must be separate from the production database")
    try:
        observation = asyncio.run(run_probe(args.source, args.database))
        append_observation(args.output, observation)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, f"source-yield probe failed: {exc}\n")
    print(json.dumps(observation, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
