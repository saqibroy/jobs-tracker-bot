from __future__ import annotations

import base64
import asyncio
import hashlib
import json
import os
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

import config
import integrations.gmail_mail as gmail_module
import integrations.zoho_mail as zoho_module
import main
from integrations.job_alerts import AlertParserRegistry, MailMessageMetadata
from integrations.job_alerts.indeed import IndeedAlertParser
from integrations.job_alerts.message import build_bounded_mail_content
from integrations.job_alerts.processing import alert_item_to_job
from models.job import Job
from integrations.gmail_mail import (
    GMAIL_READONLY_SCOPE,
    GmailMailIngestionWorker,
    GmailOAuthClient,
    compose_gmail_query,
    decode_gmail_message,
    gmail_scope_fingerprint,
)
from storage.gmail_mail import (
    cleanup_gmail_messages,
    init_gmail_mail_db,
    save_gmail_checkpoint,
    set_gmail_message_routing,
)
from storage.database import get_pending_delivery_jobs, init_db
from storage.zoho_mail import init_zoho_mail_db
from tests.v2.test_phase6a1_alert_foundation import (
    FakeZohoAPI,
    SyntheticAlertParser,
    message as zoho_message,
)
from tools.inspect_gmail_indeed import build_safe_report

FIXTURES = Path(__file__).parent / "fixtures" / "job_alerts"


@pytest.mark.asyncio
async def test_indeed_inspector_reports_only_bounded_structure() -> None:
    private_marker = "private-recipient@example.invalid"
    tracking_marker = "PERSONALIZED_TRACKING_TOKEN_ABC123"
    body = (FIXTURES / "indeed_alert_sanitized.html").read_text(encoding="utf-8")
    body = body.replace(
        "_wABU1lOVEhFVElDX09QQVFVRV9USVRMRQ",
        tracking_marker,
    ) + f"<p>{private_marker}</p>"
    raw = gmail_message("private-message-id", body)
    raw["payload"]["headers"].append({"name": "To", "value": private_marker})
    api = FakeGmailAPI(pages={}, messages={})
    decoded = await decode_gmail_message(api, raw, "private-message-id")
    report = build_safe_report(raw, decoded)
    serialized = json.dumps(report)
    assert report["cts_indeed_url_count"] == 3
    assert report["known_cta_present"] == {
        "Job anzeigen": True,
        "Passt nicht": True,
        "Hybrides Arbeiten": True,
    }
    assert report["link_host_path_shapes"] == {
        "cts.indeed.com/v3/{encoded}": 3
    }
    assert private_marker not in serialized
    assert tracking_marker not in serialized
    assert "private-message-id" not in serialized


def _b64(value: str | bytes) -> str:
    raw = value.encode() if isinstance(value, str) else value
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def gmail_message(
    message_id: str,
    body: str,
    *,
    sender: str = "Indeed <donotreply@match.indeed.com>",
    subject: str = (
        "Senior Frontend React TypeScript Engineer bei Beispiel Digital GmbH"
    ),
    received_at: datetime | None = None,
) -> dict[str, Any]:
    received_at = received_at or datetime.now(timezone.utc)
    return {
        "id": message_id,
        "labelIds": ["INBOX", "Label_jobs"],
        "internalDate": str(int(received_at.timestamp() * 1000)),
        "snippet": "Job alert recommendations",
        "payload": {
            "mimeType": "text/html",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Mon, 1 Jan 1990 00:00:00 +0000"},
            ],
            "body": {"data": _b64(body), "size": len(body.encode())},
        },
    }


class FakeGmailAPI:
    def __init__(
        self,
        *,
        pages: dict[str | None, dict[str, Any]],
        messages: dict[str, dict[str, Any]],
        attachments: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.pages = pages
        self.messages = messages
        self.attachments = attachments or {}
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self.attachment_calls: list[str] = []
        self.cache_policies: list[bool] = []
        self.closed = False

    async def list_messages(self, **kwargs: Any) -> dict[str, Any]:
        self.list_calls.append(kwargs)
        return self.pages[kwargs["page_token"]]

    async def get_message(self, message_id: str) -> dict[str, Any]:
        self.get_calls.append(message_id)
        return self.messages[message_id]

    async def get_attachment(
        self, message_id: str, attachment_id: str
    ) -> dict[str, Any]:
        self.attachment_calls.append(attachment_id)
        return self.attachments[attachment_id]

    def set_token_cache_write_allowed(
        self, allowed: bool, *, persist_current: bool = False
    ) -> None:
        self.cache_policies.append(allowed)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def gmail_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "gmail.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(path))
    monkeypatch.setattr(gmail_module.config, "DATABASE_PATH", str(path))
    monkeypatch.setattr(config, "ZOHO_INITIAL_SYNC_FROM", "")
    monkeypatch.setattr(config, "ZOHO_COMPANY_DISCOVERY_ENABLED", False)
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL_NGO", "")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")

    async def no_delivery() -> None:
        return None

    monkeypatch.setattr(
        gmail_module, "process_pending_immediate_deliveries", no_delivery
    )
    monkeypatch.setattr(
        zoho_module, "process_pending_immediate_deliveries", no_delivery
    )
    return path


@pytest.mark.asyncio
async def test_paginates_all_pages_without_using_list_order_and_uses_internal_date(
    gmail_db: Path,
) -> None:
    old = datetime.now(timezone.utc) - timedelta(days=30)
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    messages = {
        "old": gmail_message(
            "old", "unrelated", sender="newsletter@example.test", received_at=old
        ),
        "recent": gmail_message(
            "recent", "unrelated", sender="newsletter@example.test", received_at=recent
        ),
    }
    api = FakeGmailAPI(
        pages={
            None: {"messages": [{"id": "old"}], "nextPageToken": "page-2"},
            "page-2": {"messages": [{"id": "recent"}]},
        },
        messages=messages,
    )
    result = await GmailMailIngestionWorker(
        api,
        mailbox_key="personal-alerts",
        label_ids=("Label_jobs",),
        query="from:(indeed.com)",
    ).run(dry_run=False)
    assert result.pages == 2
    assert api.get_calls == ["old", "recent"]
    assert "after:" in api.list_calls[0]["query"]
    assert api.list_calls[1]["page_token"] == "page-2"
    with sqlite3.connect(gmail_db) as db:
        rows = db.execute(
            "SELECT message_id, received_at FROM gmail_mail_messages ORDER BY message_id"
        ).fetchall()
    assert rows[0][1].startswith(old.date().isoformat())
    assert rows[1][1].startswith(recent.date().isoformat())


def test_scope_fingerprint_sorts_labels_but_preserves_exact_query() -> None:
    first = gmail_scope_fingerprint("mailbox", ("B", "A"), "from:x  label:y")
    assert first == gmail_scope_fingerprint("mailbox", ("A", "B"), "from:x  label:y")
    assert first != gmail_scope_fingerprint("mailbox", ("A", "B"), "from:x label:y")
    query = compose_gmail_query(
        "from:x  label:y", datetime(2026, 8, 1, tzinfo=timezone.utc)
    )
    assert query.startswith("after:")
    assert query.endswith("from:x  label:y")


@pytest.mark.asyncio
async def test_scope_change_uses_fresh_14_day_boundary_and_replaces_checkpoint(
    gmail_db: Path,
) -> None:
    await init_gmail_mail_db()
    await save_gmail_checkpoint(
        mailbox_key="mailbox",
        scope_fingerprint="old-scope",
        synced_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    api = FakeGmailAPI(pages={None: {"messages": []}}, messages={})
    before = datetime.now(timezone.utc) - timedelta(days=14, minutes=1)
    result = await GmailMailIngestionWorker(
        api,
        mailbox_key="mailbox",
        label_ids=("Label_jobs",),
        query="from:x",
    ).run(dry_run=False)
    after_epoch = int(api.list_calls[0]["query"].split()[0].removeprefix("after:"))
    assert after_epoch >= int(before.timestamp())
    assert result.scope_changed is True
    assert result.checkpoint_advanced is True


@pytest.mark.asyncio
async def test_strong_dry_run_keeps_absent_database_absent(gmail_db: Path) -> None:
    api = FakeGmailAPI(
        pages={None: {"messages": [{"id": "m"}]}},
        messages={"m": gmail_message("m", "unrelated", sender="news@example.test")},
    )
    result = await GmailMailIngestionWorker(
        api,
        mailbox_key="mailbox",
        label_ids=("Label_jobs",),
        query="from:x",
    ).run(dry_run=True)
    assert result.dry_run is True
    assert not gmail_db.exists()
    assert api.cache_policies == [False]


@pytest.mark.asyncio
async def test_strong_dry_run_keeps_existing_database_sha_and_mtime_unchanged(
    gmail_db: Path,
) -> None:
    await init_gmail_mail_db()
    await init_db()
    original = gmail_db.read_bytes()
    fixed_ns = 1_700_000_000_987_654_321
    os.utime(gmail_db, ns=(fixed_ns, fixed_ns))
    body = (FIXTURES / "indeed_alert_sanitized.html").read_text(encoding="utf-8")
    api = FakeGmailAPI(
        pages={None: {"messages": [{"id": "dry-alert"}]}},
        messages={"dry-alert": gmail_message("dry-alert", body)},
    )
    result = await GmailMailIngestionWorker(
        api,
        mailbox_key="mailbox",
        label_ids=("Label_jobs",),
        query="from:indeed.com",
    ).run(dry_run=True)
    assert result.valid_alert_items == 1
    assert gmail_db.read_bytes() == original
    assert gmail_db.stat().st_mtime_ns == fixed_ns
    assert (
        hashlib.sha256(gmail_db.read_bytes()).digest()
        == hashlib.sha256(original).digest()
    )


@pytest.mark.asyncio
async def test_mime_budget_and_attachment_allowlist() -> None:
    payload = {
        "id": "mime",
        "internalDate": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": "Subject", "value": "jobs"}],
            "parts": [
                {
                    "mimeType": "text/html",
                    "filename": "",
                    "headers": [],
                    "body": {"attachmentId": "body", "size": 20},
                },
                {
                    "mimeType": "image/png",
                    "filename": "",
                    "headers": [],
                    "body": {"attachmentId": "image", "size": 10},
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "cv.pdf",
                    "headers": [],
                    "body": {"attachmentId": "pdf", "size": 10},
                },
                {
                    "mimeType": "text/plain",
                    "filename": "notes.txt",
                    "headers": [],
                    "body": {"attachmentId": "named", "size": 10},
                },
                {
                    "mimeType": "text/plain",
                    "filename": "",
                    "headers": [{"name": "Content-Disposition", "value": "attachment"}],
                    "body": {"attachmentId": "disposition", "size": 10},
                },
                {
                    "mimeType": "text/html",
                    "filename": "",
                    "headers": [],
                    "body": {"attachmentId": "oversized", "size": 600_000},
                },
            ],
        },
    }
    api = FakeGmailAPI(
        pages={},
        messages={},
        attachments={
            "body": {"data": _b64("<a href='https://example.test/job'>job</a>")}
        },
    )
    decoded = await decode_gmail_message(api, payload, "mime")
    assert api.attachment_calls == ["body"]
    assert decoded.external_body_fetches == 1
    assert decoded.content.links == ("https://example.test/job",)


@pytest.mark.asyncio
async def test_nested_alternative_prefers_html_and_never_loads_remote_content() -> None:
    payload = {
        "id": "nested",
        "internalDate": "0",
        "payload": {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "filename": "",
                    "headers": [],
                    "body": {"data": _b64("plain")},
                },
                {
                    "mimeType": "text/html",
                    "filename": "",
                    "headers": [],
                    "body": {
                        "data": _b64(
                            "<img src='https://tracker.test/pixel'><b>html</b>"
                        )
                    },
                },
            ],
        },
    }
    api = FakeGmailAPI(pages={}, messages={})
    decoded = await decode_gmail_message(api, payload, "nested")
    assert "html" in decoded.content.cleaned_text
    assert "plain" not in decoded.content.cleaned_text
    assert "tracker.test" not in decoded.content.sanitized_html
    assert api.attachment_calls == []


@pytest.mark.asyncio
async def test_oauth_dry_refresh_does_not_mutate_existing_cache(tmp_path: Path) -> None:
    client_file = tmp_path / "client.json"
    token_file = tmp_path / "token.json"
    client_file.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "cid",
                    "client_secret": "secret",
                    "token_uri": "https://oauth.test/token",
                }
            }
        )
    )
    original = json.dumps(
        {
            "refresh_token": "refresh",
            "access_token": "old",
            "expires_at": 0,
            "scope": GMAIL_READONLY_SCOPE,
        }
    ).encode()
    token_file.write_bytes(original)
    fixed_ns = 1_700_000_000_123_456_789
    os.utime(token_file, ns=(fixed_ns, fixed_ns))
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "fresh",
                    "expires_in": 3600,
                    "scope": GMAIL_READONLY_SCOPE,
                },
            )
        return httpx.Response(200, json={"messages": []})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = GmailOAuthClient(
        http,
        client_file=str(client_file),
        token_file=str(token_file),
        api_base="https://gmail.test",
    )
    client.set_token_cache_write_allowed(False)
    await client.list_messages(
        label_ids=("INBOX",), query="after:1", page_token=None, max_results=10
    )
    await http.aclose()
    assert token_file.read_bytes() == original
    assert token_file.stat().st_mtime_ns == fixed_ns
    assert (
        hashlib.sha256(token_file.read_bytes()).digest()
        == hashlib.sha256(original).digest()
    )
    assert requests == [("POST", "/token"), ("GET", "/users/me/messages")]


@pytest.mark.asyncio
async def test_normal_oauth_refresh_persists_atomically_with_mode_0600(
    tmp_path: Path,
) -> None:
    client_file = tmp_path / "client.json"
    token_file = tmp_path / "token.json"
    client_file.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "cid",
                    "client_secret": "secret",
                    "token_uri": "https://oauth.test/token",
                }
            }
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "fresh",
                    "expires_in": 3600,
                    "scope": GMAIL_READONLY_SCOPE,
                },
            )
        return httpx.Response(200, json={"messages": []})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = GmailOAuthClient(
        http,
        client_file=str(client_file),
        token_file=str(token_file),
        refresh_token="refresh",
        api_base="https://gmail.test",
    )
    client.set_token_cache_write_allowed(True)
    await client.list_messages(
        label_ids=("INBOX",), query="after:1", page_token=None, max_results=10
    )
    await http.aclose()
    saved = json.loads(token_file.read_text())
    assert saved["access_token"] == "fresh"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".token.json.*"))


@pytest.mark.asyncio
async def test_occurrence_backfill_is_idempotent(gmail_db: Path) -> None:
    await init_zoho_mail_db()
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(gmail_db) as db:
        db.execute(
            """
            INSERT INTO email_job_alert_items
                (provider, identity_key, content_hash, account_id, message_id,
                 title, company, location, first_seen_at, last_seen_at)
            VALUES ('indeed', 'id-1', 'hash', 'zoho-account', 'zoho-message',
                    'Title', 'Company', 'Berlin', ?, ?)
            """,
            (now, now),
        )
        db.commit()
    await init_gmail_mail_db()
    await init_gmail_mail_db()
    with sqlite3.connect(gmail_db) as db:
        assert db.execute(
            """
            SELECT transport, mailbox_key, message_id
            FROM email_job_alert_occurrences
            """
        ).fetchall() == [("zoho", "zoho-account", "zoho-message")]


@pytest.mark.asyncio
async def test_gmail_cleanup_and_processing_reason_are_bounded(gmail_db: Path) -> None:
    await init_gmail_mail_db()
    now = datetime.now(timezone.utc).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
    with sqlite3.connect(gmail_db) as db:
        db.execute(
            """
            INSERT INTO gmail_mail_messages
                (mailbox_key, message_id, first_seen_at, last_seen_at, processed)
            VALUES ('mailbox', 'old', ?, ?, 1)
            """,
            (now, old),
        )
        db.commit()
    await set_gmail_message_routing(
        mailbox_key="mailbox",
        message_id="old",
        intent="unknown_job_email",
        provider="",
        result="handled",
        reason="sensitive-looking-but-synthetic " * 100,
        dry_run=False,
    )
    with sqlite3.connect(gmail_db) as db:
        reason = db.execute(
            "SELECT processing_reason FROM gmail_mail_messages"
        ).fetchone()[0]
    assert len(reason) <= 160
    assert await cleanup_gmail_messages() == 1


class FakeScheduler:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str, dict[str, Any]]] = []

    def add_job(self, func: object, trigger: str, **kwargs: Any) -> None:
        self.calls.append((func, trigger, kwargs))


def test_gmail_scheduler_is_independent_stable_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "GMAIL_MAIL_SYNC_ENABLED", True)
    scheduler = FakeScheduler()
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    main._register_gmail_mail_job(scheduler, now=now)  # type: ignore[arg-type]
    assert len(scheduler.calls) == 1
    _, trigger, options = scheduler.calls[0]
    assert trigger == "interval"
    assert options["id"] == "gmail_mail_sync"
    assert options["max_instances"] == 1
    assert options["coalesce"] is True
    assert options["next_run_time"] == now + timedelta(minutes=3)


@pytest.mark.asyncio
async def test_fetch_failure_leaves_established_checkpoint_unchanged(
    gmail_db: Path,
) -> None:
    await init_gmail_mail_db()
    fingerprint = gmail_scope_fingerprint("mailbox", ("Label_jobs",), "from:x")
    previous = datetime(2026, 8, 10, tzinfo=timezone.utc)
    await save_gmail_checkpoint(
        mailbox_key="mailbox",
        scope_fingerprint=fingerprint,
        synced_at=previous,
    )

    class FailingAPI(FakeGmailAPI):
        async def get_message(self, message_id: str) -> dict[str, Any]:
            raise RuntimeError("mock fetch failed")

    api = FailingAPI(
        pages={None: {"messages": [{"id": "broken"}]}},
        messages={},
    )
    with pytest.raises(RuntimeError, match="mock fetch failed"):
        await GmailMailIngestionWorker(
            api,
            mailbox_key="mailbox",
            label_ids=("Label_jobs",),
            query="from:x",
        ).run(dry_run=False)
    expected_boundary = previous - timedelta(hours=48)
    actual_epoch = int(api.list_calls[0]["query"].split()[0].removeprefix("after:"))
    assert actual_epoch == int(expected_boundary.timestamp())
    assert api.closed is True
    with sqlite3.connect(gmail_db) as db:
        stored = db.execute(
            "SELECT last_successful_sync_at FROM gmail_mail_sync_state"
        ).fetchone()[0]
    assert stored == previous.isoformat()


@pytest.mark.asyncio
async def test_pipeline_failure_keeps_item_message_and_checkpoint_pending(
    gmail_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = SyntheticAlertParser()

    async def fail_pipeline(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("mock pipeline failed")

    monkeypatch.setattr(gmail_module, "process_discovered_jobs", fail_pipeline)
    api = FakeGmailAPI(
        pages={None: {"messages": [{"id": "pipeline"}]}},
        messages={
            "pipeline": gmail_message(
                "pipeline",
                "cards",
                sender="alerts@example.invalid",
                subject="Synthetic alert: job recommendations",
            )
        },
    )
    with pytest.raises(RuntimeError, match="mock pipeline failed"):
        await GmailMailIngestionWorker(
            api,
            mailbox_key="mailbox",
            label_ids=("Label_jobs",),
            query="subject:synthetic",
            parser_registry=AlertParserRegistry((parser,)),
        ).run(dry_run=False)
    with sqlite3.connect(gmail_db) as db:
        assert (
            db.execute("SELECT state FROM email_job_alert_items").fetchone()[0]
            == "pending"
        )
        assert (
            db.execute("SELECT processed FROM gmail_mail_messages").fetchone()[0] == 0
        )
        assert (
            db.execute("SELECT COUNT(*) FROM gmail_mail_sync_state").fetchone()[0] == 0
        )


@pytest.mark.asyncio
async def test_backlog_processes_bounded_batch_but_blocks_message_and_checkpoint(
    gmail_db: Path,
) -> None:
    parser = SyntheticAlertParser(item_count=3)
    api = FakeGmailAPI(
        pages={None: {"messages": [{"id": "many"}]}},
        messages={
            "many": gmail_message(
                "many",
                "cards",
                sender="alerts@example.invalid",
                subject="Synthetic alert: job recommendations",
            )
        },
    )
    result = await GmailMailIngestionWorker(
        api,
        mailbox_key="mailbox",
        label_ids=("Label_jobs",),
        query="subject:synthetic",
        max_alert_items=2,
        parser_registry=AlertParserRegistry((parser,)),
    ).run(dry_run=False)
    assert result.backlog_deferred == 1
    assert result.pending_alert_items == 2
    assert result.checkpoint_advanced is False
    with sqlite3.connect(gmail_db) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
        assert (
            db.execute("SELECT processed FROM gmail_mail_messages").fetchone()[0] == 0
        )
        assert (
            db.execute("SELECT COUNT(*) FROM gmail_mail_sync_state").fetchone()[0] == 0
        )


@pytest.mark.asyncio
async def test_current_version_rerun_skips_and_invalid_internal_date_is_safe(
    gmail_db: Path,
) -> None:
    body = (FIXTURES / "indeed_alert_sanitized.html").read_text(encoding="utf-8")
    worker_args = dict(
        mailbox_key="mailbox",
        label_ids=("Label_jobs",),
        query="from:indeed.com",
    )
    first_api = FakeGmailAPI(
        pages={None: {"messages": [{"id": "current"}]}},
        messages={"current": gmail_message("current", body)},
    )
    await GmailMailIngestionWorker(first_api, **worker_args).run(dry_run=False)
    second_api = FakeGmailAPI(
        pages={None: {"messages": [{"id": "current"}, {"id": "missing-date"}]}},
        messages={
            "current": gmail_message("current", body),
            "missing-date": {
                **gmail_message("missing-date", body),
                "internalDate": "invalid",
            },
        },
    )
    second = await GmailMailIngestionWorker(second_api, **worker_args).run(
        dry_run=False
    )
    assert second.current_version_skipped == 1
    assert second.checkpoint_advanced is True
    with sqlite3.connect(gmail_db) as db:
        missing = db.execute(
            """
            SELECT received_at, processed, processing_result, processing_reason
            FROM gmail_mail_messages WHERE message_id = 'missing-date'
            """
        ).fetchone()
        assert missing == (
            None,
            1,
            "alert_skipped",
            "alert_missing_reliable_message_date",
        )
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_zoho_pending_item_replayed_by_gmail_is_one_job_and_obligation(
    gmail_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await init_gmail_mail_db()
    await init_db()
    indeed_html = (FIXTURES / "indeed_alert_sanitized.html").read_text(encoding="utf-8")
    real_pipeline = zoho_module.process_discovered_jobs

    async def fail_pipeline(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("pipeline paused")

    monkeypatch.setattr(zoho_module, "process_discovered_jobs", fail_pipeline)
    zoho_api = FakeZohoAPI(
        [
            zoho_message(
                "zoho-copy",
                subject=(
                    "Senior Frontend React TypeScript Engineer "
                    "bei Beispiel Digital GmbH"
                ),
                sender="Indeed <donotreply@match.indeed.com>",
            )
        ],
        contents={"zoho-copy": indeed_html},
    )
    with pytest.raises(RuntimeError, match="pipeline paused"):
        await zoho_module.ZohoMailIngestionWorker(zoho_api).run(dry_run=False)
    monkeypatch.setattr(zoho_module, "process_discovered_jobs", real_pipeline)

    gmail_api = FakeGmailAPI(
        pages={None: {"messages": [{"id": "gmail-copy"}]}},
        messages={"gmail-copy": gmail_message("gmail-copy", indeed_html)},
    )
    result = await GmailMailIngestionWorker(
        gmail_api,
        mailbox_key="gmail-personal",
        label_ids=("Label_jobs",),
        query="from:indeed.com",
    ).run(dry_run=False)
    assert result.processed_alert_items == 1
    with sqlite3.connect(gmail_db) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM email_job_alert_items").fetchone()[0] == 1
        )
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert (
            db.execute("SELECT COUNT(*) FROM job_delivery_receipts").fetchone()[0] == 0
        )
        occurrences = db.execute(
            """
            SELECT transport, mailbox_key, message_id
            FROM email_job_alert_occurrences ORDER BY transport
            """
        ).fetchall()
        assert occurrences == [
            ("gmail", "gmail-personal", "gmail-copy"),
            ("zoho", "acct1", "zoho-copy"),
        ]
    obligations = await get_pending_delivery_jobs(
        "immediate",
        "discord_general",
        limit=10,
        max_age_days=30,
        ngo_webhook_configured=False,
    )
    assert len(obligations) == 1


@pytest.mark.asyncio
async def test_large_inline_body_obeys_one_aggregate_decoded_budget() -> None:
    oversized = "x" * 600_000
    payload = gmail_message("large", oversized)
    api = FakeGmailAPI(pages={}, messages={})
    decoded = await decode_gmail_message(api, payload, "large")
    assert len(decoded.content.cleaned_text.encode()) <= 512 * 1024
    assert decoded.content.truncated is True


@pytest.mark.asyncio
async def test_client_uses_no_gmail_mutation_endpoint(tmp_path: Path) -> None:
    token_file = tmp_path / "token.json"
    token_file.write_text(
        json.dumps(
            {
                "access_token": "valid",
                "expires_at": datetime.now(timezone.utc).timestamp() + 3600,
                "scope": GMAIL_READONLY_SCOPE,
            }
        )
    )
    original = token_file.read_bytes()
    fixed_ns = 1_700_000_000_111_222_333
    os.utime(token_file, ns=(fixed_ns, fixed_ns))
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        assert request.method == "GET"
        assert not any(
            mutation in request.url.path
            for mutation in ("/modify", "/trash", "/untrash", "/send", "/drafts")
        )
        if request.url.path.endswith("/attachments/body"):
            return httpx.Response(200, json={"data": _b64("body")})
        if request.url.path.endswith("/messages/id"):
            return httpx.Response(200, json={"id": "id"})
        return httpx.Response(200, json={"messages": []})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = GmailOAuthClient(
        http,
        client_file=str(tmp_path / "unused.json"),
        token_file=str(token_file),
        api_base="https://gmail.test",
    )
    await client.list_messages(
        label_ids=("INBOX",), query="after:1", page_token=None, max_results=10
    )
    await client.get_message("id")
    await client.get_attachment("id", "body")
    await http.aclose()
    assert len(requests) == 3
    assert token_file.read_bytes() == original
    assert token_file.stat().st_mtime_ns == fixed_ns


@pytest.mark.asyncio
async def test_absent_oauth_cache_refreshes_in_memory_and_stays_absent(
    tmp_path: Path,
) -> None:
    client_file = tmp_path / "client.json"
    token_file = tmp_path / "absent-token.json"
    client_file.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "cid",
                    "client_secret": "secret",
                    "token_uri": "https://oauth.test/token",
                }
            }
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "fresh",
                    "expires_in": 3600,
                    "scope": GMAIL_READONLY_SCOPE,
                },
            )
        return httpx.Response(200, json={"messages": []})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = GmailOAuthClient(
        http,
        client_file=str(client_file),
        token_file=str(token_file),
        refresh_token="configured-refresh",
        api_base="https://gmail.test",
    )
    client.set_token_cache_write_allowed(False)
    await client.list_messages(
        label_ids=("INBOX",), query="after:1", page_token=None, max_results=10
    )
    await http.aclose()
    assert not token_file.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_scope",
    [
        "",
        "https://www.googleapis.com/auth/gmail.modify",
        f"{GMAIL_READONLY_SCOPE} https://www.googleapis.com/auth/gmail.modify",
    ],
)
async def test_invalid_oauth_scope_fails_with_bounded_diagnostic(
    tmp_path: Path,
    invalid_scope: str,
) -> None:
    token_file = tmp_path / "token.json"
    token_file.write_text(
        json.dumps(
            {
                "access_token": "do-not-log",
                "expires_at": datetime.now(timezone.utc).timestamp() + 3600,
                "scope": invalid_scope,
            }
        )
    )
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500))
    )
    client = GmailOAuthClient(
        http,
        client_file=str(tmp_path / "unused.json"),
        token_file=str(token_file),
        api_base="https://gmail.test",
    )
    with pytest.raises(gmail_module.GmailError) as failure:
        await client.list_messages(
            label_ids=("INBOX",), query="after:1", page_token=None, max_results=10
        )
    await http.aclose()
    assert str(failure.value) == "gmail_oauth_token_scope_invalid"
    assert "do-not-log" not in str(failure.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("different_url", [False, True])
async def test_repeated_gmail_alert_and_direct_source_duplicate_are_one_job(
    gmail_db: Path,
    different_url: bool,
) -> None:
    indeed_html = (FIXTURES / "indeed_alert_sanitized.html").read_text(encoding="utf-8")
    api = FakeGmailAPI(
        pages={None: {"messages": [{"id": "copy-1"}, {"id": "copy-2"}]}},
        messages={
            "copy-1": gmail_message("copy-1", indeed_html),
            "copy-2": gmail_message("copy-2", indeed_html),
        },
    )
    result = await GmailMailIngestionWorker(
        api,
        mailbox_key="mailbox",
        label_ids=("Label_jobs",),
        query="from:indeed.com",
    ).run(dry_run=False)
    assert result.valid_alert_items == 2
    parsed = IndeedAlertParser().parse(
        MailMessageMetadata(
            "direct",
            "direct",
            subject=(
                "Senior Frontend React TypeScript Engineer "
                "bei Beispiel Digital GmbH"
            ),
            sender="Indeed <donotreply@match.indeed.com>",
            message_date=datetime.now(timezone.utc),
        ),
        build_bounded_mail_content(indeed_html),
    )
    direct_job = alert_item_to_job(parsed.items[0])
    if different_url:
        payload = direct_job.model_dump()
        payload.update(
            url="https://direct-source.invalid/different-url",
            source="direct_source",
            id="",
            content_hash="",
        )
        direct_job = Job(**payload)
    from job_ingestion import (
        JobIngestionCandidate,
        JobIngestionStatus,
        process_discovered_jobs,
    )

    direct = await process_discovered_jobs(
        [JobIngestionCandidate("direct-source", direct_job)],
        persist=True,
        associate_items=True,
    )
    assert direct.item_results[0].status == JobIngestionStatus.DUPLICATE
    with sqlite3.connect(gmail_db) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM email_job_alert_items").fetchone()[0] == 1
        )
        assert (
            db.execute("SELECT COUNT(*) FROM email_job_alert_occurrences").fetchone()[0]
            == 2
        )
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_cancellation_closes_client_and_releases_sync_overlap_guard(
    gmail_db: Path,
) -> None:
    class CancelledAPI(FakeGmailAPI):
        async def list_messages(self, **kwargs: Any) -> dict[str, Any]:
            raise asyncio.CancelledError

    cancelled = CancelledAPI(pages={}, messages={})
    with pytest.raises(asyncio.CancelledError):
        await GmailMailIngestionWorker(
            cancelled,
            mailbox_key="mailbox",
            label_ids=("Label_jobs",),
            query="from:x",
        ).run(dry_run=True)
    assert cancelled.closed is True
    succeeding = FakeGmailAPI(pages={None: {"messages": []}}, messages={})
    result = await GmailMailIngestionWorker(
        succeeding,
        mailbox_key="mailbox",
        label_ids=("Label_jobs",),
        query="from:x",
    ).run(dry_run=True)
    assert result.pages == 1
