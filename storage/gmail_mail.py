"""Focused SQLite state for the read-only Gmail alert transport."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

import config
from integrations.job_alerts.contracts import MAX_ISSUE_LENGTH, bounded_text
from storage.zoho_mail import MessageProcessingState, init_zoho_mail_db

_CREATE_MESSAGES = """
CREATE TABLE IF NOT EXISTS gmail_mail_messages (
    mailbox_key TEXT NOT NULL,
    message_id TEXT NOT NULL,
    label_ids TEXT NOT NULL DEFAULT '[]',
    subject TEXT,
    sender TEXT,
    snippet TEXT,
    received_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    likely_job INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0,
    processing_version INTEGER NOT NULL DEFAULT 0,
    mail_intent TEXT,
    alert_provider TEXT,
    processing_result TEXT,
    processing_reason TEXT,
    PRIMARY KEY (mailbox_key, message_id)
);
"""

_CREATE_SYNC_STATE = """
CREATE TABLE IF NOT EXISTS gmail_mail_sync_state (
    mailbox_key TEXT PRIMARY KEY,
    scope_fingerprint TEXT NOT NULL,
    last_successful_sync_at TEXT,
    update_metadata TEXT,
    updated_at TEXT NOT NULL
);
"""

_CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_gmail_messages_processed_seen "
    "ON gmail_mail_messages(processed, last_seen_at);",
)


@dataclass(frozen=True, slots=True)
class GmailSyncState:
    scope_fingerprint: str = ""
    last_successful_sync_at: datetime | None = None


async def init_gmail_mail_db() -> None:
    # This also performs the idempotent Phase 6A1/A2 occurrence backfill and
    # creates the shared alert-item tables. Gmail IDs are never written to a
    # Zoho table.
    await init_zoho_mail_db()
    path = Path(config.DATABASE_PATH)
    async with aiosqlite.connect(path) as db:
        await db.execute(_CREATE_MESSAGES)
        await db.execute(_CREATE_SYNC_STATE)
        for statement in _CREATE_INDEXES:
            await db.execute(statement)
        await db.commit()


async def get_gmail_sync_state(mailbox_key: str) -> GmailSyncState:
    path = Path(config.DATABASE_PATH)
    if not path.is_file():
        return GmailSyncState()
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        async with aiosqlite.connect(uri, uri=True) as db:
            cursor = await db.execute(
                """
                SELECT scope_fingerprint, last_successful_sync_at
                FROM gmail_mail_sync_state WHERE mailbox_key = ?
                """,
                (mailbox_key,),
            )
            row = await cursor.fetchone()
    except Exception:
        return GmailSyncState()
    if not row:
        return GmailSyncState()
    checkpoint = _parse_datetime(row[1])
    return GmailSyncState(str(row[0] or ""), checkpoint)


async def save_gmail_checkpoint(
    *,
    mailbox_key: str,
    scope_fingerprint: str,
    synced_at: datetime,
    metadata: dict[str, object] | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    bounded_metadata = json.dumps(
        metadata or {}, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )[:500]
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute(
            """
            INSERT INTO gmail_mail_sync_state
                (mailbox_key, scope_fingerprint, last_successful_sync_at,
                 update_metadata, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(mailbox_key) DO UPDATE SET
                scope_fingerprint = excluded.scope_fingerprint,
                last_successful_sync_at = excluded.last_successful_sync_at,
                update_metadata = excluded.update_metadata,
                updated_at = excluded.updated_at
            """,
            (
                bounded_text(mailbox_key, 200),
                bounded_text(scope_fingerprint, 128),
                synced_at.astimezone(timezone.utc).isoformat(),
                bounded_metadata,
                now,
            ),
        )
        await db.commit()


async def upsert_gmail_message(
    *,
    mailbox_key: str,
    message_id: str,
    label_ids: tuple[str, ...],
    subject: str,
    sender: str,
    snippet: str,
    received_at: datetime | None,
    likely_job: bool,
    dry_run: bool,
) -> bool:
    if dry_run:
        return True
    now = datetime.now(timezone.utc).isoformat()
    labels = json.dumps(
        [bounded_text(label, 100) for label in label_ids[:50]],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM gmail_mail_messages WHERE mailbox_key = ? AND message_id = ?",
            (mailbox_key, message_id),
        )
        first_seen = await cursor.fetchone() is None
        await db.execute(
            """
            INSERT INTO gmail_mail_messages
                (mailbox_key, message_id, label_ids, subject, sender, snippet,
                 received_at, first_seen_at, last_seen_at, likely_job)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mailbox_key, message_id) DO UPDATE SET
                label_ids = excluded.label_ids,
                subject = excluded.subject,
                sender = excluded.sender,
                snippet = excluded.snippet,
                received_at = excluded.received_at,
                last_seen_at = excluded.last_seen_at,
                likely_job = MAX(gmail_mail_messages.likely_job, excluded.likely_job)
            """,
            (
                bounded_text(mailbox_key, 200),
                bounded_text(message_id, 200),
                labels,
                bounded_text(subject, 500),
                bounded_text(sender, 320),
                bounded_text(snippet, 1_000),
                received_at.isoformat() if received_at else None,
                now,
                now,
                int(likely_job),
            ),
        )
        await db.commit()
    return first_seen


async def get_gmail_message_state(
    *, mailbox_key: str, message_id: str
) -> MessageProcessingState:
    path = Path(config.DATABASE_PATH)
    if not path.is_file():
        return MessageProcessingState()
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        async with aiosqlite.connect(uri, uri=True) as db:
            cursor = await db.execute(
                """
                SELECT processed, processing_version, mail_intent,
                       alert_provider, processing_result
                FROM gmail_mail_messages
                WHERE mailbox_key = ? AND message_id = ?
                """,
                (mailbox_key, message_id),
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


async def set_gmail_message_routing(
    *,
    mailbox_key: str,
    message_id: str,
    intent: str,
    provider: str,
    result: str,
    reason: str,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute(
            """
            UPDATE gmail_mail_messages
            SET mail_intent = ?, alert_provider = ?, processing_result = ?,
                processing_reason = ?
            WHERE mailbox_key = ? AND message_id = ?
            """,
            (
                bounded_text(intent, 80),
                bounded_text(provider, 80),
                bounded_text(result, 80),
                bounded_text(reason, MAX_ISSUE_LENGTH),
                mailbox_key,
                message_id,
            ),
        )
        await db.commit()


async def mark_gmail_message_processed(
    *,
    mailbox_key: str,
    message_id: str,
    intent: str,
    provider: str,
    result: str,
    reason: str,
    dry_run: bool,
    processing_version: int = 1,
) -> None:
    if dry_run:
        return
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute(
            """
            UPDATE gmail_mail_messages
            SET processed = 1, processing_version = ?, mail_intent = ?,
                alert_provider = ?, processing_result = ?, processing_reason = ?
            WHERE mailbox_key = ? AND message_id = ?
            """,
            (
                max(0, processing_version),
                bounded_text(intent, 80),
                bounded_text(provider, 80),
                bounded_text(result, 80),
                bounded_text(reason, MAX_ISSUE_LENGTH),
                mailbox_key,
                message_id,
            ),
        )
        await db.commit()


async def cleanup_gmail_messages(*, older_than_days: int = 90) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, older_than_days))
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            DELETE FROM gmail_mail_messages
            WHERE processed = 1 AND last_seen_at < ?
            """,
            (cutoff.isoformat(),),
        )
        await db.commit()
        return max(0, int(cursor.rowcount or 0))


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
