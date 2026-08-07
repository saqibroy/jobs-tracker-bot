"""SQLite-backed job deduplication, scan metrics, and delivery receipts.

Job rows track what has been seen; Phase 4 delivery receipts independently
track which logical destinations accepted each notification obligation.
"""

from __future__ import annotations

import os
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import aiosqlite
from loguru import logger

import config
from models.job import Job
from models.scan import ScanSummary, sanitize_source_error
from notifiers.base import (
    DELIVERY_DESTINATIONS,
    DELIVERY_KINDS,
    DISCORD_DESTINATIONS,
    DeliveryDestination,
    DeliveryKind,
    DeliverySuccess,
)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    content_hash TEXT,
    title       TEXT NOT NULL,
    company     TEXT NOT NULL,
    location    TEXT,
    is_remote   INTEGER DEFAULT 1,
    workplace_type TEXT DEFAULT 'unknown',
    eligible_countries TEXT DEFAULT '[]',
    eligible_regions TEXT DEFAULT '[]',
    remote_scope TEXT,
    url         TEXT NOT NULL UNIQUE,
    description TEXT,
    salary      TEXT,
    tags        TEXT,
    source      TEXT,
    is_ngo      INTEGER DEFAULT 0,
    match_score INTEGER DEFAULT 0,
    match_breakdown TEXT DEFAULT '{}',
    match_reasons TEXT DEFAULT '[]',
    eligibility_status TEXT DEFAULT 'unknown',
    eligibility_reasons TEXT DEFAULT '[]',
    notification_tier TEXT DEFAULT 'none',
    employment_relationship TEXT NOT NULL DEFAULT 'unknown',
    work_schedule TEXT NOT NULL DEFAULT 'unknown',
    contract_term TEXT NOT NULL DEFAULT 'unknown',
    weekly_hours INTEGER,
    contract_duration TEXT,
    freelance_rate TEXT,
    employment_reasons TEXT NOT NULL DEFAULT '[]',
    freelance_permission_required INTEGER NOT NULL DEFAULT 0,
    posting_language TEXT NOT NULL DEFAULT 'unknown',
    german_requirement_status TEXT NOT NULL DEFAULT 'unknown',
    german_requirement_level TEXT NOT NULL DEFAULT 'unknown',
    language_reasons TEXT NOT NULL DEFAULT '[]',
    posted_at   TEXT,
    fetched_at  TEXT NOT NULL,
    notified    INTEGER DEFAULT 0
);
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_content_hash ON jobs(content_hash);
"""

_CREATE_SOURCE_SCAN_RUNS = """
CREATE TABLE IF NOT EXISTS source_scan_runs (
    scan_id          TEXT NOT NULL,
    source           TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    completed_at     TEXT NOT NULL,
    duration_ms      INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL,
    raw_count        INTEGER NOT NULL DEFAULT 0,
    accepted_count   INTEGER NOT NULL DEFAULT 0,
    unseen_count     INTEGER NOT NULL DEFAULT 0,
    saved_count      INTEGER NOT NULL DEFAULT 0,
    rejection_counts TEXT NOT NULL DEFAULT '{}',
    routing_counts   TEXT NOT NULL DEFAULT '{}',
    issue_count      INTEGER NOT NULL DEFAULT 0,
    sanitized_error  TEXT,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (scan_id, source)
);
"""

_CREATE_JOB_DELIVERY_RECEIPTS = """
CREATE TABLE IF NOT EXISTS job_delivery_receipts (
    job_id        TEXT NOT NULL,
    delivery_kind TEXT NOT NULL,
    destination   TEXT NOT NULL,
    delivered_at  TEXT NOT NULL,
    PRIMARY KEY (job_id, delivery_kind, destination)
);
"""

_CREATE_DELIVERY_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_jobs_pending_delivery "
    "ON jobs(notification_tier, notified, fetched_at, match_score DESC);",
    "CREATE INDEX IF NOT EXISTS idx_delivery_receipts_obligation "
    "ON job_delivery_receipts(delivery_kind, destination, job_id);",
)

_CREATE_SOURCE_SCAN_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_source_scan_runs_completed "
    "ON source_scan_runs(completed_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_source_scan_runs_source_completed "
    "ON source_scan_runs(source, completed_at DESC);",
)

_SOURCE_HEALTH_QUERY = """
SELECT
    current.source,
    current.status,
    current.raw_count,
    current.accepted_count,
    current.saved_count,
    current.issue_count,
    current.sanitized_error,
    current.completed_at AS last_completed_at,
    (
        SELECT MAX(usable.completed_at)
        FROM source_scan_runs AS usable
        WHERE usable.source = current.source
          AND usable.status IN ('healthy', 'zero_results', 'partial_success')
    ) AS last_usable_at,
    (
        SELECT MAX(successful.completed_at)
        FROM source_scan_runs AS successful
        WHERE successful.source = current.source
          AND successful.status IN ('healthy', 'zero_results')
    ) AS last_fully_successful_at
FROM source_scan_runs AS current
WHERE current.rowid = (
    SELECT latest.rowid
    FROM source_scan_runs AS latest
    WHERE latest.source = current.source
    ORDER BY latest.completed_at DESC, latest.created_at DESC
    LIMIT 1
)
ORDER BY current.source
"""

# Migration: add match_score column for existing databases
_ADD_MATCH_SCORE_COL = """
ALTER TABLE jobs ADD COLUMN match_score INTEGER DEFAULT 0;
"""

_NEW_COLUMNS: dict[str, str] = {
    "workplace_type": "TEXT DEFAULT 'unknown'",
    "eligible_countries": "TEXT DEFAULT '[]'",
    "eligible_regions": "TEXT DEFAULT '[]'",
    "match_breakdown": "TEXT DEFAULT '{}'",
    "match_reasons": "TEXT DEFAULT '[]'",
    "eligibility_status": "TEXT DEFAULT 'unknown'",
    "eligibility_reasons": "TEXT DEFAULT '[]'",
    "notification_tier": "TEXT DEFAULT 'none'",
    "employment_relationship": "TEXT NOT NULL DEFAULT 'unknown'",
    "work_schedule": "TEXT NOT NULL DEFAULT 'unknown'",
    "contract_term": "TEXT NOT NULL DEFAULT 'unknown'",
    "weekly_hours": "INTEGER",
    "contract_duration": "TEXT",
    "freelance_rate": "TEXT",
    "employment_reasons": "TEXT NOT NULL DEFAULT '[]'",
    "freelance_permission_required": "INTEGER NOT NULL DEFAULT 0",
    "posting_language": "TEXT NOT NULL DEFAULT 'unknown'",
    "german_requirement_status": "TEXT NOT NULL DEFAULT 'unknown'",
    "german_requirement_level": "TEXT NOT NULL DEFAULT 'unknown'",
    "language_reasons": "TEXT NOT NULL DEFAULT '[]'",
}


async def _db_path() -> str:
    """Ensure the directory for the DB exists, return the path."""
    path = config.DATABASE_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    return path


async def init_db() -> None:
    """Create the jobs table if it doesn't exist, and run migrations."""
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        await db.execute(_CREATE_TABLE)
        await db.execute(_CREATE_INDEX)
        await db.execute(_CREATE_SOURCE_SCAN_RUNS)
        await db.execute(_CREATE_JOB_DELIVERY_RECEIPTS)
        for statement in _CREATE_SOURCE_SCAN_INDEXES:
            await db.execute(statement)
        # Migration: add match_score column if it doesn't exist
        try:
            await db.execute(_ADD_MATCH_SCORE_COL)
            logger.debug("Migration: added match_score column")
        except Exception:
            pass  # column already exists
        for column, definition in _NEW_COLUMNS.items():
            try:
                await db.execute(f"ALTER TABLE jobs ADD COLUMN {column} {definition}")
                logger.debug("Migration: added {} column", column)
            except Exception:
                pass
        # This jobs index depends on columns added by the compatibility
        # migrations above, so older databases must create it last.
        for statement in _CREATE_DELIVERY_INDEXES:
            await db.execute(statement)
        await db.commit()
    logger.info("Database initialized at {}", path)


async def is_seen(job_id: str) -> bool:
    """Return True if we've already stored a job with this id."""
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        return row is not None


async def filter_unseen(jobs: list[Job]) -> list[Job]:
    """Given a list of jobs, return only the ones we haven't seen before.

    Checks both URL-based id AND content_hash (title+company+location)
    to catch duplicates that have different URLs but are the same posting.
    """
    path = await _db_path()
    unseen: list[Job] = []
    async with aiosqlite.connect(path) as db:
        for job in jobs:
            # Check by URL hash
            cursor = await db.execute("SELECT 1 FROM jobs WHERE id = ?", (job.id,))
            if await cursor.fetchone():
                continue
            # Check by content hash (same title+company+location = duplicate)
            cursor = await db.execute(
                "SELECT 1 FROM jobs WHERE content_hash = ?", (job.content_hash,)
            )
            if await cursor.fetchone():
                logger.debug("Dedup SKIP (content_hash match): {}", job.title)
                continue
            unseen.append(job)
    logger.info("Dedup: {} total → {} new", len(jobs), len(unseen))
    return unseen


async def save_jobs(jobs: list[Job]) -> list[Job]:
    """Persist a batch and return only rows actually inserted by SQLite."""

    if not jobs:
        return []
    path = await _db_path()
    inserted: list[Job] = []
    async with aiosqlite.connect(path) as db:
        for job in jobs:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO jobs
                    (id, content_hash, title, company, location, is_remote,
                     workplace_type, eligible_countries, eligible_regions,
                     remote_scope, url, description, salary, tags, source,
                     is_ngo, match_score, match_breakdown, match_reasons,
                     eligibility_status, eligibility_reasons, notification_tier,
                     employment_relationship, work_schedule, contract_term,
                     weekly_hours, contract_duration, freelance_rate,
                     employment_reasons, freelance_permission_required,
                     posting_language, german_requirement_status,
                     german_requirement_level, language_reasons,
                     posted_at, fetched_at, notified)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, 0)
                """,
                (
                    job.id,
                    job.content_hash,
                    job.title,
                    job.company,
                    job.location,
                    int(job.is_remote),
                    job.workplace_type,
                    json.dumps(job.eligible_countries),
                    json.dumps(job.eligible_regions),
                    job.remote_scope,
                    job.url,
                    job.description,
                    job.salary,
                    ",".join(job.tags),
                    job.source,
                    int(job.is_ngo),
                    job.match_score,
                    json.dumps(job.match_breakdown),
                    json.dumps(job.match_reasons),
                    job.eligibility_status,
                    json.dumps(job.eligibility_reasons),
                    job.notification_tier,
                    job.employment_relationship,
                    job.work_schedule,
                    job.contract_term,
                    job.weekly_hours,
                    job.contract_duration,
                    job.freelance_rate,
                    json.dumps(job.employment_reasons),
                    int(job.freelance_permission_required),
                    job.posting_language,
                    job.german_requirement_status,
                    job.german_requirement_level,
                    json.dumps(job.language_reasons),
                    job.posted_at.isoformat() if job.posted_at else None,
                    job.fetched_at.isoformat(),
                ),
            )
            if cursor.rowcount == 1:
                inserted.append(job)
        await db.commit()
    logger.info("Saved {} jobs to database", len(inserted))
    return inserted


async def persist_scan_metrics(summary: ScanSummary, retention_days: int = 30) -> None:
    """Persist one bounded metrics row per source and prune expired history."""

    summary.validate_accounting()
    path = await _db_path()
    created_at = datetime.now(timezone.utc)
    rows = [
        (
            summary.scan_id,
            metrics.source,
            metrics.started_at.isoformat(),
            metrics.completed_at.isoformat(),
            max(0, metrics.duration_ms),
            metrics.status.value,
            metrics.raw_count,
            metrics.accepted_count,
            metrics.unseen_count,
            metrics.saved_count,
            metrics.rejection_counts_json(),
            metrics.routing_counts_json(),
            max(0, metrics.issue_count),
            sanitize_source_error(metrics.sanitized_error),
            created_at.isoformat(),
        )
        for metrics in summary.sources.values()
    ]
    cutoff = (created_at - timedelta(days=retention_days)).isoformat()
    async with aiosqlite.connect(path) as db:
        await db.executemany(
            """
            INSERT OR REPLACE INTO source_scan_runs (
                scan_id, source, started_at, completed_at, duration_ms, status,
                raw_count, accepted_count, unseen_count, saved_count,
                rejection_counts, routing_counts, issue_count, sanitized_error,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        # Cleanup occurs only after all metrics inserts above have succeeded.
        await db.execute("DELETE FROM source_scan_runs WHERE created_at < ?", (cutoff,))
        await db.commit()


async def cleanup_scan_metrics(retention_days: int = 30) -> int:
    """Delete expired metrics rows and return the affected-row count."""

    path = await _db_path()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute(
            "DELETE FROM source_scan_runs WHERE created_at < ?",
            (cutoff,),
        )
        await db.commit()
        return max(0, cursor.rowcount)


def _decode_counts(value: str | None) -> dict[str, int]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(key): int(count)
        for key, count in parsed.items()
        if isinstance(count, (int, float))
    }


def _decode_list(value: str | None) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _decode_dict(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def job_from_row(row: Mapping[str, Any]) -> Job:
    """Reconstruct a complete Job from a current or migrated SQLite row."""

    values = {
        key: value
        for key, value in dict(row).items()
        if key in Job.model_fields
    }
    values["location"] = values.get("location") or "Remote"
    values["source"] = values.get("source") or "unknown"
    tags = values.get("tags")
    if isinstance(tags, str):
        values["tags"] = [item.strip() for item in tags.split(",") if item.strip()]
    for key in (
        "eligible_countries",
        "eligible_regions",
        "match_reasons",
        "eligibility_reasons",
        "employment_reasons",
        "language_reasons",
    ):
        if key in values and isinstance(values[key], str):
            values[key] = _decode_list(values[key])
    if isinstance(values.get("match_breakdown"), str):
        values["match_breakdown"] = _decode_dict(values["match_breakdown"])
    for key in ("is_remote", "is_ngo", "freelance_permission_required"):
        if key in values:
            values[key] = bool(values[key])
    return Job.model_validate(values)


def _source_health_rows(rows: list[aiosqlite.Row]) -> list[dict]:
    return [
        {
            "source": row["source"],
            "status": row["status"],
            "raw": row["raw_count"],
            "accepted": row["accepted_count"],
            "saved": row["saved_count"],
            "issue_count": row["issue_count"],
            "sanitized_error": sanitize_source_error(row["sanitized_error"]),
            "last_completed_at": row["last_completed_at"],
            "last_usable_at": row["last_usable_at"],
            "last_fully_successful_at": row["last_fully_successful_at"],
        }
        for row in rows
    ]


async def get_latest_source_statuses() -> list[dict]:
    """Return each source's latest status and operational timestamps."""

    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(_SOURCE_HEALTH_QUERY)
        return _source_health_rows(await cursor.fetchall())


async def get_latest_source_status(source: str) -> dict | None:
    """Return the latest operational health record for one source."""

    for item in await get_latest_source_statuses():
        if item["source"] == source:
            return item
    return None


async def get_latest_scan_summary() -> dict | None:
    """Restore the latest completed production scan in health-payload shape."""

    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT scan_id
            FROM source_scan_runs
            GROUP BY scan_id
            ORDER BY MAX(completed_at) DESC, MAX(created_at) DESC
            LIMIT 1
            """
        )
        latest = await cursor.fetchone()
        if latest is None:
            return None
        cursor = await db.execute(
            "SELECT * FROM source_scan_runs WHERE scan_id = ? ORDER BY source",
            (latest["scan_id"],),
        )
        rows = await cursor.fetchall()

    raw = accepted = unseen = saved = 0
    rejection_counts: dict[str, int] = {}
    routing_counts = {
        "immediate": 0,
        "digest": 0,
        "explore": 0,
        "diagnostic": 0,
    }
    source_counts: dict[str, int] = {}
    completed_at: str | None = None
    for row in rows:
        raw += row["raw_count"]
        accepted += row["accepted_count"]
        unseen += row["unseen_count"]
        saved += row["saved_count"]
        source_counts[row["source"]] = row["raw_count"]
        completed_at = max(completed_at or row["completed_at"], row["completed_at"])
        for code, count in _decode_counts(row["rejection_counts"]).items():
            rejection_counts[code] = rejection_counts.get(code, 0) + count
        for route, count in _decode_counts(row["routing_counts"]).items():
            if route in routing_counts:
                routing_counts[route] += count

    source_health = await get_latest_source_statuses()
    return {
        "scan_id": latest["scan_id"],
        "completed_at": completed_at,
        "raw": raw,
        "eligible_role_matches": accepted,
        "rejected": sum(rejection_counts.values()),
        "immediate": routing_counts["immediate"],
        "digest": routing_counts["digest"],
        "explore": routing_counts["explore"],
        "diagnostic": routing_counts["diagnostic"],
        "sources": source_counts,
        "accepted": accepted,
        "unseen": unseen,
        "saved": saved,
        "rejection_counts": rejection_counts,
        "source_health": {
            item["source"]: {
                key: item.get(key)
                for key in (
                    "status",
                    "raw",
                    "accepted",
                    "saved",
                    "issue_count",
                    "sanitized_error",
                    "last_completed_at",
                    "last_usable_at",
                    "last_fully_successful_at",
                )
            }
            for item in source_health
        },
    }


get_latest_completed_scan = get_latest_scan_summary


async def mark_notified(job_ids: list[str]) -> None:
    """Legacy compatibility only: globally suppress pre-Phase-4 delivery.

    ``notified=1`` means only ``legacy_suppressed``. It does not prove which
    provider or destination received a historical job. New delivery paths must
    persist destination receipts instead and leave ``jobs.notified`` unchanged.
    """
    if not job_ids:
        return
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        await db.executemany(
            "UPDATE jobs SET notified = 1 WHERE id = ?",
            [(jid,) for jid in job_ids],
        )
        await db.commit()
    logger.info("Marked {} jobs as notified", len(job_ids))


async def get_recent_unnotified(hours: int = 6, limit: int = 15) -> list[dict]:
    """Legacy compatibility query; Phase 4 delivery uses receipt-backed pending."""
    path = await _db_path()
    cutoff = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM jobs
            WHERE notified = 0
              AND notification_tier = 'digest'
              AND fetched_at >= datetime(?, '-' || ? || ' hours')
            ORDER BY match_score DESC, fetched_at DESC
            LIMIT ?
            """,
            (cutoff, hours, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


def _validate_delivery_contract(
    delivery_kind: str,
    destination: str,
) -> tuple[DeliveryKind, DeliveryDestination]:
    if delivery_kind not in DELIVERY_KINDS:
        raise ValueError(f"unsupported delivery kind: {delivery_kind}")
    if destination not in DELIVERY_DESTINATIONS:
        raise ValueError(f"unsupported delivery destination: {destination}")
    return delivery_kind, destination  # type: ignore[return-value]


async def record_delivery_receipts(
    delivery_kind: DeliveryKind,
    successes: list[DeliverySuccess],
    *,
    delivered_at: datetime | None = None,
) -> int:
    """Persist exact destination successes transactionally and idempotently."""

    if delivery_kind not in DELIVERY_KINDS:
        raise ValueError(f"unsupported delivery kind: {delivery_kind}")
    if not successes:
        return 0
    for success in successes:
        _validate_delivery_contract(delivery_kind, success.destination)
    timestamp = (delivered_at or datetime.now(timezone.utc)).isoformat()
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        before = db.total_changes
        await db.executemany(
            """
            INSERT OR IGNORE INTO job_delivery_receipts
                (job_id, delivery_kind, destination, delivered_at)
            VALUES (?, ?, ?, ?)
            """,
            [
                (success.job_id, delivery_kind, success.destination, timestamp)
                for success in successes
            ],
        )
        await db.commit()
        inserted = db.total_changes - before
    logger.info(
        "Recorded {} new {} delivery receipt(s) from {} success(es)",
        inserted,
        delivery_kind,
        len(successes),
    )
    return inserted


async def get_pending_delivery_jobs(
    delivery_kind: DeliveryKind,
    destination: DeliveryDestination,
    *,
    limit: int = 15,
    max_age_days: int = 14,
    ngo_webhook_configured: bool = False,
    now: datetime | None = None,
) -> list[Job]:
    """Return one deterministic bounded batch missing a destination receipt.

    Historical ``notified=1`` rows are conservatively suppressed. For Discord,
    a receipt at either logical webhook satisfies the single Discord obligation,
    so later webhook configuration changes cannot resend an already delivered job.
    """

    delivery_kind, destination = _validate_delivery_contract(
        delivery_kind, destination
    )
    if limit < 1:
        return []
    if max_age_days < 1:
        raise ValueError("max_age_days must be positive")
    if destination == "discord_ngo":
        if delivery_kind != "immediate" or not ngo_webhook_configured:
            return []

    conditions = [
        "j.notified = 0",
        "j.notification_tier = ?",
        "j.fetched_at >= ?",
    ]
    params: list[Any] = [
        delivery_kind,
        ((now or datetime.now(timezone.utc)) - timedelta(days=max_age_days)).isoformat(),
    ]

    if destination in DISCORD_DESTINATIONS:
        conditions.append(
            """
            NOT EXISTS (
                SELECT 1 FROM job_delivery_receipts AS receipt
                WHERE receipt.job_id = j.id
                  AND receipt.delivery_kind = ?
                  AND receipt.destination IN ('discord_general', 'discord_ngo')
            )
            """
        )
        params.append(delivery_kind)
        if delivery_kind == "immediate":
            if destination == "discord_ngo":
                conditions.append("j.is_ngo = 1")
            elif ngo_webhook_configured:
                conditions.append("j.is_ngo = 0")
    else:
        conditions.append(
            """
            NOT EXISTS (
                SELECT 1 FROM job_delivery_receipts AS receipt
                WHERE receipt.job_id = j.id
                  AND receipt.delivery_kind = ?
                  AND receipt.destination = ?
            )
            """
        )
        params.extend((delivery_kind, destination))

    path = await _db_path()
    query = f"""
        SELECT j.*
        FROM jobs AS j
        WHERE {' AND '.join(conditions)}
        ORDER BY
            j.match_score DESC,
            COALESCE(j.posted_at, j.fetched_at) DESC,
            j.fetched_at DESC,
            j.id ASC
        LIMIT ?
    """
    params.append(min(limit, 15))
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        return [job_from_row(row) for row in await cursor.fetchall()]


async def get_delivery_receipts(job_id: str | None = None) -> list[dict[str, str]]:
    """Return receipts for diagnostics and focused migration/delivery tests."""

    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        if job_id is None:
            cursor = await db.execute(
                """
                SELECT job_id, delivery_kind, destination, delivered_at
                FROM job_delivery_receipts
                ORDER BY job_id, delivery_kind, destination
                """
            )
        else:
            cursor = await db.execute(
                """
                SELECT job_id, delivery_kind, destination, delivered_at
                FROM job_delivery_receipts
                WHERE job_id = ?
                ORDER BY delivery_kind, destination
                """,
                (job_id,),
            )
        return [dict(row) for row in await cursor.fetchall()]


async def get_total_count() -> int:
    """Return total number of jobs in the database (for health check)."""
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM jobs")
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_stats() -> dict:
    """Return comprehensive statistics about jobs in the database.

    Returns a dict with keys:
      - total: int
      - ngo_count: int
      - new_24h: int
      - sources: dict[str, int]  (source → count)
      - top_companies: list[tuple[str, int]]  (company, count) top 10
      - last_fetched_at: datetime | None
    """
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        # Total count
        cursor = await db.execute("SELECT COUNT(*) FROM jobs")
        total = (await cursor.fetchone())[0]

        # NGO count
        cursor = await db.execute("SELECT COUNT(*) FROM jobs WHERE is_ngo = 1")
        ngo_count = (await cursor.fetchone())[0]

        # New in last 24h
        now_iso = datetime.now(timezone.utc).isoformat()
        cursor = await db.execute(
            "SELECT COUNT(*) FROM jobs WHERE fetched_at >= datetime(?, '-24 hours')",
            (now_iso,),
        )
        new_24h = (await cursor.fetchone())[0]

        # Per-source breakdown
        cursor = await db.execute(
            "SELECT source, COUNT(*) as cnt FROM jobs GROUP BY source ORDER BY cnt DESC"
        )
        sources = {row[0]: row[1] for row in await cursor.fetchall()}

        cursor = await db.execute(
            "SELECT notification_tier, COUNT(*) FROM jobs GROUP BY notification_tier"
        )
        notification_tiers = {row[0]: row[1] for row in await cursor.fetchall()}

        # Top companies (top 10)
        cursor = await db.execute(
            "SELECT company, COUNT(*) as cnt FROM jobs GROUP BY company ORDER BY cnt DESC LIMIT 10"
        )
        top_companies = [(row[0], row[1]) for row in await cursor.fetchall()]

        # Last fetched_at timestamp
        cursor = await db.execute(
            "SELECT fetched_at FROM jobs ORDER BY fetched_at DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        last_fetched_at = None
        if row and row[0]:
            try:
                last_fetched_at = datetime.fromisoformat(row[0])
            except (ValueError, TypeError):
                pass

        cursor = await db.execute(_SOURCE_HEALTH_QUERY)
        source_health = _source_health_rows(await cursor.fetchall())

        return {
            "total": total,
            "ngo_count": ngo_count,
            "new_24h": new_24h,
            "sources": sources,
            "notification_tiers": notification_tiers,
            "top_companies": top_companies,
            "last_fetched_at": last_fetched_at,
            "source_health": source_health,
        }


async def get_weekly_ngo_jobs(days: int = 7, limit: int = 20) -> list[dict]:
    """Get NGO jobs from the last N days, sorted by match_score descending.

    Returns a list of dicts (one per job) with all DB columns.
    Used by the weekly NGO digest.
    """
    path = await _db_path()
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM jobs
            WHERE is_ngo = 1
              AND fetched_at > datetime(?, '-' || ? || ' days')
            ORDER BY match_score DESC, fetched_at DESC
            LIMIT ?
            """,
            (now_iso, days, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_weekly_general_count(days: int = 7) -> int:
    """Count non-NGO jobs tracked in the last N days."""
    path = await _db_path()
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute(
            """
            SELECT COUNT(*) FROM jobs
            WHERE is_ngo = 0
              AND fetched_at > datetime(?, '-' || ? || ' days')
            """,
            (now_iso, days),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def backfill_match_scores() -> int:
    """Re-compute match_score for all jobs that have score = 0 or NULL.

    Reconstructs a minimal Job object from each DB row, runs
    ``compute_match_score()``, and writes the result back.

    Returns the number of rows updated.
    """
    from filters.match import compute_match_score

    path = await _db_path()
    updated = 0
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT *
            FROM jobs
            WHERE match_score IS NULL OR match_score = 0
            """
        )
        rows = await cursor.fetchall()

        for row in rows:
            try:
                job = job_from_row(row)
            except Exception as exc:
                logger.debug("Backfill: skip row {}: {}", row["id"], exc)
                continue

            score = compute_match_score(job)
            if score > 0:
                await db.execute(
                    "UPDATE jobs SET match_score = ? WHERE id = ?",
                    (score, row["id"]),
                )
                updated += 1

        await db.commit()

    logger.info("Backfill: updated {} / {} jobs with match scores", updated, len(rows))
    return updated
