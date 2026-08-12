from __future__ import annotations

import base64
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import config
import integrations.zoho_mail as zoho_module
from integrations.job_alerts import AlertParseStatus, AlertParserRegistry, MailIntent
from integrations.job_alerts.indeed import IndeedAlertParser
from integrations.job_alerts.linkedin import LinkedInAlertParser
from integrations.job_alerts.message import build_bounded_mail_content
from integrations.job_alerts.processing import alert_item_to_job
from integrations.job_alerts.routing import route_mail_intent
from integrations.job_alerts.urls import (
    canonicalize_indeed_job_url,
    canonicalize_linkedin_job_url,
)
from integrations.zoho_mail import (
    ZohoMailIngestionWorker,
    is_likely_job_email,
)
from job_ingestion import (
    JobIngestionCandidate,
    JobIngestionStatus,
    process_discovered_jobs,
)
from models.job import Job
from storage.database import init_db
from storage.zoho_mail import init_zoho_mail_db
from tests.v2.test_phase6a1_alert_foundation import FakeZohoAPI, message

FIXTURES = Path(__file__).parent / "fixtures" / "job_alerts"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def metadata(
    provider: str,
    *,
    subject: str | None = None,
    message_id: str = "message-1",
):
    from integrations.job_alerts import MailMessageMetadata

    if provider == "linkedin":
        return MailMessageMetadata(
            "account-1",
            message_id,
            subject=subject or "Software Engineer at Example Product GmbH",
            sender="LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
            message_date=datetime(2026, 8, 11, tzinfo=timezone.utc),
        )
    return MailMessageMetadata(
        "account-1",
        message_id,
        subject=subject
        or "Senior Frontend React TypeScript Engineer bei Beispiel Digital GmbH",
        sender="Indeed <donotreply@match.indeed.com>",
        message_date=datetime(2026, 8, 11, tzinfo=timezone.utc),
    )


def wrapper(payload: dict[str, object]) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    return f"https://cts.indeed.com/v3/{encoded}"


def linkedin_card(
    job_id: str,
    *,
    title: str = "Senior Frontend React Engineer",
    company: str = "Example GmbH",
    location: str = "Remote — Germany",
    extra: str = "",
) -> str:
    return f"""
    <div class="job-card" data-job-card="true">
      <a class="job-title" href="https://www.linkedin.com/comm/jobs/view/{job_id}/?trackingId=TEST_TOKEN">{title}</a>
      <span class="job-company">{company}</span>
      <span class="job-location">{location}</span>
      {extra}
    </div>
    """


def linkedin_mail(cards: str) -> str:
    return (
        "<h1>Your job alert for Frontend Engineer</h1>"
        + cards
        + "<footer>You are receiving Job Alert emails.</footer>"
    )


def indeed_mail(card: str) -> str:
    return f"{card}<a>Passt nicht</a>"


def indeed_card(
    job_url: str,
    *,
    title: str = "Senior Frontend React Engineer",
    company: str = "Beispiel GmbH",
    location: str = "Berlin",
    extra: str = '<span class="job-workplace">Hybrides Arbeiten</span>',
) -> str:
    return f"""
    <section class="recommendation-card" data-job-card="true">
      <h2 class="job-title">{title}</h2>
      <span class="job-company">{company}</span>
      <span class="job-location">{location}</span>
      {extra}
      <a href="{job_url}">Job anzeigen</a>
    </section>
    """


def test_registry_and_job_specific_subject_routing() -> None:
    registry = AlertParserRegistry()
    assert tuple(parser.provider for parser in registry.parsers) == (
        "linkedin",
        "indeed",
        "stepstone",
    )
    for provider, name in (
        ("linkedin", "linkedin_alert_sanitized.html"),
        ("indeed", "indeed_alert_sanitized.html"),
    ):
        decision = route_mail_intent(
            metadata(provider),
            build_bounded_mail_content(fixture(name)),
            registry,
        )
        assert decision.intent == MailIntent.JOB_ALERT
        assert decision.provider == provider


def test_linkedin_authoritative_multiple_cards_and_metadata() -> None:
    parsed = LinkedInAlertParser().parse(
        metadata("linkedin"),
        build_bounded_mail_content(fixture("linkedin_alert_sanitized.html")),
    )
    assert parsed.status == AlertParseStatus.PARSED
    assert parsed.examined_count == 2
    assert parsed.invalid_count == 0
    assert [item.provider_item_id for item in parsed.items] == [
        "900000001",
        "900000002",
    ]
    first, second = parsed.items
    assert first.canonical_url == "https://www.linkedin.com/jobs/view/900000001"
    assert first.salary == "€75,000–€90,000"
    assert first.posted_at == datetime(2026, 8, 10, 9, 30, tzinfo=timezone.utc)
    assert first.workplace_type == "hybrid"
    assert second.canonical_url == "https://www.linkedin.com/jobs/view/900000002"
    assert second.job_url == "https://jobs.sample.invalid/positions/full-stack"
    assert second.workplace_type == "remote"
    assert second.remote_scope == "germany"
    job = alert_item_to_job(second)
    assert job.url == second.job_url
    assert job.source == "linkedin_alert"


def test_linkedin_single_duplicate_missing_malformed_and_unsafe_cards() -> None:
    good = linkedin_card("900000010")
    duplicate = linkedin_card("900000010", title="Duplicate presentation")
    missing = linkedin_card("900000011", company="")
    malformed = linkedin_card("not-numeric")
    parsed = LinkedInAlertParser().parse(
        metadata("linkedin"),
        build_bounded_mail_content(
            linkedin_mail(good + duplicate + missing + malformed)
        ),
    )
    assert len(parsed.items) == 1
    assert parsed.items[0].posted_at is None
    assert parsed.examined_count == 4
    assert parsed.invalid_count == 2
    assert set(parsed.issues) == {
        "duplicate_provider_item",
        "missing_required_fields",
        "unsafe_or_unrecognized_job_url",
    }
    assert canonicalize_linkedin_job_url("javascript:alert(1)") is None
    assert canonicalize_linkedin_job_url(
        "https://linkedin.com.evil.invalid/comm/jobs/view/900000010/"
    ) is None

    single = LinkedInAlertParser().parse(
        metadata("linkedin"), build_bounded_mail_content(linkedin_mail(good))
    )
    assert len(single.items) == 1
    assert single.examined_count == 1


def test_linkedin_tracking_stripped_and_numeric_identity_required() -> None:
    assert canonicalize_linkedin_job_url(
        "https://www.linkedin.com/comm/jobs/view/900000123/"
        "?trackingId=TEST&refId=TEST&lipi=TEST&midToken=TEST&midSig=TEST"
        "&trk=TEST&trkEmail=TEST&eid=TEST&otpToken=TEST"
    ) == ("900000123", "https://www.linkedin.com/jobs/view/900000123")
    assert canonicalize_linkedin_job_url(
        "https://www.linkedin.com/jobs/view/not-numeric"
    ) is None


def test_linkedin_negative_recommendation_and_lifecycle_not_alerts() -> None:
    parser = LinkedInAlertParser()
    negative = build_bounded_mail_content(
        fixture("linkedin_land_faster_sanitized.html")
    )
    assert not parser.matches(metadata("linkedin"), negative).matched
    assert parser.parse(metadata("linkedin"), negative).status == AlertParseStatus.UNSUPPORTED

    registry = AlertParserRegistry()
    for subject, body in (
        ("Application received", "We received your application for a role."),
        ("Your interview", "We invite you to an interview."),
        ("Recruiter message", "A recruiter found your profile."),
    ):
        decision = route_mail_intent(
            metadata("linkedin", subject=subject),
            build_bounded_mail_content(body),
            registry,
        )
        assert decision.intent == MailIntent.APPLICATION_OR_RECRUITMENT


def test_indeed_authoritative_german_single_recommendation() -> None:
    parsed = IndeedAlertParser().parse(
        metadata("indeed"),
        build_bounded_mail_content(fixture("indeed_alert_sanitized.html")),
    )
    assert parsed.status == AlertParseStatus.PARSED
    assert len(parsed.items) == 1
    item = parsed.items[0]
    assert (item.title, item.company, item.location) == (
        "Senior Frontend React TypeScript Engineer",
        "Beispiel Digital GmbH",
        "Berlin",
    )
    assert item.provider_item_id.startswith("content-")
    assert item.canonical_url.startswith("https://de.indeed.com/jobs?")
    assert "cts.indeed.com" not in item.canonical_url
    assert canonicalize_indeed_job_url(item.canonical_url) is None
    assert item.workplace_type == "hybrid"
    assert item.posted_at is None
    assert item.summary == ""
    assert alert_item_to_job(item).source == "indeed_alert"

    changed_wrappers = fixture("indeed_alert_sanitized.html").replace(
        "U1lOVEhFVElDX09QQVFVRV9USVRMRQ",
        "U1lOVEhFVElDX09USEVSX09QQVFVRV9USVRMRQ",
    )
    repeated = IndeedAlertParser().parse(
        metadata("indeed"), build_bounded_mail_content(changed_wrappers)
    )
    assert repeated.items[0].provider_item_id == item.provider_item_id


def test_indeed_wrapper_decoding_and_canonicalization_are_offline() -> None:
    url = wrapper(
        {
            "metadata": {
                "url": "https://de.indeed.com/viewjob?jk=SYNTHETIC_JK_777"
                "&campaign=TEST_CAMPAIGN&utm_source=mail",
                "aggJobId": "SYNTHETIC_JK_777",
            },
            "clickType": "viewjob",
        }
    )
    result = canonicalize_indeed_job_url(url)
    assert result is not None
    assert result.provider_item_id == "SYNTHETIC_JK_777"
    assert result.canonical_url == (
        "https://de.indeed.com/viewjob?jk=SYNTHETIC_JK_777"
    )
    direct = canonicalize_indeed_job_url(
        "https://de.indeed.com/viewjob?jk=SYNTHETIC_JK_888"
        "&from=jobmail&utm_source=mail"
    )
    assert direct is not None
    assert direct.provider_item_id == "SYNTHETIC_JK_888"


def test_indeed_malformed_unsafe_wrapper_returns_bounded_issue() -> None:
    malformed = indeed_mail(
        indeed_card("https://cts.indeed.com/v3/NOT_BASE64_JSON")
    )
    result = IndeedAlertParser().parse(
        metadata("indeed"), build_bounded_mail_content(malformed)
    )
    assert result.status == AlertParseStatus.NO_ITEMS
    assert result.invalid_count == 1
    assert result.issues == ("malformed_or_unsafe_indeed_wrapper",)

    unsafe = wrapper(
        {
            "url": "javascript:alert(1)",
            "aggJobId": "SYNTHETIC_JK_999",
        }
    )
    result = IndeedAlertParser().parse(
        metadata("indeed"),
        build_bounded_mail_content(indeed_mail(indeed_card(unsafe))),
    )
    assert result.status == AlertParseStatus.NO_ITEMS
    assert result.invalid_count == 1


def test_indeed_duplicate_missing_field_subject_fallback_and_direct_ats() -> None:
    first_url = "https://de.indeed.com/viewjob?jk=SYNTHETIC_JK_010&from=mail"
    second_url = "https://de.indeed.com/viewjob?jk=SYNTHETIC_JK_011&from=mail"
    duplicate = indeed_card(first_url)
    missing = indeed_card(second_url, location="")
    parsed = IndeedAlertParser().parse(
        metadata("indeed"),
        build_bounded_mail_content(indeed_mail(duplicate + duplicate + missing)),
    )
    assert len(parsed.items) == 1
    assert parsed.examined_count == 3
    assert parsed.invalid_count == 1
    assert set(parsed.issues) == {
        "duplicate_provider_item",
        "missing_required_fields",
    }

    no_title = indeed_card(
        "https://de.indeed.com/viewjob?jk=SYNTHETIC_JK_012",
        title="",
        extra=(
            '<a class="direct-job-link" data-direct-job-link="true" '
            'href="https://careers.example.invalid/jobs/12?utm_source=indeed">'
            "Auf Karriereseite bewerben</a>"
        ),
    )
    single = IndeedAlertParser().parse(
        metadata("indeed", subject="Frontend Platform Engineer"),
        build_bounded_mail_content(indeed_mail(no_title)),
    )
    assert single.items[0].title == "Frontend Platform Engineer"
    assert single.items[0].job_url == "https://careers.example.invalid/jobs/12"

    multiple = IndeedAlertParser().parse(
        metadata("indeed"),
        build_bounded_mail_content(
            indeed_mail(
                indeed_card(first_url)
                + indeed_card(
                    second_url,
                    title="Full Stack TypeScript Engineer",
                    company="Zweite Beispiel GmbH",
                )
            )
        ),
    )
    assert len(multiple.items) == 2
    assert multiple.examined_count == 2


def test_provider_item_limit_is_bounded() -> None:
    cards = "".join(
        linkedin_card(str(900001000 + index)) for index in range(60)
    )
    parsed = LinkedInAlertParser().parse(
        metadata("linkedin"), build_bounded_mail_content(linkedin_mail(cards))
    )
    assert len(parsed.items) == 50
    assert parsed.examined_count == 50


def test_indeed_non_alert_and_lifecycle_mail_not_parsed() -> None:
    registry = AlertParserRegistry()
    unrelated = route_mail_intent(
        metadata("indeed", subject="Profile update"),
        build_bounded_mail_content("Your profile settings were updated."),
        registry,
    )
    assert unrelated.intent == MailIntent.UNKNOWN_JOB_EMAIL
    for subject, body in (
        ("Bewerbung eingegangen", "Deine Bewerbung ist eingegangen."),
        ("Application update", "Update on your application."),
        ("Recruiter outreach", "A recruiter found your profile."),
    ):
        decision = route_mail_intent(
            metadata("indeed", subject=subject),
            build_bounded_mail_content(body),
            registry,
        )
        assert decision.intent == MailIntent.APPLICATION_OR_RECRUITMENT


@pytest.mark.asyncio
async def test_worker_routes_realistic_subjects_without_false_application_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "phase6a2.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_path))
    monkeypatch.setattr(config, "ZOHO_INITIAL_SYNC_FROM", "")
    monkeypatch.setattr(config, "ZOHO_COMPANY_DISCOVERY_ENABLED", False)
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL_NGO", "")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")

    async def no_delivery() -> None:
        return None

    monkeypatch.setattr(zoho_module, "process_pending_immediate_deliveries", no_delivery)
    await init_zoho_mail_db()
    await init_db()
    messages = [
        message(
            "linkedin-realistic",
            subject="Software Engineer at Example Product GmbH",
            sender="LinkedIn <jobalerts-noreply@linkedin.com>",
        ),
        message(
            "indeed-realistic",
            subject=(
                "Senior Frontend React TypeScript Engineer "
                "bei Beispiel Digital GmbH"
            ),
            sender="Indeed <donotreply@match.indeed.com>",
        ),
    ]
    assert all(is_likely_job_email(item) for item in messages)
    result = await ZohoMailIngestionWorker(
        FakeZohoAPI(
            messages,
            contents={
                "linkedin-realistic": fixture("linkedin_alert_sanitized.html"),
                "indeed-realistic": fixture("indeed_alert_sanitized.html"),
            },
        )
    ).run(dry_run=False)
    assert result.alert_messages == 2
    assert result.valid_alert_items == 3
    assert result.application_messages == 0
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM email_job_applications").fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM email_application_review_queue"
        ).fetchone()[0] == 0
        assert {
            row[0]
            for row in db.execute("SELECT provider FROM email_job_alert_items")
        } == {"linkedin", "indeed"}
        assert {
            row[0] for row in db.execute("SELECT source FROM jobs")
        } <= {"linkedin_alert", "indeed_alert"}


def test_synthetic_edge_fixture_contains_no_private_values() -> None:
    payload = json.loads(fixture("provider_edge_cases.json"))
    text = json.dumps(payload).lower()
    assert "synthetic" in text
    assert "test user" not in text
    assert "@" not in text
    assert all(
        forbidden.lower() not in text
        for forbidden in ("trackingid", "refid", "lipi", "midtoken", "midsig")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mail_first", [False, True])
@pytest.mark.parametrize("direct_ats", [False, True])
async def test_linkedin_source_alert_cross_path_dedup_in_both_orders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mail_first: bool,
    direct_ats: bool,
) -> None:
    db_path = tmp_path / "cross-path.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_path))
    await init_db()
    parsed = LinkedInAlertParser().parse(
        metadata("linkedin"),
        build_bounded_mail_content(fixture("linkedin_alert_sanitized.html")),
    )
    item = parsed.items[1 if direct_ats else 0]
    mail_job = alert_item_to_job(item)
    source_job = Job.model_validate(
        {
            **mail_job.model_dump(),
            "id": "",
            "content_hash": "",
            "source": "linkedin",
            "url": item.canonical_url,
        }
    )
    ordered = (mail_job, source_job) if mail_first else (source_job, mail_job)
    first = await process_discovered_jobs(
        [JobIngestionCandidate("first", ordered[0])],
        persist=True,
        associate_items=True,
    )
    second = await process_discovered_jobs(
        [JobIngestionCandidate("second", ordered[1])],
        persist=True,
        associate_items=True,
    )
    assert first.item_results[0].status == JobIngestionStatus.SAVED
    assert second.item_results[0].status == JobIngestionStatus.DUPLICATE
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
