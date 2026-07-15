from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from tools.zoho_setup_common import (
    READ_ONLY_SCOPES,
    ZohoSetupConfig,
    build_authorization_url,
    exchange_authorization_code,
    get_accounts,
    get_folders,
    read_one_email_probe,
    update_env,
)


def test_authorization_url_uses_read_only_scopes():
    url = build_authorization_url(
        ZohoSetupConfig(
            client_id="cid",
            client_secret="secret",
            accounts_url="https://accounts.zoho.eu",
            redirect_uri="http://localhost",
        )
    )
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "accounts.zoho.eu"
    assert query["client_id"] == ["cid"]
    assert query["response_type"] == ["code"]
    assert query["access_type"] == ["offline"]
    assert query["scope"] == [",".join(READ_ONLY_SCOPES)]


def test_update_env_preserves_comments_and_upserts(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("# hello\nFOO=old\n", encoding="utf-8")
    update_env({"FOO": "new", "ZOHO_ACCOUNT_ID": "123"}, path=env_path)
    text = env_path.read_text(encoding="utf-8")
    assert "# hello" in text
    assert "FOO=new" in text
    assert "ZOHO_ACCOUNT_ID=123" in text


@pytest.mark.asyncio
async def test_exchange_code_and_api_probes_use_expected_endpoints():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/oauth/v2/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "api_domain": "https://www.zohoapis.eu",
                    "expires_in": 3600,
                },
            )
        if request.url.path == "/api/accounts":
            return httpx.Response(
                200,
                json={
                    "data": [{"accountId": "acct1", "mailboxAddress": "me@example.com"}]
                },
            )
        if request.url.path == "/api/accounts/acct1/folders":
            return httpx.Response(
                200,
                json={"data": [{"folderId": "inbox", "folderName": "Inbox"}]},
            )
        if request.url.path == "/api/accounts/acct1/messages/view":
            assert request.url.params["attachedMails"] == "false"
            return httpx.Response(
                200,
                json={"data": [{"messageId": "m1", "subject": "Application received"}]},
            )
        if request.url.path == "/api/accounts/acct1/folders/inbox/messages/m1/content":
            return httpx.Response(200, json={"data": {"content": "hello"}})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        token = await exchange_authorization_code(
            ZohoSetupConfig(
                client_id="cid",
                client_secret="secret",
                accounts_url="https://accounts.zoho.eu",
                redirect_uri="http://localhost",
            ),
            "code",
            http=client,
        )
        accounts = await get_accounts(
            access_token=token.access_token,
            mail_api_base=token.mail_api_base,
            http=client,
        )
        folders = await get_folders(
            access_token=token.access_token,
            mail_api_base=token.mail_api_base,
            account_id=accounts[0].account_id,
            http=client,
        )
        probe = await read_one_email_probe(
            access_token=token.access_token,
            mail_api_base=token.mail_api_base,
            account_id=accounts[0].account_id,
            http=client,
        )

    assert token.refresh_token == "refresh"
    assert token.mail_api_base == "https://mail.zoho.eu"
    assert accounts[0].account_id == "acct1"
    assert folders[0].name == "Inbox"
    assert probe is not None
    assert probe.message_id == "m1"
    assert [request.url.path for request in requests] == [
        "/oauth/v2/token",
        "/api/accounts",
        "/api/accounts/acct1/folders",
        "/api/accounts/acct1/folders",
        "/api/accounts/acct1/messages/view",
        "/api/accounts/acct1/folders/inbox/messages/m1/content",
    ]
