"""SQLite storage for read-only Zoho Mail ingestion.

The tables here are intentionally separate from the job-posting store. Email
records are keyed by ``account_id + message_id`` so the worker remains safe
when a message is moved between folders or a sync is rerun with overlap.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import aiosqlite
from loguru import logger

import config

_CREATE_SYNC_STATE = """
CREATE TABLE IF NOT EXISTS zoho_mail_sync_state (
    account_id TEXT PRIMARY KEY,
    last_successful_sync_at TEXT,
    api_domain TEXT,
    updated_at TEXT NOT NULL
);
"""

_CREATE_MESSAGES = """
CREATE TABLE IF NOT EXISTS zoho_mail_messages (
    account_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    folder_id TEXT,
    folder_name TEXT,
    subject TEXT,
    sender TEXT,
    message_date TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    likely_job INTEGER DEFAULT 0,
    processed INTEGER DEFAULT 0,
    PRIMARY KEY (account_id, message_id)
);
"""

_CREATE_APPLICATIONS = """
CREATE TABLE IF NOT EXISTS email_job_applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    company_name TEXT,
    company_domain TEXT,
    ats TEXT,
    ats_slug TEXT,
    ats_board_url TEXT,
    original_job_url TEXT,
    job_title TEXT,
    application_date TEXT,
    status TEXT,
    evidence TEXT DEFAULT '{}',
    confidence REAL DEFAULT 0,
    verified INTEGER DEFAULT 0,
    needs_review INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id, message_id, original_job_url, ats, ats_slug)
);
"""

_CREATE_REVIEW = """
CREATE TABLE IF NOT EXISTS email_application_review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER,
    account_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
"""


async def _db_path() -> str:
    path = config.DATABASE_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    return path


async def init_zoho_mail_db() -> None:
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        await db.execute(_CREATE_SYNC_STATE)
        await db.execute(_CREATE_MESSAGES)
        await db.execute(_CREATE_APPLICATIONS)
        await db.execute(_CREATE_REVIEW)
        await db.commit()
    logger.debug("Zoho Mail tables initialized at {}", path)


async def get_last_successful_sync_at(account_id: str) -> datetime | None:
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute(
            "SELECT last_successful_sync_at FROM zoho_mail_sync_state WHERE account_id = ?",
            (account_id,),
        )
        row = await cursor.fetchone()
    if not row or not row[0]:
        return None
    try:
        dt = datetime.fromisoformat(row[0])
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def save_successful_sync_checkpoint(
    account_id: str,
    *,
    synced_at: datetime,
    api_domain: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            INSERT INTO zoho_mail_sync_state
                (account_id, last_successful_sync_at, api_domain, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                last_successful_sync_at = excluded.last_successful_sync_at,
                api_domain = excluded.api_domain,
                updated_at = excluded.updated_at
            """,
            (account_id, synced_at.isoformat(), api_domain, now),
        )
        await db.commit()


async def upsert_message_summary(
    *,
    account_id: str,
    message_id: str,
    folder_id: str,
    folder_name: str,
    subject: str,
    sender: str,
    message_date: datetime | None,
    likely_job: bool,
    dry_run: bool,
) -> bool:
    """Upsert a message summary.

    Returns True when this is the first time the message was seen. Folder
    fields are updated on reruns so moved messages do not create duplicates.
    """
    if dry_run:
        return True

    now = datetime.now(timezone.utc).isoformat()
    message_date_iso = message_date.isoformat() if message_date else None
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute(
            "SELECT 1 FROM zoho_mail_messages WHERE account_id = ? AND message_id = ?",
            (account_id, message_id),
        )
        first_seen = await cursor.fetchone() is None
        await db.execute(
            """
            INSERT INTO zoho_mail_messages
                (account_id, message_id, folder_id, folder_name, subject, sender,
                 message_date, first_seen_at, last_seen_at, likely_job, processed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(account_id, message_id) DO UPDATE SET
                folder_id = excluded.folder_id,
                folder_name = excluded.folder_name,
                subject = COALESCE(excluded.subject, zoho_mail_messages.subject),
                sender = COALESCE(excluded.sender, zoho_mail_messages.sender),
                message_date = COALESCE(excluded.message_date, zoho_mail_messages.message_date),
                last_seen_at = excluded.last_seen_at,
                likely_job = CASE
                    WHEN excluded.likely_job > zoho_mail_messages.likely_job THEN excluded.likely_job
                    ELSE zoho_mail_messages.likely_job
                END
            """,
            (
                account_id,
                message_id,
                folder_id,
                folder_name,
                subject,
                sender,
                message_date_iso,
                now,
                now,
                int(likely_job),
            ),
        )
        await db.commit()
    return first_seen


def _serialize_evidence(evidence: dict[str, Any] | None) -> str:
    return json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True)


async def upsert_application_record(record: Any, *, dry_run: bool) -> int | None:
    """Insert/update an extracted application record.

    Verified rows are protected: a verified row is never overwritten by a
    lower-confidence extraction. Unverified rows only take newer values when
    the incoming confidence is at least as high as the stored confidence.
    """
    if dry_run:
        return None

    now = datetime.now(timezone.utc).isoformat()
    payload = (
        asdict(record) if hasattr(record, "__dataclass_fields__") else dict(record)
    )
    evidence = _serialize_evidence(payload.get("evidence"))
    key = (
        payload["account_id"],
        payload["message_id"],
        payload.get("original_job_url") or "",
        payload.get("ats") or "",
        payload.get("ats_slug") or "",
    )
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM email_job_applications
            WHERE account_id = ?
              AND message_id = ?
              AND COALESCE(original_job_url, '') = ?
              AND COALESCE(ats, '') = ?
              AND COALESCE(ats_slug, '') = ?
            """,
            key,
        )
        existing = await cursor.fetchone()
        if existing:
            stored_conf = float(existing["confidence"] or 0)
            verified = bool(existing["verified"])
            incoming_conf = float(payload.get("confidence") or 0)
            if verified and incoming_conf < stored_conf:
                return int(existing["id"])
            if incoming_conf < stored_conf:
                return int(existing["id"])

            def keep(field: str) -> Any:
                value = payload.get(field)
                return value if value not in (None, "") else existing[field]

            await db.execute(
                """
                UPDATE email_job_applications SET
                    company_name = ?,
                    company_domain = ?,
                    ats = ?,
                    ats_slug = ?,
                    ats_board_url = ?,
                    original_job_url = ?,
                    job_title = ?,
                    application_date = ?,
                    status = ?,
                    evidence = ?,
                    confidence = ?,
                    needs_review = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    keep("company_name"),
                    keep("company_domain"),
                    keep("ats"),
                    keep("ats_slug"),
                    keep("ats_board_url"),
                    keep("original_job_url"),
                    keep("job_title"),
                    keep("application_date"),
                    keep("status"),
                    evidence,
                    incoming_conf,
                    int(payload.get("needs_review", False)),
                    now,
                    existing["id"],
                ),
            )
            await db.commit()
            return int(existing["id"])

        cursor = await db.execute(
            """
            INSERT INTO email_job_applications
                (account_id, message_id, company_name, company_domain, ats,
                 ats_slug, ats_board_url, original_job_url, job_title,
                 application_date, status, evidence, confidence, verified,
                 needs_review, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                payload["account_id"],
                payload["message_id"],
                payload.get("company_name"),
                payload.get("company_domain"),
                payload.get("ats"),
                payload.get("ats_slug"),
                payload.get("ats_board_url"),
                payload.get("original_job_url"),
                payload.get("job_title"),
                payload.get("application_date"),
                payload.get("status"),
                evidence,
                float(payload.get("confidence") or 0),
                int(payload.get("needs_review", False)),
                now,
                now,
            ),
        )
        await db.commit()
        return int(cursor.lastrowid or 0)


async def enqueue_review(
    *,
    application_id: int | None,
    account_id: str,
    message_id: str,
    reason: str,
    payload: dict[str, Any],
    dry_run: bool,
) -> None:
    if dry_run:
        return
    now = datetime.now(timezone.utc).isoformat()
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            INSERT INTO email_application_review_queue
                (application_id, account_id, message_id, reason, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                application_id,
                account_id,
                message_id,
                reason,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        await db.commit()


async def mark_message_processed(
    *,
    account_id: str,
    message_id: str,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "UPDATE zoho_mail_messages SET processed = 1 WHERE account_id = ? AND message_id = ?",
            (account_id, message_id),
        )
        await db.commit()
