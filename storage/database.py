"""SQLite-backed deduplication store.

Tracks which job URLs we've already seen so we only notify on new postings.
Easy to swap for PostgreSQL later — just replace the SQL calls.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import aiosqlite
from loguru import logger

import config
from models.job import Job

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    content_hash TEXT,
    title       TEXT NOT NULL,
    company     TEXT NOT NULL,
    location    TEXT,
    is_remote   INTEGER DEFAULT 1,
    remote_scope TEXT,
    url         TEXT NOT NULL UNIQUE,
    description TEXT,
    salary      TEXT,
    tags        TEXT,
    source      TEXT,
    is_ngo      INTEGER DEFAULT 0,
    posted_at   TEXT,
    fetched_at  TEXT NOT NULL,
    notified    INTEGER DEFAULT 0
);
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_content_hash ON jobs(content_hash);
"""


async def _db_path() -> str:
    """Ensure the directory for the DB exists, return the path."""
    path = config.DATABASE_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    return path


async def init_db() -> None:
    """Create the jobs table if it doesn't exist."""
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        await db.execute(_CREATE_TABLE)
        await db.execute(_CREATE_INDEX)
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


async def save_jobs(jobs: list[Job]) -> None:
    """Persist a batch of jobs to the database."""
    if not jobs:
        return
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        for job in jobs:
            await db.execute(
                """
                INSERT OR IGNORE INTO jobs
                    (id, content_hash, title, company, location, is_remote,
                     remote_scope, url, description, salary, tags, source,
                     is_ngo, posted_at, fetched_at, notified)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    job.id,
                    job.content_hash,
                    job.title,
                    job.company,
                    job.location,
                    int(job.is_remote),
                    job.remote_scope,
                    job.url,
                    job.description,
                    job.salary,
                    ",".join(job.tags),
                    job.source,
                    int(job.is_ngo),
                    job.posted_at.isoformat() if job.posted_at else None,
                    job.fetched_at.isoformat(),
                ),
            )
        await db.commit()
    logger.info("Saved {} jobs to database", len(jobs))


async def mark_notified(job_ids: list[str]) -> None:
    """Mark jobs as notified so the digest doesn't re-send them."""
    if not job_ids:
        return
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        placeholders = ",".join("?" for _ in job_ids)
        await db.execute(
            f"UPDATE jobs SET notified = 1 WHERE id IN ({placeholders})", job_ids
        )
        await db.commit()


async def get_recent_unnotified(hours: int = 6) -> list[dict]:
    """Fetch jobs from the last N hours that haven't been notified (for digest)."""
    path = await _db_path()
    cutoff = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM jobs
            WHERE notified = 0
              AND fetched_at >= datetime(?, '-' || ? || ' hours')
            ORDER BY fetched_at DESC
            """,
            (cutoff, hours),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


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

        return {
            "total": total,
            "ngo_count": ngo_count,
            "new_24h": new_24h,
            "sources": sources,
            "top_companies": top_companies,
            "last_fetched_at": last_fetched_at,
        }
