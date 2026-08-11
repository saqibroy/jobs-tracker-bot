from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import quote

import httpx
import pytest

import config
import integrations.zoho_mail as zoho_module
from integrations.job_alerts import (
    AlertMatch,
    AlertParseStatus,
    AlertParserRegistry,
    JobAlertItem,
    JobAlertParseResult,
    MailIntent,
    MailMessageMetadata,
)
from integrations.job_alerts.contracts import BoundedMailContent
from integrations.job_alerts.message import build_bounded_mail_content
from integrations.job_alerts.processing import alert_item_to_job
from integrations.job_alerts.routing import route_mail_intent
from integrations.job_alerts.urls import alert_identity_key, normalize_alert_url
from integrations.zoho_mail import (
    ZohoAccount,
    ZohoFolder,
    ZohoMailIngestionWorker,
    ZohoMessageSummary,
    ZohoOAuthMailClient,
    infer_status,
)
from job_ingestion import (
    JobIngestionCandidate,
    JobIngestionStatus,
    process_discovered_jobs,
)
from storage.database import init_db
from storage.zoho_mail import (
    cleanup_processed_alert_items,
    get_last_successful_sync_at,
    init_zoho_mail_db,
)


class FakeZohoAPI:
    def __init__(
        self,
        messages: list[ZohoMessageSummary],
        *,
        contents: dict[str, str] | None = None,
        folders: list[ZohoFolder] | None = None,
    ) -> None:
        self.api_domain = "https://www.zohoapis.eu"
        self.mail_api_base = "https://mail.zoho.eu"
        self.messages = messages
        self.contents = contents or {}
        self.folders = folders or [ZohoFolder("inbox", "Inbox")]
        self.content_calls: list[str] = []
        self.closed = False

    async def list_accounts(self) -> list[ZohoAccount]:
        return [ZohoAccount("acct1", "private@example.invalid")]

    async def list_folders(self, account_id: str) -> list[ZohoFolder]:
        return self.folders

    async def list_messages(
        self,
        account_id: str,
        folder_id: str,
        *,
        start: int,
        limit: int,
    ) -> list[ZohoMessageSummary]:
        if start != 1:
            return []
        return [message for message in self.messages if message.folder_id == folder_id]

    async def get_message_content(
        self,
        account_id: str,
        folder_id: str,
        message_id: str,
    ) -> str:
        self.content_calls.append(message_id)
        return self.contents.get(message_id, "")

    async def close(self) -> None:
        self.closed = True


@dataclass
class SyntheticAlertParser:
    provider: str = "synthetic_alert"
    item_count: int = 1
    invalid_count: int = 0
    parse_error: bool = False
    company: str = "Example GmbH"

    def matches(
        self,
        message: MailMessageMetadata,
        content: BoundedMailContent,
    ) -> AlertMatch:
        matched = "synthetic alert" in message.subject.lower()
        return AlertMatch(
            self.provider,
            matched,
            confidence=95 if matched else 0,
            evidence=("synthetic_fixture_match",),
        )

    def parse(
        self,
        message: MailMessageMetadata,
        content: BoundedMailContent,
    ) -> JobAlertParseResult:
        if self.parse_error:
            raise ValueError("synthetic parser failed")
        items = tuple(
            JobAlertItem(
                provider=self.provider,
                provider_item_id=f"{message.message_id}-item-{index}",
                title=f"Senior Frontend React Developer {message.message_id} {index}",
                company=self.company,
                location="Remote - Germany",
                canonical_url=(
                    f"https://jobs.example.invalid/{message.message_id}/{index}"
                    "?utm_source=mail"
                ),
                account_id=message.account_id,
                message_id=message.message_id,
                workplace_type="remote",
                is_remote=True,
                remote_scope="germany",
                summary="React TypeScript frontend product development",
                evidence=("synthetic_card",),
            )
            for index in range(self.item_count)
        )
        return JobAlertParseResult(
            provider=self.provider,
            status=AlertParseStatus.PARSED if items else AlertParseStatus.NO_ITEMS,
            items=items,
            invalid_count=self.invalid_count,
            issues=("synthetic_invalid_card",) if self.invalid_count else (),
        )


def message(
    message_id: str,
    *,
    subject: str = "Synthetic alert: job recommendations",
    sender: str = "alerts@example.invalid",
    days_ago: int = 0,
    date: datetime | None | object = ...,
    folder_id: str = "inbox",
) -> ZohoMessageSummary:
    resolved_date = (
        datetime.now(timezone.utc) - timedelta(days=days_ago)
        if date is ...
        else date
    )
    return ZohoMessageSummary(
        message_id=message_id,
        folder_id=folder_id,
        folder_name="Inbox",
        subject=subject,
        sender=sender,
        message_date=resolved_date,
    )


@pytest.fixture
async def phase6_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "phase6.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_path))
    monkeypatch.setattr(config, "ZOHO_INITIAL_SYNC_FROM", "")
    monkeypatch.setattr(config, "ZOHO_COMPANY_DISCOVERY_ENABLED", False)
    monkeypatch.setattr(
        config,
        "ZOHO_DISCOVERY_SEED_FILE",
        str(tmp_path / "discovery.txt"),
    )
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL_NGO", "")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(
        zoho_module,
        "process_pending_immediate_deliveries",
        AsyncMock(),
    )
    await init_zoho_mail_db()
    await init_db()
    return db_path


def registry(parser: SyntheticAlertParser | None = None) -> AlertParserRegistry:
    return AlertParserRegistry((parser or SyntheticAlertParser(),))


def test_production_registry_contains_phase6a2_providers() -> None:
    assert tuple(parser.provider for parser in AlertParserRegistry().parsers) == (
        "linkedin",
        "indeed",
    )
    assert Path("integrations/job_alerts/linkedin.py").is_file()
    assert Path("integrations/job_alerts/indeed.py").is_file()


def test_routing_precedence_and_all_three_intents() -> None:
    parser_registry = registry()
    alert_meta = MailMessageMetadata("a", "1", subject="Synthetic alert: jobs")
    content = build_bounded_mail_content("A recruiter found your profile")
    alert = route_mail_intent(alert_meta, content, parser_registry)
    assert alert.intent == MailIntent.JOB_ALERT

    lifecycle = route_mail_intent(
        MailMessageMetadata(
            "a",
            "2",
            subject="Synthetic alert — application received",
        ),
        content,
        parser_registry,
    )
    assert lifecycle.intent == MailIntent.APPLICATION_OR_RECRUITMENT

    recruiter = route_mail_intent(
        MailMessageMetadata("a", "3", subject="A recruiter found your profile"),
        build_bounded_mail_content("Would you like to discuss this opportunity?"),
        AlertParserRegistry(),
    )
    assert recruiter.intent == MailIntent.APPLICATION_OR_RECRUITMENT

    unknown = route_mail_intent(
        MailMessageMetadata("a", "4", subject="Five new jobs for you"),
        build_bounded_mail_content("Roles and career opportunities"),
        AlertParserRegistry(),
    )
    assert unknown.intent == MailIntent.UNKNOWN_JOB_EMAIL
    assert len(unknown.evidence) <= 8
    assert all(len(value) <= 120 for value in unknown.evidence)

    generic_unfortunate = route_mail_intent(
        MailMessageMetadata("a", "5", subject="Job alert update"),
        build_bounded_mail_content(
            "Unfortunately, this job opportunity is no longer available."
        ),
        AlertParserRegistry(),
    )
    assert generic_unfortunate.intent == MailIntent.UNKNOWN_JOB_EMAIL


@pytest.mark.parametrize(
    ("subject", "body", "expected_status"),
    [
        ("Application received", "We received your application", "applied"),
        ("Update on your application", "Unfortunately we will not move forward", "rejected"),
        ("Application update", "Thank you for your interest. Unfortunately your application was not a match.", "rejected"),
        ("Your interview", "We invite you to an interview", "interview"),
        ("Screening call", "Your screening call is scheduled", "interview"),
        ("Your job offer", "We are pleased to offer you this role", "offer"),
        ("Recruiter outreach", "A recruiter found your profile for an opportunity", "recruiter_outreach"),
    ],
)
def test_application_lifecycle_and_recruiter_regression_matrix(
    subject: str,
    body: str,
    expected_status: str,
) -> None:
    decision = route_mail_intent(
        MailMessageMetadata("a", "m", subject=subject),
        build_bounded_mail_content(body),
        AlertParserRegistry(),
    )
    assert decision.intent == MailIntent.APPLICATION_OR_RECRUITMENT
    assert infer_status(subject, body) == expected_status


def test_content_url_and_identity_bounds() -> None:
    html = "".join(
        f'<a href="https://example.invalid/jobs/{index}?utm_source=x">job</a>'
        for index in range(250)
    ) + '<script>https://scripts.invalid/must-not-be-a-link</script>' + "x" * (512 * 1024)
    bounded = build_bounded_mail_content(html)
    assert bounded.truncated is True
    assert len(bounded.sanitized_html.encode()) <= 512 * 1024
    assert len(bounded.cleaned_text.encode()) <= 512 * 1024
    assert len(bounded.links) <= 200
    assert all("scripts.invalid" not in link for link in bounded.links)
    assert len(build_bounded_mail_content(html, link_limit=7).links) == 7

    assert normalize_alert_url("javascript:alert(1)") is None
    assert normalize_alert_url("data:text/plain,no") is None
    assert normalize_alert_url("file:///tmp/no") is None
    assert normalize_alert_url("https://user:pass@example.invalid/job") is None
    assert normalize_alert_url("/hostless") is None
    assert (
        normalize_alert_url(
            "https://wrap.invalid/go?target=https%3A%2F%2Fjobs.invalid%2F1%3Futm_source%3Dx",
            wrapper_hosts=("wrap.invalid",),
            wrapper_query_params=("target",),
        )
        == "https://jobs.invalid/1"
    )
    assert (
        normalize_alert_url(
            "https://unapproved.invalid/go?target=https%3A%2F%2Fjobs.invalid%2F1",
            wrapper_hosts=("wrap.invalid",),
            wrapper_query_params=("target",),
        )
        == "https://unapproved.invalid/go?target=https%3A%2F%2Fjobs.invalid%2F1"
    )
    wrapped = "https://jobs.invalid/final"
    for _ in range(3):
        wrapped = f"https://wrap.invalid/go?target={quote(wrapped, safe='')}"
    assert normalize_alert_url(
        wrapped,
        wrapper_hosts=("wrap.invalid",),
        wrapper_query_params=("target",),
    ) == "https://jobs.invalid/final"
    wrapped = f"https://wrap.invalid/go?target={quote(wrapped, safe='')}"
    assert normalize_alert_url(
        wrapped,
        wrapper_hosts=("wrap.invalid",),
        wrapper_query_params=("target",),
    ) is None
    native, content_hash = alert_identity_key(
        provider_item_id="123",
        canonical_url="https://jobs.invalid/1",
        title="Frontend Developer",
        company="Example",
        location="Germany",
    )
    assert native == "id:123"
    assert len(content_hash) == 64

    bounded_item = JobAlertItem(
        provider="synthetic_alert",
        title="t" * 250,
        company="c" * 200,
        location="l" * 250,
        canonical_url="https://jobs.invalid/bounded",
        account_id="a",
        message_id="m",
        summary="s" * 1_200,
        evidence=tuple(f"evidence-{index}-" + "x" * 150 for index in range(12)),
    )
    assert len(bounded_item.title) == 200
    assert len(bounded_item.company) == 160
    assert len(bounded_item.location) == 200
    assert len(bounded_item.summary) == 1_000
    assert len(bounded_item.evidence) == 8
    assert all(len(value) <= 120 for value in bounded_item.evidence)
    bounded_result = JobAlertParseResult(
        provider="synthetic_alert",
        status=AlertParseStatus.PARSED,
        items=(bounded_item,) * 60,
        issues=tuple(f"issue-{index}-" + "x" * 180 for index in range(15)),
        examined_count=999,
        invalid_count=999,
    )
    assert len(bounded_result.items) == 50
    assert len(bounded_result.issues) == 10
    assert all(len(value) == 160 for value in bounded_result.issues)
    assert bounded_result.examined_count == 50
    assert bounded_result.invalid_count == 0


@pytest.mark.asyncio
async def test_migration_is_idempotent_and_preserves_legacy_version_zero(
    phase6_db: Path,
) -> None:
    with sqlite3.connect(phase6_db) as db:
        db.execute(
            """
            INSERT INTO zoho_mail_messages
                (account_id, message_id, first_seen_at, last_seen_at, processed)
            VALUES ('acct1', 'legacy', '2026-01-01', '2026-01-01', 1)
            """
        )
        db.commit()
    await init_zoho_mail_db()
    await init_zoho_mail_db()
    with sqlite3.connect(phase6_db) as db:
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(zoho_mail_messages)")
        }
        assert {
            "processing_version",
            "mail_intent",
            "alert_provider",
            "processing_result",
            "processing_reason",
        } <= columns
        assert db.execute(
            "SELECT processed, processing_version FROM zoho_mail_messages WHERE message_id='legacy'"
        ).fetchone() == (1, 0)
        assert db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='email_job_alert_items'"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='email_job_alert_provider_health'"
        ).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_linkedin_and_indeed_like_mail_create_no_false_applications(
    phase6_db: Path,
) -> None:
    messages = [
        message("li", subject="LinkedIn Job Alert: five roles for you"),
        message("in", subject="Indeed: new jobs matching frontend developer"),
    ]
    api = FakeZohoAPI(
        messages,
        contents={
            "li": "Frontend role at Example — view this job opportunity",
            "in": "Recommended position and career opportunity",
        },
    )
    result = await ZohoMailIngestionWorker(api).run(dry_run=False)
    assert result.unknown_job_messages == 2
    assert result.application_messages == 0
    with sqlite3.connect(phase6_db) as db:
        assert db.execute("SELECT COUNT(*) FROM email_job_applications").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM email_application_review_queue").fetchone()[0] == 0
        assert {
            row[0] for row in db.execute("SELECT mail_intent FROM zoho_mail_messages")
        } == {MailIntent.UNKNOWN_JOB_EMAIL.value}


@pytest.mark.asyncio
async def test_application_history_still_extracts_reviews_and_discovery_metadata(
    phase6_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery_path = phase6_db.parent / "application-discovery.txt"
    monkeypatch.setattr(config, "ZOHO_COMPANY_DISCOVERY_ENABLED", True)
    monkeypatch.setattr(config, "ZOHO_DISCOVERY_MIN_CONFIDENCE", 0.75)
    monkeypatch.setattr(config, "ZOHO_DISCOVERY_SEED_FILE", str(discovery_path))
    api = FakeZohoAPI(
        [
            message(
                "app",
                subject="Application received for Frontend Engineer",
                sender="Recruiting <jobs@newcompanygmbh.teamtailor-mail.com>",
            )
        ],
        contents={"app": "Thank you for applying."},
    )
    result = await ZohoMailIngestionWorker(api).run(dry_run=False)
    assert result.application_messages == 1
    assert result.extracted_records == 1
    assert result.review_records == 1
    assert result.discovery_candidates == 1
    assert "jsonld:newcompanygmbh" in discovery_path.read_text(encoding="utf-8")
    with sqlite3.connect(phase6_db) as db:
        assert db.execute("SELECT COUNT(*) FROM email_job_applications").fetchone()[0] == 1
        assert db.execute(
            "SELECT ats, ats_slug FROM email_job_applications"
        ).fetchone() == ("teamtailor", "newcompanygmbh")
        assert db.execute(
            "SELECT COUNT(*) FROM email_application_review_queue"
        ).fetchone()[0] == 1
        assert db.execute("SELECT processing_version FROM zoho_mail_messages").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_application_history_is_not_limited_by_alert_window(
    phase6_db: Path,
) -> None:
    api = FakeZohoAPI(
        [
            message(
                "old-application",
                subject="Application received for Frontend Engineer",
                days_ago=30,
            )
        ],
        contents={"old-application": "We received your application"},
    )
    result = await ZohoMailIngestionWorker(api).run(dry_run=False)
    assert result.application_messages == 1
    with sqlite3.connect(phase6_db) as db:
        assert db.execute("SELECT COUNT(*) FROM email_job_applications").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_current_version_overlap_skips_fetch_but_updates_moved_folder(
    phase6_db: Path,
) -> None:
    first = FakeZohoAPI(
        [message("same", subject="Application received for Frontend Engineer")],
        contents={"same": "Thank you for applying"},
    )
    await ZohoMailIngestionWorker(first).run(dry_run=False)
    second_message = message(
        "same",
        subject="Application received for Frontend Engineer",
        folder_id="archive",
    )
    second = FakeZohoAPI(
        [second_message],
        folders=[ZohoFolder("archive", "Archive")],
        contents={"same": "must not be fetched"},
    )
    result = await ZohoMailIngestionWorker(second).run(dry_run=False)
    assert second.content_calls == []
    assert result.current_version_skipped == 1
    with sqlite3.connect(phase6_db) as db:
        assert db.execute(
            "SELECT folder_name, processing_version FROM zoho_mail_messages"
        ).fetchone() == ("Archive", 1)


@pytest.mark.asyncio
async def test_legacy_version_zero_reclassifies_once_within_window(
    phase6_db: Path,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(phase6_db) as db:
        db.execute(
            """
            INSERT INTO zoho_mail_messages
                (account_id, message_id, folder_id, folder_name, subject, sender,
                 message_date, first_seen_at, last_seen_at, likely_job, processed,
                 processing_version)
            VALUES ('acct1', 'legacy', 'inbox', 'Inbox', ?, '', ?, ?, ?, 1, 1, 0)
            """,
            ("Application received for Frontend Engineer", now, now, now),
        )
        db.commit()
    api = FakeZohoAPI(
        [message("legacy", subject="Application received for Frontend Engineer")],
        contents={"legacy": "Thank you for applying"},
    )
    await ZohoMailIngestionWorker(api).run(dry_run=False)
    assert api.content_calls == ["legacy"]
    api.content_calls.clear()
    await ZohoMailIngestionWorker(api).run(dry_run=False)
    assert api.content_calls == []
    with sqlite3.connect(phase6_db) as db:
        assert db.execute(
            "SELECT processing_version FROM zoho_mail_messages WHERE message_id='legacy'"
        ).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_alert_batch_uses_one_company_cap_and_never_creates_source_metrics(
    phase6_db: Path,
) -> None:
    messages = [message(str(index)) for index in range(3)]
    parser = SyntheticAlertParser(item_count=1, company="One Company")
    api = FakeZohoAPI(messages, contents={str(index): "cards" for index in range(3)})
    result = await ZohoMailIngestionWorker(api, parser_registry=registry(parser)).run(
        dry_run=False
    )
    assert result.alert_messages == 3
    assert result.pipeline_accepted == 2
    assert result.pipeline_rejected == 1
    with sqlite3.connect(phase6_db) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM source_scan_runs").fetchone()[0] == 0
        outcomes = dict(
            db.execute(
                "SELECT terminal_outcome, COUNT(*) FROM email_job_alert_items GROUP BY terminal_outcome"
            )
        )
        assert outcomes == {"rejected": 1, "saved": 2}
        assert db.execute(
            "SELECT COUNT(*) FROM email_job_applications"
        ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_alert_date_window_missing_and_old_are_terminal_without_jobs(
    phase6_db: Path,
) -> None:
    api = FakeZohoAPI(
        [message("old", days_ago=15), message("missing", date=None)],
        contents={"old": "cards", "missing": "cards"},
    )
    result = await ZohoMailIngestionWorker(api, parser_registry=registry()).run(
        dry_run=False
    )
    assert result.alert_messages == 2
    assert result.parsed_alert_items == 0
    with sqlite3.connect(phase6_db) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        reasons = {
            row[0] for row in db.execute("SELECT processing_reason FROM zoho_mail_messages")
        }
        assert reasons == {
            "alert_outside_14_day_window",
            "alert_missing_reliable_message_date",
        }


@pytest.mark.asyncio
async def test_parser_failure_leaves_message_unhandled_and_blocks_checkpoint(
    phase6_db: Path,
) -> None:
    api = FakeZohoAPI([message("bad")], contents={"bad": "cards"})
    parser = SyntheticAlertParser(parse_error=True)
    with pytest.raises(ValueError, match="synthetic parser failed"):
        await ZohoMailIngestionWorker(api, parser_registry=registry(parser)).run(
            dry_run=False
        )
    assert await get_last_successful_sync_at("acct1") is None
    with sqlite3.connect(phase6_db) as db:
        assert db.execute(
            "SELECT processed, processing_version FROM zoho_mail_messages"
        ).fetchone() == (0, 0)
        assert db.execute(
            "SELECT status, processing_failure_count FROM email_job_alert_provider_health"
        ).fetchone() == ("parse_error", 1)


@pytest.mark.asyncio
async def test_deterministic_invalid_alert_is_handled_without_poisoning_checkpoint(
    phase6_db: Path,
) -> None:
    parser = SyntheticAlertParser(item_count=0, invalid_count=1)
    api = FakeZohoAPI([message("invalid")], contents={"invalid": "bad card"})
    result = await ZohoMailIngestionWorker(api, parser_registry=registry(parser)).run(
        dry_run=False
    )
    assert result.invalid_alert_items == 1
    assert result.checkpoint_advanced is True
    with sqlite3.connect(phase6_db) as db:
        assert db.execute(
            "SELECT processed, processing_version, processing_reason FROM zoho_mail_messages"
        ).fetchone() == (1, 1, "synthetic_invalid_card")


@pytest.mark.asyncio
async def test_sync_item_limit_leaves_message_and_checkpoint_pending(
    phase6_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(zoho_module, "MAX_ALERT_ITEMS_PER_SYNC", 2)
    parser = SyntheticAlertParser(item_count=3)
    api = FakeZohoAPI([message("many")], contents={"many": "cards"})
    result = await ZohoMailIngestionWorker(api, parser_registry=registry(parser)).run(
        dry_run=False
    )
    assert result.pending_alert_items == 2
    assert result.backlog_deferred == 1
    assert result.checkpoint_advanced is False
    with sqlite3.connect(phase6_db) as db:
        assert db.execute("SELECT COUNT(*) FROM email_job_alert_items").fetchone()[0] == 2
        assert db.execute(
            "SELECT processed, processing_version FROM zoho_mail_messages"
        ).fetchone() == (0, 0)


@pytest.mark.asyncio
async def test_cleanup_deletes_only_old_processed_items(phase6_db: Path) -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
    with sqlite3.connect(phase6_db) as db:
        for identity, state in (("old-processed", "processed"), ("old-pending", "pending")):
            db.execute(
                """
                INSERT INTO email_job_alert_items
                    (provider, identity_key, content_hash, account_id, message_id,
                     title, company, location, state, first_seen_at, last_seen_at)
                VALUES ('synthetic_alert', ?, 'hash', 'a', 'm', 'T', 'C', 'L', ?, ?, ?)
                """,
                (identity, state, old, old),
            )
        db.commit()
    assert await cleanup_processed_alert_items(dry_run=False) == 1
    with sqlite3.connect(phase6_db) as db:
        assert db.execute(
            "SELECT state FROM email_job_alert_items"
        ).fetchone()[0] == "pending"


@pytest.mark.asyncio
async def test_strong_dry_run_absent_database_remains_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "absent.db"
    discovery_path = tmp_path / "absent-discovery.txt"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_path))
    monkeypatch.setattr(config, "ZOHO_INITIAL_SYNC_FROM", "")
    monkeypatch.setattr(config, "ZOHO_DISCOVERY_SEED_FILE", str(discovery_path))
    delivery = AsyncMock()
    monkeypatch.setattr(zoho_module, "process_pending_immediate_deliveries", delivery)
    api = FakeZohoAPI([message("dry")], contents={"dry": "cards"})
    result = await ZohoMailIngestionWorker(api, parser_registry=registry()).run(
        dry_run=True
    )
    assert result.dry_run is True
    assert result.pipeline_accepted + result.pipeline_rejected == 1
    assert not db_path.exists()
    assert not discovery_path.exists()
    delivery.assert_not_awaited()


@pytest.mark.asyncio
async def test_strong_dry_run_existing_database_sha_and_mtime_unchanged(
    phase6_db: Path,
) -> None:
    before_hash = hashlib.sha256(phase6_db.read_bytes()).hexdigest()
    before_mtime = phase6_db.stat().st_mtime_ns
    api = FakeZohoAPI([message("dry-existing")], contents={"dry-existing": "cards"})
    await ZohoMailIngestionWorker(api, parser_registry=registry()).run(dry_run=True)
    assert hashlib.sha256(phase6_db.read_bytes()).hexdigest() == before_hash
    assert phase6_db.stat().st_mtime_ns == before_mtime


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_cache", [False, True])
async def test_dry_oauth_refresh_never_mutates_token_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_cache: bool,
) -> None:
    token_path = tmp_path / "token.json"
    if existing_cache:
        token_path.write_text(
            json.dumps({"access_token": "expired", "expires_at": 1}),
            encoding="utf-8",
        )
    before_bytes = token_path.read_bytes() if token_path.exists() else None
    before_mtime = token_path.stat().st_mtime_ns if token_path.exists() else None
    db_path = tmp_path / "oauth-absent.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_path))
    monkeypatch.setattr(config, "ZOHO_OAUTH_TOKEN_FILE", str(token_path))
    monkeypatch.setattr(config, "ZOHO_CLIENT_ID", "cid")
    monkeypatch.setattr(config, "ZOHO_CLIENT_SECRET", "secret")
    monkeypatch.setattr(config, "ZOHO_REFRESH_TOKEN", "refresh")
    monkeypatch.setattr(config, "ZOHO_ACCOUNTS_URL", "https://accounts.zoho.eu")
    monkeypatch.setattr(config, "ZOHO_MAIL_API_BASE", "https://mail.zoho.eu")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/v2/token":
            return httpx.Response(200, json={"access_token": "memory", "expires_in": 3600})
        if request.url.path == "/api/accounts":
            return httpx.Response(200, json={"data": [{"accountId": "acct1"}]})
        if request.url.path.endswith("/folders"):
            return httpx.Response(200, json={"data": []})
        raise AssertionError(request.url)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ZohoOAuthMailClient(http)
    result = await ZohoMailIngestionWorker(client).run(dry_run=None)
    await http.aclose()
    assert result.dry_run is True
    assert not db_path.exists()
    if existing_cache:
        assert token_path.read_bytes() == before_bytes
        assert token_path.stat().st_mtime_ns == before_mtime
    else:
        assert not token_path.exists()


@pytest.mark.asyncio
async def test_dry_oauth_valid_cache_is_read_without_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_path = tmp_path / "valid-token.json"
    token_path.write_text(
        json.dumps(
            {
                "access_token": "cached",
                "expires_at": datetime.now(timezone.utc).timestamp() + 3600,
                "mail_api_base": "https://mail.zoho.eu",
            }
        ),
        encoding="utf-8",
    )
    before_hash = hashlib.sha256(token_path.read_bytes()).hexdigest()
    before_mtime = token_path.stat().st_mtime_ns
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "absent.db"))
    monkeypatch.setattr(config, "ZOHO_OAUTH_TOKEN_FILE", str(token_path))
    monkeypatch.setattr(config, "ZOHO_MAIL_API_BASE", "https://mail.zoho.eu")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if request.url.path == "/api/accounts":
            return httpx.Response(200, json={"data": [{"accountId": "acct1"}]})
        if request.url.path.endswith("/folders"):
            return httpx.Response(200, json={"data": []})
        raise AssertionError(request.url)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ZohoOAuthMailClient(http)
    await ZohoMailIngestionWorker(client).run(dry_run=True)
    await http.aclose()
    assert hashlib.sha256(token_path.read_bytes()).hexdigest() == before_hash
    assert token_path.stat().st_mtime_ns == before_mtime


def test_alert_posted_at_is_never_derived_from_mail_date() -> None:
    parser = SyntheticAlertParser()
    metadata = MailMessageMetadata(
        "a",
        "m",
        subject="Synthetic alert",
        message_date=datetime.now(timezone.utc),
    )
    item = parser.parse(metadata, build_bounded_mail_content("cards")).items[0]
    assert item.posted_at is None


@pytest.mark.asyncio
async def test_alert_without_explicit_work_eligibility_is_rejected() -> None:
    item = JobAlertItem(
        provider="synthetic_alert",
        title="Frontend React Developer",
        company="Example",
        location="Remote",
        canonical_url="https://jobs.invalid/unknown-scope",
        account_id="a",
        message_id="m",
    )
    result = await process_discovered_jobs(
        [JobIngestionCandidate("synthetic_alert:id:unknown", alert_item_to_job(item))],
        persist=False,
        associate_items=True,
    )
    assert result.item_results[0].status == JobIngestionStatus.REJECTED
    assert result.item_results[0].rejection_code == "location"


def test_alert_identity_stays_separate_from_direct_final_job_url() -> None:
    item = JobAlertItem(
        provider="synthetic_alert",
        provider_item_id="provider-123",
        title="Frontend Developer",
        company="Example",
        location="Remote - Germany",
        canonical_url="https://provider.invalid/jobs/123?utm_source=mail",
        job_url="https://ats.invalid/example/jobs/abc?utm_campaign=alert",
        account_id="a",
        message_id="m",
        is_remote=True,
        workplace_type="remote",
        remote_scope="germany",
    )
    identity, _ = alert_identity_key(
        provider_item_id=item.provider_item_id,
        canonical_url=item.canonical_url,
        title=item.title,
        company=item.company,
        location=item.location,
    )
    assert identity == "id:provider-123"
    assert item.canonical_url == "https://provider.invalid/jobs/123"
    assert alert_item_to_job(item).url == "https://ats.invalid/example/jobs/abc"
