"""Helpers for interactive Zoho Mail OAuth setup.

These functions intentionally avoid printing secrets. They are used by the
interactive setup command and by the small one-step helper scripts in
``tools/`` so users never need to hand-write curl commands.
"""

from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
TOKEN_PATH_DEFAULT = "./data/private/zoho_oauth_token.json"

READ_ONLY_SCOPES = (
    "ZohoMail.accounts.READ",
    "ZohoMail.folders.READ",
    "ZohoMail.messages.READ",
)

ZOHO_DATA_CENTERS = {
    "eu": "https://accounts.zoho.eu",
    "com": "https://accounts.zoho.com",
    "in": "https://accounts.zoho.in",
    "au": "https://accounts.zoho.com.au",
    "jp": "https://accounts.zoho.jp",
    "ca": "https://accounts.zohocloud.ca",
}


@dataclass(frozen=True)
class ZohoSetupConfig:
    client_id: str
    client_secret: str
    accounts_url: str
    redirect_uri: str = ""
    token_file: str = TOKEN_PATH_DEFAULT


@dataclass(frozen=True)
class ZohoTokenResult:
    access_token: str
    refresh_token: str
    api_domain: str
    mail_api_base: str
    expires_in: int


@dataclass(frozen=True)
class ZohoAccountInfo:
    account_id: str
    email: str = ""


@dataclass(frozen=True)
class ZohoFolderInfo:
    folder_id: str
    name: str
    folder_type: str = ""


@dataclass(frozen=True)
class ZohoMessageProbe:
    account_id: str
    folder_id: str
    folder_name: str
    message_id: str
    subject: str
    content_length: int


def load_env(path: Path = ENV_PATH) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def update_env(updates: dict[str, str], path: Path = ENV_PATH) -> None:
    """Upsert environment values while preserving unrelated comments/lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = (
        path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if path.exists()
        else []
    )
    remaining = dict(updates)
    output: list[str] = []
    for raw in existing_lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw:
            output.append(raw)
            continue
        key = raw.split("=", 1)[0].strip()
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(raw)
    if remaining:
        if output and output[-1].strip():
            output.append("")
        output.append("# Zoho Mail OAuth")
        for key, value in remaining.items():
            output.append(f"{key}={value}")

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    tmp.replace(path)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def build_authorization_url(config: ZohoSetupConfig, *, state: str = "job-bot") -> str:
    params = {
        "scope": ",".join(READ_ONLY_SCOPES),
        "client_id": config.client_id,
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    if config.redirect_uri:
        params["redirect_uri"] = config.redirect_uri
    query = urlencode(params)
    return f"{config.accounts_url.rstrip('/')}/oauth/v2/auth?{query}"


def derive_mail_api_base(accounts_url: str, api_domain: str = "") -> str:
    candidates = " ".join([accounts_url, api_domain]).lower()
    for suffix in ("eu", "in", "com.au", "jp", "ca", "com"):
        if f"zoho.{suffix}" in candidates or f"zohoapis.{suffix}" in candidates:
            return f"https://mail.zoho.{suffix}"
    if "zohocloud.ca" in candidates:
        return "https://mail.zoho.ca"
    return "https://mail.zoho.com"


def save_token_cache(
    token: ZohoTokenResult,
    *,
    token_file: str = TOKEN_PATH_DEFAULT,
) -> None:
    path = ROOT / token_file if not Path(token_file).is_absolute() else Path(token_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "access_token": token.access_token,
        "api_domain": token.api_domain,
        "mail_api_base": token.mail_api_base,
        "expires_at": time.time() + token.expires_in,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(path)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _rows(payload: object, nested_key: str) -> list[dict]:
    if isinstance(payload, dict):
        data = payload.get("data", payload)
    else:
        data = payload
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in (nested_key, "list", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        return [data]
    return []


async def exchange_authorization_code(
    config: ZohoSetupConfig,
    code: str,
    *,
    http: httpx.AsyncClient | None = None,
) -> ZohoTokenResult:
    owns_http = http is None
    client = http or httpx.AsyncClient(timeout=30)
    try:
        data = {
            "code": code.strip(),
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "grant_type": "authorization_code",
        }
        if config.redirect_uri:
            data["redirect_uri"] = config.redirect_uri
        response = await client.post(
            f"{config.accounts_url.rstrip('/')}/oauth/v2/token",
            data=data,
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_http:
            await client.aclose()

    if "refresh_token" not in payload:
        raise RuntimeError(
            "Zoho did not return a refresh_token. Re-open the authorization URL "
            "and approve consent again; make sure access_type=offline is present."
        )
    if "access_token" not in payload:
        raise RuntimeError(f"Zoho token exchange failed: {payload}")

    api_domain = str(payload.get("api_domain") or config.accounts_url)
    mail_api_base = derive_mail_api_base(config.accounts_url, api_domain)
    return ZohoTokenResult(
        access_token=str(payload["access_token"]),
        refresh_token=str(payload["refresh_token"]),
        api_domain=api_domain,
        mail_api_base=mail_api_base,
        expires_in=int(payload.get("expires_in") or 3600),
    )


async def refresh_access_token(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    accounts_url: str,
    http: httpx.AsyncClient | None = None,
) -> ZohoTokenResult:
    owns_http = http is None
    client = http or httpx.AsyncClient(timeout=30)
    try:
        response = await client.post(
            f"{accounts_url.rstrip('/')}/oauth/v2/token",
            data={
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
            },
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_http:
            await client.aclose()

    if "access_token" not in payload:
        raise RuntimeError(f"Zoho refresh failed: {payload}")
    api_domain = str(payload.get("api_domain") or accounts_url)
    mail_api_base = derive_mail_api_base(accounts_url, api_domain)
    return ZohoTokenResult(
        access_token=str(payload["access_token"]),
        refresh_token=refresh_token,
        api_domain=api_domain,
        mail_api_base=mail_api_base,
        expires_in=int(payload.get("expires_in") or 3600),
    )


async def get_accounts(
    *,
    access_token: str,
    mail_api_base: str,
    http: httpx.AsyncClient | None = None,
) -> list[ZohoAccountInfo]:
    owns_http = http is None
    client = http or httpx.AsyncClient(timeout=30)
    try:
        response = await client.get(
            f"{mail_api_base.rstrip('/')}/api/accounts",
            headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_http:
            await client.aclose()
    accounts: list[ZohoAccountInfo] = []
    for row in _rows(payload, "accounts"):
        account_id = str(
            row.get("accountId") or row.get("account_id") or row.get("id") or ""
        )
        if account_id:
            accounts.append(
                ZohoAccountInfo(
                    account_id=account_id,
                    email=str(
                        row.get("mailboxAddress") or row.get("emailAddress") or ""
                    ),
                )
            )
    return accounts


async def get_folders(
    *,
    access_token: str,
    mail_api_base: str,
    account_id: str,
    http: httpx.AsyncClient | None = None,
) -> list[ZohoFolderInfo]:
    owns_http = http is None
    client = http or httpx.AsyncClient(timeout=30)
    try:
        response = await client.get(
            f"{mail_api_base.rstrip('/')}/api/accounts/{account_id}/folders",
            headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_http:
            await client.aclose()
    folders: list[ZohoFolderInfo] = []
    for row in _rows(payload, "folders"):
        folder_id = str(
            row.get("folderId") or row.get("folder_id") or row.get("id") or ""
        )
        name = str(row.get("folderName") or row.get("name") or "")
        if folder_id and name:
            folders.append(
                ZohoFolderInfo(
                    folder_id=folder_id,
                    name=name,
                    folder_type=str(row.get("folderType") or row.get("type") or ""),
                )
            )
    return folders


async def read_one_email_probe(
    *,
    access_token: str,
    mail_api_base: str,
    account_id: str,
    folder_id: str | None = None,
    http: httpx.AsyncClient | None = None,
) -> ZohoMessageProbe | None:
    owns_http = http is None
    client = http or httpx.AsyncClient(timeout=30)
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    try:
        folders = await get_folders(
            access_token=access_token,
            mail_api_base=mail_api_base,
            account_id=account_id,
            http=client,
        )
        selected = next(
            (
                folder
                for folder in folders
                if folder_id is None and folder.name.lower() == "inbox"
            ),
            None,
        )
        if folder_id:
            selected = next(
                (folder for folder in folders if folder.folder_id == folder_id), None
            )
        selected = selected or next(iter(folders), None)
        if selected is None:
            return None

        list_response = await client.get(
            f"{mail_api_base.rstrip('/')}/api/accounts/{account_id}/messages/view",
            params={
                "folderId": selected.folder_id,
                "start": 1,
                "limit": 1,
                "sortBy": "date",
                "sortorder": "false",
                "attachedMails": "false",
                "inlinedMails": "false",
            },
            headers=headers,
        )
        list_response.raise_for_status()
        messages = _rows(list_response.json(), "messages")
        if not messages:
            return None
        first = messages[0]
        message_id = str(
            first.get("messageId") or first.get("message_id") or first.get("id") or ""
        )
        if not message_id:
            return None
        content_response = await client.get(
            f"{mail_api_base.rstrip('/')}/api/accounts/{account_id}/folders/{selected.folder_id}/messages/{message_id}/content",
            headers=headers,
        )
        content_response.raise_for_status()
        content_text = content_response.text
        subject = str(first.get("subject") or "")
        return ZohoMessageProbe(
            account_id=account_id,
            folder_id=selected.folder_id,
            folder_name=selected.name,
            message_id=message_id,
            subject=subject,
            content_length=len(content_text),
        )
    finally:
        if owns_http:
            await client.aclose()


def config_from_env(path: Path = ENV_PATH) -> dict[str, str]:
    values = load_env(path)
    return {
        "client_id": values.get("ZOHO_CLIENT_ID", ""),
        "client_secret": values.get("ZOHO_CLIENT_SECRET", ""),
        "refresh_token": values.get("ZOHO_REFRESH_TOKEN", ""),
        "account_id": values.get("ZOHO_ACCOUNT_ID", ""),
        "accounts_url": values.get("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.eu"),
        "mail_api_base": values.get("ZOHO_MAIL_API_BASE", ""),
        "token_file": values.get("ZOHO_OAUTH_TOKEN_FILE", TOKEN_PATH_DEFAULT),
        "redirect_uri": values.get("ZOHO_REDIRECT_URI", "http://localhost"),
    }
