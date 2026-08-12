"""SQLite storage for read-only Zoho Mail application and alert ingestion.

Mail metadata remains separate from the job-posting store. Message records are
keyed by ``account_id + message_id`` so the worker remains safe when a message
is moved between folders or a sync is rerun with overlap.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from loguru import logger

import config
from integrations.job_alerts.contracts import (
    JobAlertItem,
    MAX_ALERT_ITEMS_PER_MESSAGE,
    MAX_COMPANY,
    MAX_EVIDENCE,
    MAX_EVIDENCE_LENGTH,
    MAX_ISSUES,
    MAX_ISSUE_LENGTH,
    MAX_LOCATION,
    MAX_SUMMARY,
    MAX_TITLE,
    MAX_URL,
    bounded_text,
    bounded_values,
)
from integrations.job_alerts.urls import alert_identity_key

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
    processing_version INTEGER NOT NULL DEFAULT 0,
    mail_intent TEXT,
    alert_provider TEXT,
    processing_result TEXT,
    processing_reason TEXT,
    PRIMARY KEY (account_id, message_id)
);
"""

_MESSAGE_MIGRATIONS = {
    "processing_version": "INTEGER NOT NULL DEFAULT 0",
    "mail_intent": "TEXT",
    "alert_provider": "TEXT",
    "processing_result": "TEXT",
    "processing_reason": "TEXT",
}

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

_CREATE_ALERT_ITEMS = """
CREATE TABLE IF NOT EXISTS email_job_alert_items (
    provider TEXT NOT NULL,
    identity_key TEXT NOT NULL,
    provider_item_id TEXT,
    canonical_url TEXT,
    content_hash TEXT NOT NULL,
    account_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT NOT NULL,
    summary TEXT,
    evidence TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'pending',
    terminal_outcome TEXT,
    terminal_reason TEXT,
    job_id TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    processed_at TEXT,
    PRIMARY KEY (provider, identity_key)
);
"""

_CREATE_ALERT_PROVIDER_HEALTH = """
CREATE TABLE IF NOT EXISTS email_job_alert_provider_health (
    provider TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    examined_count INTEGER NOT NULL DEFAULT 0,
    valid_count INTEGER NOT NULL DEFAULT 0,
    invalid_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT NOT NULL,
    last_successful_parse_at TEXT,
    processing_failure_count INTEGER NOT NULL DEFAULT 0,
    issue_text TEXT
);
"""

_CREATE_ALERT_OCCURRENCES = """
CREATE TABLE IF NOT EXISTS email_job_alert_occurrences (
    transport TEXT NOT NULL,
    mailbox_key TEXT NOT NULL,
    message_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    identity_key TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (transport, mailbox_key, message_id, provider, identity_key)
);
"""

_CREATE_ALERT_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_email_alert_items_state_seen "
    "ON email_job_alert_items(state, last_seen_at);",
    "CREATE INDEX IF NOT EXISTS idx_email_alert_items_message "
    "ON email_job_alert_items(account_id, message_id);",
    "CREATE INDEX IF NOT EXISTS idx_email_alert_occurrences_item "
    "ON email_job_alert_occurrences(provider, identity_key);",
)


@dataclass(frozen=True, slots=True)
class MessageProcessingState:
    processed: bool = False
    processing_version: int = 0
    mail_intent: str = ""
    alert_provider: str = ""
    processing_result: str = ""

    @property
    def current(self) -> bool:
        return self.processed and self.processing_version >= 1


@dataclass(frozen=True, slots=True)
class AlertItemPersistence:
    provider: str
    identity_key: str
    state: str
    terminal_outcome: str = ""
    job_id: str | None = None

    @property
    def input_key(self) -> str:
        return f"{self.provider}:{self.identity_key}"


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
        cursor = await db.execute("PRAGMA table_info(zoho_mail_messages)")
        message_columns = {str(row[1]) for row in await cursor.fetchall()}
        for column, definition in _MESSAGE_MIGRATIONS.items():
            if column not in message_columns:
                await db.execute(
                    f"ALTER TABLE zoho_mail_messages ADD COLUMN {column} {definition}"
                )
        await db.execute(_CREATE_ALERT_ITEMS)
        await db.execute(_CREATE_ALERT_PROVIDER_HEALTH)
        await db.execute(_CREATE_ALERT_OCCURRENCES)
        for statement in _CREATE_ALERT_INDEXES:
            await db.execute(statement)
        # Phase 6A1/A2 stored only the latest Zoho provenance on the globally
        # identified alert item. Preserve it in the mailbox-neutral occurrence
        # relation without changing any item identity or terminal result.
        await db.execute(
            """
            INSERT OR IGNORE INTO email_job_alert_occurrences
                (transport, mailbox_key, message_id, provider, identity_key,
                 first_seen_at, last_seen_at)
            SELECT 'zoho', account_id, message_id, provider, identity_key,
                   first_seen_at, last_seen_at
            FROM email_job_alert_items
            WHERE account_id <> '' AND message_id <> ''
            """
        )
        await db.commit()
    logger.debug("Zoho Mail tables initialized at {}", path)


async def get_last_successful_sync_at(account_id: str) -> datetime | None:
    path = config.DATABASE_PATH
    if not Path(path).is_file():
        return None
    try:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        async with aiosqlite.connect(uri, uri=True) as db:
            cursor = await db.execute(
                "SELECT last_successful_sync_at FROM zoho_mail_sync_state WHERE account_id = ?",
                (account_id,),
            )
            row = await cursor.fetchone()
    except Exception:
        return None
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
                bounded_text(account_id, 200),
                bounded_text(message_id, 200),
                bounded_text(folder_id, 200),
                bounded_text(folder_name, 200),
                bounded_text(subject, 500),
                bounded_text(sender, 320),
                message_date_iso,
                now,
                now,
                int(likely_job),
            ),
        )
        await db.commit()
    return first_seen


async def get_message_processing_state(
    *,
    account_id: str,
    message_id: str,
) -> MessageProcessingState:
    """Read routing generation state without creating or migrating SQLite."""

    path = config.DATABASE_PATH
    if not Path(path).is_file():
        return MessageProcessingState()
    try:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        async with aiosqlite.connect(uri, uri=True) as db:
            cursor = await db.execute(
                """
                SELECT processed, processing_version, mail_intent,
                       alert_provider, processing_result
                FROM zoho_mail_messages
                WHERE account_id = ? AND message_id = ?
                """,
                (account_id, message_id),
            )
            row = await cursor.fetchone()
    except Exception:
        return MessageProcessingState()
    if not row:
        return MessageProcessingState()
    return MessageProcessingState(
        processed=bool(row[0]),
        processing_version=int(row[1] or 0),
        mail_intent=str(row[2] or ""),
        alert_provider=str(row[3] or ""),
        processing_result=str(row[4] or ""),
    )


async def get_pending_alert_input_keys(
    *,
    account_id: str,
    message_id: str,
    transport: str = "zoho",
    mailbox_key: str | None = None,
) -> frozenset[str]:
    """Read bounded pending identities for replay without mutating SQLite."""

    path = config.DATABASE_PATH
    if not Path(path).is_file():
        return frozenset()
    try:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        async with aiosqlite.connect(uri, uri=True) as db:
            cursor = await db.execute(
                """
                SELECT item.provider, item.identity_key
                FROM email_job_alert_occurrences AS occurrence
                JOIN email_job_alert_items AS item
                  ON item.provider = occurrence.provider
                 AND item.identity_key = occurrence.identity_key
                WHERE occurrence.transport = ?
                  AND occurrence.mailbox_key = ?
                  AND occurrence.message_id = ?
                  AND item.state = 'pending'
                ORDER BY item.provider, item.identity_key
                LIMIT ?
                """,
                (
                    bounded_text(transport, 40).lower(),
                    bounded_text(mailbox_key or account_id, 200),
                    message_id,
                    MAX_ALERT_ITEMS_PER_MESSAGE,
                ),
            )
            rows = await cursor.fetchall()
    except Exception:
        return frozenset()
    return frozenset(f"{row[0]}:{row[1]}" for row in rows)


async def set_message_routing(
    *,
    account_id: str,
    message_id: str,
    intent: str,
    provider: str = "",
    result: str = "",
    reason: str = "",
    dry_run: bool,
) -> None:
    if dry_run:
        return
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            UPDATE zoho_mail_messages
            SET mail_intent = ?, alert_provider = ?, processing_result = ?,
                processing_reason = ?
            WHERE account_id = ? AND message_id = ?
            """,
            (
                bounded_text(intent, 80),
                bounded_text(provider, 80),
                bounded_text(result, 80),
                bounded_text(reason, MAX_ISSUE_LENGTH),
                account_id,
                message_id,
            ),
        )
        await db.commit()


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
            SELECT ?, ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM email_application_review_queue
                WHERE account_id = ? AND message_id = ? AND reason = ?
                  AND resolved_at IS NULL
            )
            """,
            (
                application_id,
                account_id,
                message_id,
                reason,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                now,
                account_id,
                message_id,
                reason,
            ),
        )
        await db.commit()


async def mark_message_processed(
    *,
    account_id: str,
    message_id: str,
    dry_run: bool,
    processing_version: int = 1,
    intent: str = "",
    provider: str = "",
    result: str = "handled",
    reason: str = "",
) -> None:
    if dry_run:
        return
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            UPDATE zoho_mail_messages SET
                processed = 1,
                processing_version = ?,
                mail_intent = COALESCE(NULLIF(?, ''), mail_intent),
                alert_provider = COALESCE(NULLIF(?, ''), alert_provider),
                processing_result = ?,
                processing_reason = ?
            WHERE account_id = ? AND message_id = ?
            """,
            (
                max(0, int(processing_version)),
                bounded_text(intent, 80),
                bounded_text(provider, 80),
                bounded_text(result, 80),
                bounded_text(reason, MAX_ISSUE_LENGTH),
                account_id,
                message_id,
            ),
        )
        await db.commit()


async def upsert_alert_item_pending(
    item: JobAlertItem,
    *,
    dry_run: bool,
    transport: str = "zoho",
    mailbox_key: str | None = None,
) -> AlertItemPersistence:
    """Persist a replayable item and retain an already-terminal state."""

    identity_key, content_hash = alert_identity_key(
        provider_item_id=item.provider_item_id,
        canonical_url=item.canonical_url,
        title=item.title,
        company=item.company,
        location=item.location,
    )
    if dry_run:
        return AlertItemPersistence(item.provider, identity_key, "pending")

    now = datetime.now(timezone.utc).isoformat()
    evidence = json.dumps(
        bounded_values(
            item.evidence,
            count=MAX_EVIDENCE,
            length=MAX_EVIDENCE_LENGTH,
        ),
        ensure_ascii=False,
    )
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            """
            INSERT INTO email_job_alert_items
                (provider, identity_key, provider_item_id, canonical_url,
                 content_hash, account_id, message_id, title, company, location,
                 summary, evidence, state, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            ON CONFLICT(provider, identity_key) DO UPDATE SET
                provider_item_id = COALESCE(NULLIF(excluded.provider_item_id, ''),
                                            email_job_alert_items.provider_item_id),
                canonical_url = COALESCE(NULLIF(excluded.canonical_url, ''),
                                         email_job_alert_items.canonical_url),
                content_hash = excluded.content_hash,
                account_id = excluded.account_id,
                message_id = excluded.message_id,
                title = excluded.title,
                company = excluded.company,
                location = excluded.location,
                summary = excluded.summary,
                evidence = excluded.evidence,
                last_seen_at = excluded.last_seen_at
            """,
            (
                item.provider,
                identity_key,
                bounded_text(item.provider_item_id, 200),
                bounded_text(item.canonical_url, MAX_URL),
                content_hash,
                bounded_text(item.account_id, 200),
                bounded_text(item.message_id, 200),
                bounded_text(item.title, MAX_TITLE),
                bounded_text(item.company, MAX_COMPANY),
                bounded_text(item.location, MAX_LOCATION),
                bounded_text(item.summary, MAX_SUMMARY),
                evidence,
                now,
                now,
            ),
        )
        await db.execute(
            """
            INSERT INTO email_job_alert_occurrences
                (transport, mailbox_key, message_id, provider, identity_key,
                 first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(transport, mailbox_key, message_id, provider, identity_key)
            DO UPDATE SET last_seen_at = excluded.last_seen_at
            """,
            (
                bounded_text(transport, 40).lower(),
                bounded_text(mailbox_key or item.account_id, 200),
                bounded_text(item.message_id, 200),
                item.provider,
                identity_key,
                now,
                now,
            ),
        )
        cursor = await db.execute(
            """
            SELECT state, terminal_outcome, job_id
            FROM email_job_alert_items
            WHERE provider = ? AND identity_key = ?
            """,
            (item.provider, identity_key),
        )
        row = await cursor.fetchone()
        await db.commit()
    assert row is not None
    return AlertItemPersistence(
        item.provider,
        identity_key,
        str(row["state"]),
        str(row["terminal_outcome"] or ""),
        str(row["job_id"]) if row["job_id"] else None,
    )


async def complete_alert_item_results(results: tuple[Any, ...]) -> None:
    """Mark saved/duplicate/rejected alert inputs terminal in one transaction."""

    if not results:
        return
    now = datetime.now(timezone.utc).isoformat()
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        for result in results:
            provider, separator, identity_key = str(result.input_key).partition(":")
            if not separator or not provider or not identity_key:
                raise ValueError("invalid alert ingestion input key")
            outcome = str(getattr(result.status, "value", result.status))
            if outcome not in {"saved", "duplicate", "rejected"}:
                raise ValueError(f"non-terminal alert outcome: {outcome}")
            cursor = await db.execute(
                """
                UPDATE email_job_alert_items SET
                    state = 'processed',
                    terminal_outcome = ?,
                    terminal_reason = ?,
                    job_id = ?,
                    processed_at = ?
                WHERE provider = ? AND identity_key = ? AND state = 'pending'
                """,
                (
                    outcome,
                    bounded_text(
                        getattr(result, "rejection_code", "")
                        or getattr(result, "explanation", ""),
                        MAX_ISSUE_LENGTH,
                    ),
                    bounded_text(getattr(result, "job_id", ""), 128) or None,
                    now,
                    provider,
                    identity_key,
                ),
            )
            if cursor.rowcount != 1:
                cursor = await db.execute(
                    """
                    SELECT state FROM email_job_alert_items
                    WHERE provider = ? AND identity_key = ?
                    """,
                    (provider, identity_key),
                )
                row = await cursor.fetchone()
                if not row or str(row[0]) != "processed":
                    raise RuntimeError(
                        "pending alert item disappeared before completion"
                    )
        await db.commit()


async def record_alert_provider_health(
    *,
    provider: str,
    status: str,
    examined_count: int,
    valid_count: int,
    invalid_count: int,
    issues: tuple[str, ...] = (),
    processing_failure: bool = False,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    now = datetime.now(timezone.utc).isoformat()
    issue_text = "; ".join(
        bounded_values(issues, count=MAX_ISSUES, length=MAX_ISSUE_LENGTH)
    )[: MAX_ISSUES * MAX_ISSUE_LENGTH]
    successful_at = now if not processing_failure else None
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            INSERT INTO email_job_alert_provider_health
                (provider, status, examined_count, valid_count, invalid_count,
                 last_attempt_at, last_successful_parse_at,
                 processing_failure_count, issue_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider) DO UPDATE SET
                status = excluded.status,
                examined_count = excluded.examined_count,
                valid_count = excluded.valid_count,
                invalid_count = excluded.invalid_count,
                last_attempt_at = excluded.last_attempt_at,
                last_successful_parse_at = COALESCE(
                    excluded.last_successful_parse_at,
                    email_job_alert_provider_health.last_successful_parse_at
                ),
                processing_failure_count =
                    email_job_alert_provider_health.processing_failure_count + ?,
                issue_text = excluded.issue_text
            """,
            (
                bounded_text(provider, 80),
                bounded_text(status, 80),
                max(0, examined_count),
                max(0, valid_count),
                max(0, invalid_count),
                now,
                successful_at,
                int(processing_failure),
                issue_text,
                int(processing_failure),
            ),
        )
        await db.commit()


async def cleanup_processed_alert_items(
    *,
    older_than_days: int = 90,
    dry_run: bool,
) -> int:
    if dry_run:
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - older_than_days * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute(
            """
            DELETE FROM email_job_alert_items
            WHERE state = 'processed' AND last_seen_at < ?
            """,
            (cutoff_iso,),
        )
        await db.execute(
            """
            DELETE FROM email_job_alert_occurrences
            WHERE NOT EXISTS (
                SELECT 1 FROM email_job_alert_items AS item
                WHERE item.provider = email_job_alert_occurrences.provider
                  AND item.identity_key = email_job_alert_occurrences.identity_key
            )
            """
        )
        await db.commit()
        return max(0, int(cursor.rowcount or 0))
