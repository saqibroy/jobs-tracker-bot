from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from integrations.zoho_mail import (
    ZohoAccount,
    ZohoFolder,
    ZohoMailIngestionWorker,
    ZohoMessageSummary,
    ZohoOAuthMailClient,
    append_zoho_discovery_candidates,
    clean_message_content,
    detect_ats_link,
    detect_ats_from_message_metadata,
    extract_application_records,
)
from storage.zoho_mail import (
    get_last_successful_sync_at,
    init_zoho_mail_db,
    save_successful_sync_checkpoint,
)


class FakeZohoAPI:
    def __init__(
        self,
        *,
        pages: dict[tuple[str, int], list[ZohoMessageSummary]],
        contents: dict[str, str] | None = None,
        folders: list[ZohoFolder] | None = None,
        fail_after_pages: int | None = None,
    ) -> None:
        self.api_domain = "https://www.zohoapis.eu"
        self.mail_api_base = "https://mail.zoho.eu"
        self.pages = pages
        self.contents = contents or {}
        self.folders = folders or [ZohoFolder("inbox", "Inbox")]
        self.page_calls: list[tuple[str, int, int]] = []
        self.content_calls: list[str] = []
        self.fail_after_pages = fail_after_pages

    async def list_accounts(self) -> list[ZohoAccount]:
        return [ZohoAccount("acct1", "me@example.com")]

    async def list_folders(self, account_id: str) -> list[ZohoFolder]:
        return self.folders

    async def list_messages(
        self, account_id: str, folder_id: str, *, start: int, limit: int
    ) -> list[ZohoMessageSummary]:
        self.page_calls.append((folder_id, start, limit))
        if (
            self.fail_after_pages is not None
            and len(self.page_calls) > self.fail_after_pages
        ):
            raise RuntimeError("boom")
        return self.pages.get((folder_id, start), [])

    async def get_message_content(
        self, account_id: str, folder_id: str, message_id: str
    ) -> str:
        self.content_calls.append(message_id)
        return self.contents.get(message_id, "")

    async def close(self) -> None:
        pass


@pytest.fixture
async def zoho_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "zoho.db")
    monkeypatch.setattr("storage.zoho_mail.config.DATABASE_PATH", db_path)
    monkeypatch.setattr("config.DATABASE_PATH", db_path)
    monkeypatch.setattr("integrations.zoho_mail.config.ZOHO_INITIAL_SYNC_FROM", "")
    monkeypatch.setattr(
        "integrations.zoho_mail.config.ZOHO_DISCOVERY_SEED_FILE",
        str(tmp_path / "zoho_mail_candidates.txt"),
    )
    await init_zoho_mail_db()
    return db_path


def msg(
    message_id: str,
    *,
    days_ago: int = 0,
    subject: str = "Application received for Frontend Engineer",
    summary: str = "",
    sender: str = "jobs@example.com",
) -> ZohoMessageSummary:
    return ZohoMessageSummary(
        message_id=message_id,
        folder_id="inbox",
        folder_name="Inbox",
        subject=subject,
        sender=sender,
        summary=summary,
        message_date=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


@pytest.mark.asyncio
async def test_initial_paginates_until_exhausted_and_skips_default_folders(zoho_db):
    folders = [
        ZohoFolder("inbox", "Inbox"),
        ZohoFolder("custom", "Recruiting"),
        ZohoFolder("trash", "Trash"),
    ]
    api = FakeZohoAPI(
        folders=folders,
        pages={
            ("inbox", 1): [msg("1"), msg("2")],
            ("inbox", 3): [msg("3")],
            ("custom", 1): [msg("4")],
        },
        contents={
            "1": '<a href="https://jobs.ashbyhq.com/acme/123">Apply</a>',
            "2": '<a href="https://boards.greenhouse.io/acme/jobs/1">Apply</a>',
            "3": '<a href="https://jobs.lever.co/acme/1">Apply</a>',
            "4": '<a href="https://apply.workable.com/acme/j/1">Apply</a>',
        },
    )
    result = await ZohoMailIngestionWorker(api, page_limit=2).run(dry_run=False)
    assert result.messages_seen == 4
    assert result.full_messages_fetched == 4
    assert ("trash", 1, 2) not in api.page_calls
    assert await get_last_successful_sync_at("acct1") is not None


@pytest.mark.asyncio
async def test_subsequent_sync_uses_overlap_and_stops_old_pages(zoho_db):
    await save_successful_sync_checkpoint(
        "acct1",
        synced_at=datetime.now(timezone.utc),
        api_domain="https://www.zohoapis.eu",
    )
    api = FakeZohoAPI(
        pages={
            ("inbox", 1): [msg("new", days_ago=1), msg("old", days_ago=5)],
            ("inbox", 3): [msg("too-old", days_ago=6)],
        },
        contents={"new": '<a href="https://jobs.ashbyhq.com/acme/123">Apply</a>'},
    )
    result = await ZohoMailIngestionWorker(api, page_limit=2, overlap_hours=48).run(
        dry_run=False
    )
    assert result.messages_seen == 1
    assert api.page_calls == [("inbox", 1, 2), ("inbox", 3, 2)]


@pytest.mark.asyncio
async def test_duplicate_and_moved_messages_are_rerunnable(zoho_db):
    api = FakeZohoAPI(
        folders=[ZohoFolder("inbox", "Inbox"), ZohoFolder("archive", "Archive")],
        pages={
            ("inbox", 1): [msg("same")],
            ("archive", 1): [msg("same")],
        },
        contents={"same": '<a href="https://jobs.ashbyhq.com/acme/123">Apply</a>'},
    )
    await ZohoMailIngestionWorker(api, page_limit=200).run(dry_run=False)
    await ZohoMailIngestionWorker(api, page_limit=200).run(dry_run=False)
    with sqlite3.connect(zoho_db) as db:
        assert db.execute("SELECT COUNT(*) FROM zoho_mail_messages").fetchone()[0] == 1
        assert (
            db.execute("SELECT folder_name FROM zoho_mail_messages").fetchone()[0]
            == "Archive"
        )


@pytest.mark.asyncio
async def test_interrupted_run_does_not_advance_checkpoint(zoho_db):
    api = FakeZohoAPI(
        pages={("inbox", 1): [msg("1")], ("inbox", 2): [msg("2")]},
        contents={"1": '<a href="https://jobs.ashbyhq.com/acme/123">Apply</a>'},
        fail_after_pages=1,
    )
    with pytest.raises(RuntimeError):
        await ZohoMailIngestionWorker(api, page_limit=1).run(dry_run=False)
    assert await get_last_successful_sync_at("acct1") is None


@pytest.mark.asyncio
async def test_oauth_refresh_stores_api_domain_and_access_token(tmp_path, monkeypatch):
    token_path = tmp_path / "token.json"
    monkeypatch.setattr("integrations.zoho_mail.config.ZOHO_CLIENT_ID", "cid")
    monkeypatch.setattr("integrations.zoho_mail.config.ZOHO_CLIENT_SECRET", "secret")
    monkeypatch.setattr("integrations.zoho_mail.config.ZOHO_REFRESH_TOKEN", "refresh")
    monkeypatch.setattr(
        "integrations.zoho_mail.config.ZOHO_ACCOUNTS_URL", "https://accounts.zoho.eu"
    )
    monkeypatch.setattr(
        "integrations.zoho_mail.config.ZOHO_OAUTH_TOKEN_FILE", str(token_path)
    )
    monkeypatch.setattr("integrations.zoho_mail.config.ZOHO_MAIL_API_BASE", "")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth/v2/token"
        return httpx.Response(
            200,
            json={
                "access_token": "access",
                "expires_in": 3600,
                "api_domain": "https://www.zohoapis.eu",
            },
        )

    client = ZohoOAuthMailClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    headers = await client._headers()
    await client.close()
    assert headers["Authorization"] == "Zoho-oauthtoken access"
    payload = json.loads(token_path.read_text())
    assert payload["api_domain"] == "https://www.zohoapis.eu"
    assert payload["mail_api_base"] == "https://mail.zoho.eu"


def test_company_extraction_detects_supported_ats_links():
    examples = {
        "personio": "https://researchgate.jobs.personio.de/job/1",
        "ashby": "https://jobs.ashbyhq.com/acme/123",
        "greenhouse": "https://boards.greenhouse.io/acme/jobs/1",
        "lever": "https://jobs.lever.co/acme/1",
        "workable": "https://apply.workable.com/acme/j/1",
        "bamboohr": "https://acme.bamboohr.com/careers/1",
        "teamtailor": "https://acme.teamtailor.com/jobs/1",
        "smartrecruiters": "https://jobs.smartrecruiters.com/acme/1",
        "recruitee": "https://acme.recruitee.com/o/frontend",
        "join": "https://join.com/companies/acme/jobs/1",
        "onlyfy": "https://jobs.onlyfy.io/acme/1",
        "softgarden": "https://acme.softgarden.io/job/1",
        "workday": "https://acme.wd3.myworkdayjobs.com/acme/job/1",
        "sap_successfactors": "https://career012.successfactors.eu/career?company=acme",
    }
    for expected, url in examples.items():
        detected = detect_ats_link(url)
        assert detected is not None, url
        assert detected.ats == expected
        assert detected.slug


def test_cleaning_removes_tracking_pixels_signatures_and_extracts_review_record():
    html = """
    <p>Thank you for applying for Senior Frontend Engineer.</p>
    <img width="1" height="1" src="https://track.example/pixel">
    <p>Best regards</p><p>Recruiting Team</p>
    <blockquote>old quoted mail</blockquote>
    """
    cleaned = clean_message_content(html)
    assert "track.example" not in cleaned
    assert "old quoted mail" not in cleaned
    message = msg(
        "m1", summary="Application received", sender="Recruiting <jobs@company.de>"
    )
    records = extract_application_records(
        account_id="acct1", message=message, cleaned_content=cleaned
    )
    assert records[0].needs_review is True
    assert records[0].status == "applied"


def test_metadata_detection_for_application_confirmation_senders():
    cases = [
        (
            msg(
                "m1",
                sender="joanne@eternohealthgmbh.teamtailor-mail.com",
                subject="ETERNO ♾️ Thank you for your application!",
            ),
            "teamtailor",
            "eternohealthgmbh",
        ),
        (
            msg(
                "m2",
                sender="e+abc.amperecloud@recruitee-mailbox.com",
                subject="[Amperecloud] Thanks a lot for your application",
            ),
            "recruitee",
            "amperecloud",
        ),
        (
            msg(
                "m3",
                sender="no-reply@eu.greenhouse-mail.io",
                subject="emnify | Application received | Senior Software Engineer",
            ),
            "greenhouse",
            "emnify",
        ),
        (
            msg(
                "m5",
                sender="no-reply@eu.greenhouse-mail.io",
                subject="Thanks for Applying to Join think-cell — We’re Excited to Connect!",
            ),
            "greenhouse",
            "think-cell",
        ),
        (
            msg(
                "m4",
                sender="Covestro@myworkday.com",
                subject="Application Received for GenAI Engineer",
            ),
            "workday",
            "covestro",
        ),
    ]
    for message, ats, slug in cases:
        detections = detect_ats_from_message_metadata(message)
        assert detections
        assert detections[0].ats == ats
        assert detections[0].slug == slug


def test_zoho_discovery_candidates_are_written_as_seed_lines(tmp_path):
    record = extract_application_records(
        account_id="acct1",
        message=msg(
            "m1",
            sender="joanne@newcompanygmbh.teamtailor-mail.com",
            subject="Thank you for your application!",
        ),
        cleaned_content="Thank you for applying.",
    )[0]
    path = tmp_path / "zoho_mail_candidates.txt"
    appended = append_zoho_discovery_candidates([record], path=path)
    assert appended == 1
    text = path.read_text(encoding="utf-8")
    assert "source=zoho_mail" in text
    assert "jsonld:newcompanygmbh https://newcompanygmbh.teamtailor.com" in text
    assert append_zoho_discovery_candidates([record], path=path) == 0
