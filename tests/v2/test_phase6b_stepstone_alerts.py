from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import config
import integrations.gmail_mail as gmail_module
import integrations.zoho_mail as zoho_module
from integrations.gmail_mail import GmailMailIngestionWorker
from integrations.job_alerts import (
    AlertParseStatus,
    AlertParserRegistry,
    MailIntent,
    MailMessageMetadata,
)
from integrations.job_alerts.message import build_bounded_mail_content
from integrations.job_alerts.processing import alert_item_to_job
from integrations.job_alerts.routing import route_mail_intent
from integrations.job_alerts.stepstone import (
    StepStoneAlertParser,
    is_stepstone_click_wrapper,
    safe_stepstone_search_url,
)
from integrations.zoho_mail import ZohoMailIngestionWorker
from job_ingestion import JobIngestionCandidate, process_discovered_jobs
from models.job import Job
from notifiers.base import DeliverySuccess
from notifiers.delivery import process_pending_immediate_deliveries
from sources.catalog import GROUP_BY_ID, manual_all_source_names
from storage.database import get_delivery_receipts, init_db
from storage.zoho_mail import init_zoho_mail_db
from tests.v2.test_phase6a1_alert_foundation import FakeZohoAPI, message
from tests.v2.test_phase6a3_gmail_transport import FakeGmailAPI, gmail_message
from tools.inspect_gmail_stepstone import build_safe_report

FIXTURES = Path(__file__).parent / "fixtures" / "job_alerts"
SUBJECT = "Example Product GmbH and 1 other company are looking for candidates like you"
SENDER = "StepStone <info@jobagent.stepstone.de>"


def test_documented_gmail_query_covers_all_completed_alert_providers() -> None:
    expected_senders = {
        "jobalerts-noreply@linkedin.com",
        "donotreply@match.indeed.com",
        "info@jobagent.stepstone.de",
    }
    for path in (Path(".env.example"), Path("README.md")):
        query_line = next(
            line for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("GMAIL_QUERY=")
        )
        assert all(sender in query_line for sender in expected_senders)


def fixture() -> str:
    return (FIXTURES / "stepstone_alert_sanitized.html").read_text(encoding="utf-8")


def metadata(*, subject: str = SUBJECT, message_id: str = "stepstone-1") -> MailMessageMetadata:
    return MailMessageMetadata(
        "mailbox-1",
        message_id,
        subject=subject,
        sender=SENDER,
        message_date=datetime.now(timezone.utc),
    )


def card(
    opaque: str,
    *,
    badge: str = "Strong Fit",
    title: str = "Senior Frontend React TypeScript Engineer",
    company: str = "Example Product GmbH",
    location: str = "Berlin",
    contract: str = "Feste Anstellung",
    workplace_time: str = "Homeoffice möglich, Vollzeit",
    salary: str = "72.000 - 88.000 €/Jahr",
) -> str:
    optional_badge = f"<tr><td>{badge}</td></tr>" if badge else ""
    optional_title = (
        f'<tr><td><a href="https://click.stepstone.de/{opaque}"><strong>{title}</strong></a></td></tr>'
    )
    optional_company_heading = f"<tr><td>{company}</td></tr>"
    optional = "".join(
        f"<tr><td><span>{value}</span></td></tr>"
        for _label, value in (
            ("contract type", contract),
            ("time", workplace_time),
            ("salary", salary),
        )
        if value
    )
    return f"""
    <table role="presentation">
      {optional_badge}{optional_company_heading}{optional_title}
      <tr><td><span>{company}</span></td></tr>
      <tr><td><span>{location}</span></td></tr>
      {optional}
    </table>
    """


def mail(cards: str, *, footer: bool = True) -> str:
    cta = (
        '<a href="https://click.stepstone.de/SYNTHETIC_OPAQUE_ALL">See all matching jobs</a>'
        if footer
        else ""
    )
    return f"""
    <h1>Check out your latest matches...</h1>
    <p>We found these new jobs that match your search for synthetic roles.</p>
    {cards}
    <p>Candidates like you also viewed these jobs</p>
    {cta}
    """


def test_registry_routes_only_strong_stepstone_digest_structure() -> None:
    registry = AlertParserRegistry()
    assert tuple(parser.provider for parser in registry.parsers) == (
        "linkedin",
        "indeed",
        "stepstone",
    )
    content = build_bounded_mail_content(fixture())
    match = StepStoneAlertParser().matches(metadata(), content)
    assert match.strong
    assert set(match.evidence) == {
        "stepstone_alert_sender",
        "stepstone_digest_heading",
        "stepstone_matching_jobs_body",
        "stepstone_matching_jobs_cta",
        "stepstone_click_wrapper_structure",
    }
    decision = route_mail_intent(metadata(), content, registry)
    assert (decision.intent, decision.provider) == (MailIntent.JOB_ALERT, "stepstone")


def test_authoritative_multicard_digest_extracts_only_explicit_fields() -> None:
    parsed = StepStoneAlertParser().parse(
        metadata(), build_bounded_mail_content(fixture())
    )
    assert parsed.status == AlertParseStatus.PARSED
    assert (parsed.examined_count, parsed.invalid_count, len(parsed.items)) == (2, 0, 2)
    first, second = parsed.items
    assert (first.title, first.company, first.location) == (
        "Senior Frontend React TypeScript Engineer",
        "Example Product GmbH",
        "Berlin",
    )
    assert first.employment_text == "Feste Anstellung, Homeoffice möglich, Vollzeit"
    assert first.salary == "72.000 - 88.000 €/Jahr"
    assert first.workplace_type == "hybrid"
    assert first.is_remote is False
    assert first.remote_scope is None
    assert first.posted_at is None
    assert second.salary == ""
    assert second.workplace_type == "remote"
    assert second.remote_scope == "germany"
    assert alert_item_to_job(first).source == "stepstone_alert"


def test_one_card_optional_badge_optional_fields_and_duplicates() -> None:
    one = card(
        "SYNTHETIC_ONE",
        badge="",
        contract="",
        workplace_time="",
        salary="",
    )
    parsed = StepStoneAlertParser().parse(
        metadata(), build_bounded_mail_content(mail(one))
    )
    assert len(parsed.items) == 1
    assert parsed.items[0].employment_text == ""
    assert parsed.items[0].salary == ""
    assert parsed.items[0].workplace_type == "unknown"

    repeated = StepStoneAlertParser().parse(
        metadata(), build_bounded_mail_content(mail(one + one.replace("SYNTHETIC_ONE", "SYNTHETIC_TWO")))
    )
    assert len(repeated.items) == 1
    assert repeated.issues == ("duplicate_provider_item",)


def test_missing_required_and_malformed_cards_are_bounded() -> None:
    good = card("SYNTHETIC_GOOD")
    missing_title = card("SYNTHETIC_NO_TITLE", title="")
    missing_company = card("SYNTHETIC_NO_COMPANY", company="")
    missing_location = card("SYNTHETIC_NO_LOCATION", location="")
    parsed = StepStoneAlertParser().parse(
        metadata(),
        build_bounded_mail_content(
            mail(good + missing_title + missing_company + missing_location)
        ),
    )
    assert len(parsed.items) == 1
    assert parsed.invalid_count == 3
    assert parsed.examined_count == 4
    assert parsed.issues == ("missing_required_fields",)


def test_personalized_wrappers_are_refused_as_identity_and_navigation() -> None:
    opaque = "SYNTHETIC_PRIVATE_PROFILE_TOKEN_DO_NOT_STORE"
    body = mail(card(opaque))
    parsed = StepStoneAlertParser().parse(
        metadata(), build_bounded_mail_content(body)
    )
    item = parsed.items[0]
    assert item.provider_item_id.startswith("content-")
    assert item.canonical_url.startswith("https://www.stepstone.de/jobs?")
    assert "click.stepstone.de" not in item.canonical_url
    assert opaque not in item.canonical_url
    assert opaque not in item.provider_item_id
    assert opaque not in " ".join(item.evidence)
    assert item.job_url == ""
    assert is_stepstone_click_wrapper(f"https://click.stepstone.de/{opaque}")
    assert is_stepstone_click_wrapper(
        "https://click.stepstone.de/" + "X" * 2_000
    )
    assert not is_stepstone_click_wrapper(
        f"https://click.stepstone.de.evil.invalid/{opaque}"
    )
    assert not is_stepstone_click_wrapper("javascript:alert(1)")


def test_content_identity_and_safe_search_url_ignore_wrapper_changes() -> None:
    first = StepStoneAlertParser().parse(
        metadata(), build_bounded_mail_content(mail(card("SYNTHETIC_FIRST")))
    ).items[0]
    second = StepStoneAlertParser().parse(
        metadata(message_id="stepstone-2"),
        build_bounded_mail_content(mail(card("SYNTHETIC_SECOND"))),
    ).items[0]
    assert first.provider_item_id == second.provider_item_id
    assert first.canonical_url == second.canonical_url
    assert first.canonical_url == safe_stepstone_search_url(
        first.title, first.company, first.location
    )


def test_non_alert_marketing_and_application_recruitment_keep_precedence() -> None:
    registry = AlertParserRegistry()
    marketing = build_bounded_mail_content(
        '<p>Update your StepStone profile.</p><a href="https://click.stepstone.de/SYNTHETIC_MARKETING">Profile</a>'
    )
    assert not StepStoneAlertParser().matches(metadata(subject="Profile update"), marketing).matched
    assert route_mail_intent(metadata(subject="Profile update"), marketing, registry).intent == MailIntent.UNKNOWN_JOB_EMAIL

    for subject, body in (
        ("Application received", fixture() + " We received your application."),
        ("Your interview", fixture() + " We invite you to an interview."),
        ("Recruiter outreach", fixture() + " A recruiter found your profile."),
    ):
        decision = route_mail_intent(
            metadata(subject=subject), build_bounded_mail_content(body), registry
        )
        assert decision.intent == MailIntent.APPLICATION_OR_RECRUITMENT


@pytest.fixture
async def provider_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "stepstone.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(path))
    monkeypatch.setattr(gmail_module.config, "DATABASE_PATH", str(path))
    monkeypatch.setattr(config, "ZOHO_INITIAL_SYNC_FROM", "")
    monkeypatch.setattr(config, "ZOHO_COMPANY_DISCOVERY_ENABLED", False)
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL_NGO", "")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(
        zoho_module, "process_pending_immediate_deliveries", AsyncMock()
    )
    monkeypatch.setattr(
        gmail_module, "process_pending_immediate_deliveries", AsyncMock()
    )
    await init_zoho_mail_db()
    await init_db()
    return path


@pytest.mark.asyncio
async def test_stepstone_alert_creates_no_false_application_or_review_rows(
    provider_db: Path,
) -> None:
    api = FakeZohoAPI(
        [message("stepstone", subject=SUBJECT, sender=SENDER)],
        contents={"stepstone": fixture()},
    )
    result = await ZohoMailIngestionWorker(api).run(dry_run=False)
    assert result.alert_messages == 1
    assert result.valid_alert_items == 2
    assert result.application_messages == 0
    with sqlite3.connect(provider_db) as db:
        assert db.execute("SELECT COUNT(*) FROM email_job_applications").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM email_application_review_queue").fetchone()[0] == 0
        assert {row[0] for row in db.execute("SELECT source FROM jobs")} <= {"stepstone_alert"}


@pytest.mark.asyncio
async def test_repeated_gmail_alerts_share_items_without_persisting_wrappers(
    provider_db: Path,
) -> None:
    body = mail(card("SYNTHETIC_PRIVATE_PROFILE_TOKEN"))
    messages = {
        message_id: gmail_message(
            message_id,
            body,
            sender=SENDER,
            subject=SUBJECT,
        )
        for message_id in ("gmail-stepstone-1", "gmail-stepstone-2")
    }
    api = FakeGmailAPI(
        pages={None: {"messages": [{"id": key} for key in messages]}},
        messages=messages,
    )
    result = await GmailMailIngestionWorker(
        api,
        mailbox_key="stepstone-mailbox",
        label_ids=("Label_jobs",),
        query="from:info@jobagent.stepstone.de",
    ).run(dry_run=False)
    assert result.valid_alert_items == 2
    assert result.pending_alert_items == 1
    with sqlite3.connect(provider_db) as db:
        assert db.execute("SELECT COUNT(*) FROM email_job_alert_items").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM email_job_alert_occurrences").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM source_scan_runs").fetchone()[0] == 0
        row = db.execute(
            "SELECT provider_item_id, canonical_url FROM email_job_alert_items"
        ).fetchone()
    assert row is not None
    assert "SYNTHETIC_PRIVATE_PROFILE_TOKEN" not in " ".join(row)
    assert "click.stepstone.de" not in " ".join(row)


class _FakeDiscord:
    general_configured = True
    ngo_configured = False

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def has_destination(self, destination: str) -> bool:
        return destination == "discord_general"

    async def send_jobs(self, jobs: list[Job], **_kwargs: object) -> list[DeliverySuccess]:
        self.calls.append([job.id for job in jobs])
        return [DeliverySuccess(job.id, "discord_general") for job in jobs]


class _DisabledTelegram:
    configured = False


@pytest.mark.asyncio
@pytest.mark.parametrize("alert_first", [False, True])
async def test_stepstone_and_direct_source_dedup_to_one_immediate_obligation(
    provider_db: Path,
    alert_first: bool,
) -> None:
    item = StepStoneAlertParser().parse(
        metadata(), build_bounded_mail_content(mail(card("SYNTHETIC_DEDUP")))
    ).items[0]
    alert_job = alert_item_to_job(item)
    direct_job = Job.model_validate(
        {
            **alert_job.model_dump(),
            "id": "",
            "content_hash": "",
            "url": "https://careers.example.invalid/jobs/frontend-platform",
            "source": "greenhouse",
            "description": "React TypeScript frontend product development",
        }
    )
    ordered = (alert_job, direct_job) if alert_first else (direct_job, alert_job)
    result = await process_discovered_jobs(
        [
            JobIngestionCandidate("first", ordered[0]),
            JobIngestionCandidate("second", ordered[1]),
        ],
        persist=True,
        associate_items=True,
    )
    assert len(result.saved_jobs) == 1
    assert result.saved_jobs[0].notification_tier == "immediate"
    discord = _FakeDiscord()
    delivery = await process_pending_immediate_deliveries(
        discord_notifier=discord,
        telegram_notifier=_DisabledTelegram(),
    )
    assert delivery.selected_count == 1
    assert len(discord.calls) == 1
    assert len(discord.calls[0]) == 1
    assert len(await get_delivery_receipts(result.saved_jobs[0].id)) == 1
    with sqlite3.connect(provider_db) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_gmail_strong_dry_run_is_immutable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "must-stay-absent.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(path))
    monkeypatch.setattr(gmail_module.config, "DATABASE_PATH", str(path))
    raw = gmail_message(
        "gmail-stepstone-dry",
        fixture(),
        sender=SENDER,
        subject=SUBJECT,
    )
    api = FakeGmailAPI(
        pages={None: {"messages": [{"id": "gmail-stepstone-dry"}]}},
        messages={"gmail-stepstone-dry": raw},
    )
    result = await GmailMailIngestionWorker(
        api,
        mailbox_key="stepstone-mailbox",
        label_ids=("Label_jobs",),
        query="from:info@jobagent.stepstone.de",
    ).run(dry_run=True)
    assert result.valid_alert_items == 2
    assert result.provider_health == ("stepstone:parsed",)
    assert result.checkpoint_advanced is False
    assert not path.exists()


def test_fixture_is_sanitized_and_source_scheduling_is_unchanged() -> None:
    text = fixture()
    lowered = text.lower()
    assert "test user" in lowered
    assert "synthetic_opaque" in lowered
    assert "@" not in text
    assert all(
        private not in lowered
        for private in (
            "verivox",
            "autohaus royal",
            "kpmg",
            "a.b.s. rechenzentrum",
            "48.000 - 80.000",
        )
    )
    assert "stepstone_alert" not in manual_all_source_names()
    assert all(
        "stepstone_alert" not in group.source_names for group in GROUP_BY_ID.values()
    )


@pytest.mark.asyncio
async def test_stepstone_inspector_reports_structure_without_private_values() -> None:
    private_address = "private-recipient@example.invalid"
    private_token = "PRIVATE_PERSONALIZED_TOKEN_ABC123"
    body = fixture().replace("SYNTHETIC_OPAQUE_CARD_ALPHA", private_token)
    body += f"<p>{private_address}</p>"
    raw = gmail_message(
        "private-message-identifier",
        body,
        sender=SENDER,
        subject=SUBJECT,
    )
    raw["payload"]["headers"].append({"name": "To", "value": private_address})
    api = FakeGmailAPI(pages={}, messages={})
    from integrations.gmail_mail import decode_gmail_message

    decoded = await decode_gmail_message(api, raw, "private-message-identifier")
    serialized = str(build_safe_report(raw, decoded))
    assert "sender_domain_match" in serialized
    assert "click.stepstone.de/{opaque}" in serialized
    assert private_address not in serialized
    assert private_token not in serialized
    assert "private-message-identifier" not in serialized
    assert SUBJECT not in serialized
    assert SENDER not in serialized
