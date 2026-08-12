"""Production-safe Zoho Mail application and job-alert ingestion worker.

The worker is deliberately read-only against Zoho. It stores only bounded
normalized metadata, never attachments, and fetches full message content only
after cheap header/summary checks indicate a likely job email.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup
from loguru import logger

import config
from integrations.job_alerts import (
    AlertParserRegistry,
    MailIntent,
    MailMessageMetadata,
)
from integrations.job_alerts.contracts import (
    MAX_ALERT_ITEMS_PER_SYNC,
    MAX_LINKS,
    MAX_SUMMARY,
    MAX_URL,
    bounded_text,
    bounded_values,
)
from integrations.job_alerts.message import build_bounded_mail_content
from integrations.job_alerts.service import (
    ApplicationProcessingResult,
    MailTransportEnvelope,
    SharedMailPostFetchProcessor,
    is_current_processing_state,
    is_likely_job_message,
    is_stale_legacy_processing_state,
)
from job_ingestion import process_discovered_jobs
from notifiers.delivery import process_pending_immediate_deliveries
from storage.database import init_db
from storage.zoho_mail import (
    cleanup_processed_alert_items,
    complete_alert_item_results,
    enqueue_review,
    get_message_processing_state,
    get_last_successful_sync_at,
    init_zoho_mail_db,
    mark_message_processed,
    save_successful_sync_checkpoint,
    set_message_routing,
    upsert_application_record,
    upsert_message_summary,
)
from sources.registry import load_company_boards

SKIPPED_FOLDER_NAMES = {name.lower() for name in config.ZOHO_SKIP_FOLDERS}
@dataclass(frozen=True)
class ZohoAccount:
    account_id: str
    email: str = ""


@dataclass(frozen=True)
class ZohoFolder:
    folder_id: str
    name: str
    folder_type: str = ""


@dataclass(frozen=True)
class ZohoMessageSummary:
    message_id: str
    folder_id: str
    folder_name: str
    subject: str = ""
    sender: str = ""
    summary: str = ""
    message_date: datetime | None = None
    links: tuple[str, ...] = ()


@dataclass(frozen=True)
class DetectedATS:
    ats: str
    slug: str
    board_url: str
    original_job_url: str
    company_name: str = ""
    company_domain: str = ""
    confidence: float = 0.85
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractedApplicationRecord:
    account_id: str
    message_id: str
    company_name: str = ""
    company_domain: str = ""
    ats: str = ""
    ats_slug: str = ""
    ats_board_url: str = ""
    original_job_url: str = ""
    job_title: str = ""
    application_date: str = ""
    status: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    needs_review: bool = True


@dataclass(frozen=True)
class ZohoSyncResult:
    dry_run: bool
    accounts: int = 0
    folders: int = 0
    messages_seen: int = 0
    full_messages_fetched: int = 0
    extracted_records: int = 0
    review_records: int = 0
    discovery_candidates: int = 0
    checkpoint_advanced: bool = False
    application_messages: int = 0
    alert_messages: int = 0
    unknown_job_messages: int = 0
    parsed_alert_items: int = 0
    valid_alert_items: int = 0
    invalid_alert_items: int = 0
    pending_alert_items: int = 0
    processed_alert_items: int = 0
    provider_failures: int = 0
    pipeline_accepted: int = 0
    pipeline_rejected: int = 0
    current_version_skipped: int = 0
    backlog_deferred: int = 0
    provider_health: tuple[str, ...] = ()


class ZohoMailAPI(Protocol):
    api_domain: str
    mail_api_base: str

    async def list_accounts(self) -> list[ZohoAccount]: ...

    async def list_folders(self, account_id: str) -> list[ZohoFolder]: ...

    async def list_messages(
        self,
        account_id: str,
        folder_id: str,
        *,
        start: int,
        limit: int,
    ) -> list[ZohoMessageSummary]: ...

    async def get_message_content(
        self,
        account_id: str,
        folder_id: str,
        message_id: str,
    ) -> str: ...

    async def close(self) -> None: ...


def parse_zoho_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        # Zoho often emits milliseconds since epoch.
        seconds = float(value) / 1000 if value > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return parse_zoho_datetime(int(text))
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%d %b %Y %H:%M:%S %z"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_initial_sync_from(value: str | None = None) -> datetime | None:
    raw = value if value is not None else config.ZOHO_INITIAL_SYNC_FROM
    if not raw:
        return None
    dt = parse_zoho_datetime(raw)
    if dt is None:
        raise ValueError(f"Invalid ZOHO_INITIAL_SYNC_FROM timestamp: {raw!r}")
    return dt


def _token_file_path() -> Path:
    return Path(config.ZOHO_OAUTH_TOKEN_FILE)


def _secure_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(path)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _derive_mail_base(api_domain: str = "") -> str:
    if config.ZOHO_MAIL_API_BASE:
        return config.ZOHO_MAIL_API_BASE

    candidates = " ".join([api_domain, config.ZOHO_ACCOUNTS_URL]).lower()
    for suffix in ("eu", "in", "com.au", "jp", "ca", "com"):
        if f"zoho.{suffix}" in candidates or f"zohoapis.{suffix}" in candidates:
            return f"https://mail.zoho.{suffix}"
    return "https://mail.zoho.com"


class ZohoOAuthMailClient:
    """Async Zoho Mail REST client with refresh-token OAuth."""

    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        *,
        allow_token_cache_write: bool = True,
    ) -> None:
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(config.HTTP_TIMEOUT)
        )
        self._owns_http = http is None
        self._allow_token_cache_write = bool(allow_token_cache_write)
        self._refreshed_in_memory = False
        self._token: dict[str, Any] = self._load_token()
        self.api_domain = str(self._token.get("api_domain") or "")
        self.mail_api_base = _derive_mail_base(self.api_domain)

    def set_token_cache_write_allowed(
        self,
        allowed: bool,
        *,
        persist_current: bool = False,
    ) -> None:
        """Set explicit cache policy after effective sync mode is known."""

        self._allow_token_cache_write = bool(allowed)
        if (
            self._allow_token_cache_write
            and persist_current
            and self._refreshed_in_memory
            and self._token.get("access_token")
        ):
            _secure_write_json(_token_file_path(), self._token)
            self._refreshed_in_memory = False

    def _load_token(self) -> dict[str, Any]:
        path = _token_file_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Ignoring unreadable Zoho OAuth token cache at {}", path)
            return {}

    def _token_expired(self) -> bool:
        expires_at = float(self._token.get("expires_at") or 0)
        return (
            not self._token.get("access_token")
            or expires_at <= datetime.now(timezone.utc).timestamp() + 120
        )

    async def _refresh_access_token(self) -> str:
        if not (
            config.ZOHO_CLIENT_ID
            and config.ZOHO_CLIENT_SECRET
            and config.ZOHO_REFRESH_TOKEN
        ):
            raise RuntimeError(
                "Zoho OAuth config missing. Set ZOHO_CLIENT_ID, "
                "ZOHO_CLIENT_SECRET and ZOHO_REFRESH_TOKEN."
            )

        response = await self._http.post(
            f"{config.ZOHO_ACCOUNTS_URL}/oauth/v2/token",
            data={
                "refresh_token": config.ZOHO_REFRESH_TOKEN,
                "client_id": config.ZOHO_CLIENT_ID,
                "client_secret": config.ZOHO_CLIENT_SECRET,
                "grant_type": "refresh_token",
            },
        )
        response.raise_for_status()
        payload = response.json()
        if "access_token" not in payload:
            raise RuntimeError(
                f"Zoho OAuth refresh did not return access_token: {payload}"
            )

        expires_in = int(payload.get("expires_in") or 3600)
        self.api_domain = str(
            payload.get("api_domain") or self.api_domain or config.ZOHO_ACCOUNTS_URL
        )
        self.mail_api_base = _derive_mail_base(self.api_domain)
        self._token = {
            "access_token": payload["access_token"],
            "api_domain": self.api_domain,
            "mail_api_base": self.mail_api_base,
            "expires_at": datetime.now(timezone.utc).timestamp() + expires_in,
        }
        self._refreshed_in_memory = True
        if self._allow_token_cache_write:
            _secure_write_json(_token_file_path(), self._token)
            self._refreshed_in_memory = False
        return str(payload["access_token"])

    async def _headers(self) -> dict[str, str]:
        if self._token_expired():
            await self._refresh_access_token()
        return {"Authorization": f"Zoho-oauthtoken {self._token['access_token']}"}

    async def _get_json(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> Any:
        url = f"{self.mail_api_base.rstrip('/')}/{path.lstrip('/')}"
        response = await self._http.get(
            url, params=params, headers=await self._headers()
        )
        if response.status_code == 401:
            await self._refresh_access_token()
            response = await self._http.get(
                url, params=params, headers=await self._headers()
            )
        response.raise_for_status()
        return response.json()

    async def list_accounts(self) -> list[ZohoAccount]:
        payload = await self._get_json("/api/accounts")
        rows = _extract_rows(payload, "accounts")
        accounts: list[ZohoAccount] = []
        for row in rows:
            account_id = str(
                row.get("accountId") or row.get("account_id") or row.get("id") or ""
            )
            if account_id:
                accounts.append(
                    ZohoAccount(
                        account_id=account_id,
                        email=str(
                            row.get("mailboxAddress") or row.get("emailAddress") or ""
                        ),
                    )
                )
        if config.ZOHO_ACCOUNT_ID:
            return [
                account
                for account in accounts
                if account.account_id == config.ZOHO_ACCOUNT_ID
            ]
        return accounts

    async def list_folders(self, account_id: str) -> list[ZohoFolder]:
        payload = await self._get_json(f"/api/accounts/{account_id}/folders")
        rows = _extract_rows(payload, "folders")
        folders: list[ZohoFolder] = []
        for row in rows:
            folder_id = str(
                row.get("folderId") or row.get("folder_id") or row.get("id") or ""
            )
            name = str(row.get("folderName") or row.get("name") or "")
            if folder_id and name:
                folders.append(
                    ZohoFolder(
                        folder_id=folder_id,
                        name=name,
                        folder_type=str(row.get("folderType") or row.get("type") or ""),
                    )
                )
        return folders

    async def list_messages(
        self,
        account_id: str,
        folder_id: str,
        *,
        start: int,
        limit: int,
    ) -> list[ZohoMessageSummary]:
        payload = await self._get_json(
            f"/api/accounts/{account_id}/messages/view",
            params={
                "folderId": folder_id,
                "start": start,
                "limit": limit,
                "sortBy": "date",
                "sortorder": "false",
                "attachedMails": "false",
                "inlinedMails": "false",
            },
        )
        rows = _extract_rows(payload, "messages")
        messages: list[ZohoMessageSummary] = []
        for row in rows:
            message_id = str(
                row.get("messageId") or row.get("message_id") or row.get("id") or ""
            )
            if not message_id:
                continue
            messages.append(
                ZohoMessageSummary(
                    message_id=message_id,
                    folder_id=folder_id,
                    folder_name="",
                    subject=bounded_text(row.get("subject"), 500),
                    sender=bounded_text(
                        row.get("sender")
                        or row.get("fromAddress")
                        or row.get("from")
                        or "",
                        320,
                    ),
                    summary=bounded_text(
                        row.get("summary") or row.get("snippet"),
                        MAX_SUMMARY,
                    ),
                    message_date=parse_zoho_datetime(
                        row.get("receivedTime")
                        or row.get("sentDateInGMT")
                        or row.get("receivedTimeMs")
                        or row.get("time")
                    ),
                    links=tuple(
                        _extract_links(
                            " ".join(
                                str(row.get(key) or "")[:MAX_SUMMARY]
                                for key in ("summary", "snippet", "content", "html")
                            )
                        )
                    ),
                )
            )
        return messages

    async def get_message_content(
        self, account_id: str, folder_id: str, message_id: str
    ) -> str:
        payload = await self._get_json(
            f"/api/accounts/{account_id}/folders/{folder_id}/messages/{message_id}/content"
        )
        if isinstance(payload, dict):
            data = payload.get("data", payload)
            if isinstance(data, dict):
                for key in ("content", "html", "body", "messageContent"):
                    if data.get(key):
                        return str(data[key])
            if isinstance(data, str):
                return data
        return str(payload)

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()


def _extract_rows(payload: Any, nested_key: str) -> list[dict[str, Any]]:
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in (nested_key, "list", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        return [data]
    return []


def should_process_folder(folder: ZohoFolder) -> bool:
    name = folder.name.strip().lower()
    folder_type = folder.folder_type.strip().lower()
    if name in SKIPPED_FOLDER_NAMES or folder_type in SKIPPED_FOLDER_NAMES:
        return False
    return True


def is_likely_job_email(message: ZohoMessageSummary) -> bool:
    return is_likely_job_message(
        MailMessageMetadata(
            account_id="",
            message_id=message.message_id,
            folder_id=message.folder_id,
            folder_name=message.folder_name,
            subject=message.subject,
            sender=message.sender,
            summary=message.summary,
            message_date=message.message_date,
        ),
        links=message.links,
    )


def clean_message_content(content: str) -> str:
    """Remove HTML noise, tracking pixels, signatures and quoted history."""
    return build_bounded_mail_content(content).cleaned_text


URL_RE = re.compile(r"https?://[^\s<>'\")]+", re.I)


def _extract_links(text: str) -> list[str]:
    soup = BeautifulSoup(text or "", "html.parser")
    links: list[str] = []
    for tag in soup.find_all("a", limit=MAX_LINKS):
        href = tag.get("href")
        if href:
            links.append(str(href))
        if len(links) >= MAX_LINKS:
            break
    if len(links) < MAX_LINKS:
        for match in URL_RE.finditer(text or ""):
            links.append(match.group(0))
            if len(links) >= MAX_LINKS:
                break
    cleaned: list[str] = []
    seen: set[str] = set()
    for link in links:
        link = unquote(link[:MAX_URL]).rstrip(".,);]")[:MAX_URL]
        if link not in seen:
            seen.add(link)
            cleaned.append(link)
        if len(cleaned) >= MAX_LINKS:
            break
    return cleaned


def _display_name(slug: str) -> str:
    cleaned = re.sub(r"\.(com|de|io|ai|co|org|net)$", "", slug.strip(), flags=re.I)
    cleaned = cleaned.replace("_", "-").replace(".", "-")
    return " ".join(part.capitalize() for part in cleaned.split("-") if part) or slug


def _path_part(path: str, index: int = 0) -> str:
    parts = [unquote(part) for part in path.split("/") if part]
    return parts[index] if len(parts) > index else ""


def detect_ats_link(url: str) -> DetectedATS | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path

    def candidate(
        ats: str,
        slug: str,
        board_url: str,
        confidence: float = 0.9,
        company_name: str = "",
    ) -> DetectedATS | None:
        if not slug:
            return None
        company = company_name or _display_name(slug)
        return DetectedATS(
            ats=ats,
            slug=slug,
            board_url=board_url.rstrip("/"),
            original_job_url=url,
            company_name=company,
            company_domain=host,
            confidence=confidence,
            evidence={"detected_from_url": url, "host": host},
        )

    if host == "jobs.ashbyhq.com":
        slug = _path_part(path)
        return candidate("ashby", slug, f"https://jobs.ashbyhq.com/{slug}")
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        slug = _path_part(path).lower()
        return candidate("greenhouse", slug, f"https://boards.greenhouse.io/{slug}")
    if host.endswith(".jobs.personio.de"):
        slug = host.removesuffix(".jobs.personio.de").lower()
        return candidate("personio", slug, f"https://{slug}.jobs.personio.de")
    if host == "jobs.lever.co":
        slug = _path_part(path).lower()
        return candidate("lever", slug, f"https://jobs.lever.co/{slug}")
    if host == "apply.workable.com":
        slug = _path_part(path).lower()
        return candidate("workable", slug, f"https://apply.workable.com/{slug}")
    if host.endswith(".bamboohr.com"):
        slug = host.removesuffix(".bamboohr.com").lower()
        return candidate("bamboohr", slug, f"https://{slug}.bamboohr.com/careers")
    if host.endswith(".teamtailor.com"):
        slug = host.removesuffix(".teamtailor.com").lower()
        return candidate("teamtailor", slug, f"https://{slug}.teamtailor.com")
    if host == "jobs.smartrecruiters.com":
        slug = _path_part(path).lower()
        return candidate(
            "smartrecruiters", slug, f"https://jobs.smartrecruiters.com/{slug}"
        )
    if host.endswith(".recruitee.com"):
        slug = host.removesuffix(".recruitee.com").lower()
        return candidate("recruitee", slug, f"https://{slug}.recruitee.com")
    if host == "careers.recruitee.com":
        slug = _path_part(path).lower()
        return candidate("recruitee", slug, f"https://{slug}.recruitee.com")
    if host.endswith(".join.com") and host != "join.com":
        slug = host.removesuffix(".join.com").lower()
        return candidate("join", slug, f"https://{slug}.join.com")
    if host == "join.com":
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) >= 2 and parts[0].lower() == "companies":
            slug = parts[1].lower()
            return candidate("join", slug, f"https://join.com/companies/{slug}")
    if "onlyfy" in host:
        slug = _path_part(path).lower() or host.split(".")[0]
        return candidate("onlyfy", slug, f"https://{host}", 0.82)
    if "softgarden" in host:
        slug = (
            host.split(".")[0]
            if not host.startswith("jobs.")
            else _path_part(path).lower()
        )
        return candidate("softgarden", slug, f"https://{host}", 0.82)
    if "myworkdayjobs.com" in host or "workdayjobs.com" in host:
        slug = host.split(".")[0].lower()
        return candidate("workday", slug, f"https://{host}", 0.78)
    if "successfactors" in host:
        query = parse_qs(parsed.query)
        slug = (query.get("company") or query.get("career_ns") or [""])[0].lower()
        if not slug:
            slug = _path_part(path).lower() or host.split(".")[0]
        return candidate("sap_successfactors", slug, f"https://{host}", 0.72)
    return None


def detect_ats_links(text: str) -> list[DetectedATS]:
    found: list[DetectedATS] = []
    seen: set[tuple[str, str, str]] = set()
    for link in _extract_links(text):
        detected = detect_ats_link(link)
        if detected is None:
            continue
        key = (detected.ats, detected.slug, detected.original_job_url)
        if key not in seen:
            seen.add(key)
            found.append(detected)
    return found


def _slugify_company(value: str) -> str:
    value = re.sub(r"&#39;|'", "", value)
    value = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower()).strip("-")
    return value


def _company_from_subject(subject: str) -> str:
    patterns = [
        r"thank you (?:very much )?for applying to\s+(.+?)(?:$|[–—|])",
        r"thanks for applying to(?: join)?\s+(.+?)(?:$|[–—|])",
        r"your application at\s+(.+?)(?:\s+for\b|$|[–—|/])",
        r"deine bewerbung bei\s+(.+?)(?:\s*/|$|[–—|])",
        r"feedback on your application\s*\|\s*.+?\|\s*(.+?)(?:$|[–—|])",
        r"(.+?)\s*\|\s*application received",
        r"(.+?)\s*\|\s*update on your application",
        r"we received your application for a position at\s+(.+?)(?:$|[–—|])",
        r"your application @\s*(.+?)(?:$|[–—|])",
        r"\[(.+?)\]\s*thank",
        r"application received for .+? at\s+(.+?)(?:$|[–—|])",
        r"\bposition at\s+(.+?)(?:$|[–—|.])",
    ]
    for pattern in patterns:
        match = re.search(pattern, subject, flags=re.I)
        if match:
            company = match.group(1).strip(" .,:;!—–-")
            company = re.sub(r"\s+—.*$", "", company).strip()
            return company
    return ""


def detect_ats_from_message_metadata(message: ZohoMessageSummary) -> list[DetectedATS]:
    """Infer ATS/company from sender and subject when emails omit direct links."""
    sender = message.sender.lower()
    subject = message.subject
    detections: list[DetectedATS] = []

    sender_email = parseaddr(message.sender)[1].lower()
    email_match = re.search(r"@([^>\s]+)", sender)
    sender_domain = (
        sender_email.rsplit("@", 1)[1]
        if "@" in sender_email
        else (email_match.group(1) if email_match else sender)
    )
    sender_domain = sender_domain.strip().strip(">")
    sender_local = sender_email.split("@", 1)[0] if "@" in sender_email else ""
    company_from_subject = _company_from_subject(subject)

    if sender_domain == "m.personio.de":
        slug = ""
        local = re.sub(r"\+.*$", "", sender_local)
        match = re.match(r"(?P<slug>.+?)-jobs(?:[._-].*)?$", local)
        if match:
            slug = match.group("slug").lower()
        elif company_from_subject:
            slug = _slugify_company(company_from_subject)
        if slug:
            detections.append(
                DetectedATS(
                    ats="personio",
                    slug=slug,
                    board_url=f"https://{slug}.jobs.personio.de",
                    original_job_url="",
                    company_name=company_from_subject or _display_name(slug),
                    company_domain=sender_domain,
                    confidence=0.86,
                    evidence={
                        "detected_from_sender": sender_domain,
                        "sender_local": sender_local,
                        "subject": subject,
                        "source": "personio_sender_alias",
                    },
                )
            )

    if sender_domain in {"personio.com", "personio.de"} and company_from_subject:
        slug = _slugify_company(company_from_subject)
        if slug:
            detections.append(
                DetectedATS(
                    ats="personio",
                    slug=slug,
                    board_url=f"https://{slug}.jobs.personio.de",
                    original_job_url="",
                    company_name=company_from_subject,
                    company_domain=sender_domain,
                    confidence=0.68,
                    evidence={
                        "detected_from_sender": sender_domain,
                        "subject": subject,
                        "source": "personio_company_subject",
                    },
                )
            )

    if sender_domain.endswith(".teamtailor-mail.com"):
        slug = sender_domain.removesuffix(".teamtailor-mail.com").lower()
        detections.append(
            DetectedATS(
                ats="teamtailor",
                slug=slug,
                board_url=f"https://{slug}.teamtailor.com",
                original_job_url="",
                company_name=company_from_subject or _display_name(slug),
                company_domain=sender_domain,
                confidence=0.76,
                evidence={
                    "detected_from_sender": sender_domain,
                    "subject": subject,
                    "source": "sender_domain",
                },
            )
        )

    if sender_domain == "recruitee-mailbox.com":
        local = sender.split("@", 1)[0]
        slug = ""
        if "." in local:
            slug = local.rsplit(".", 1)[-1].lower()
        if not slug and company_from_subject:
            slug = _slugify_company(company_from_subject)
        if slug:
            detections.append(
                DetectedATS(
                    ats="recruitee",
                    slug=slug,
                    board_url=f"https://{slug}.recruitee.com",
                    original_job_url="",
                    company_name=company_from_subject or _display_name(slug),
                    company_domain=sender_domain,
                    confidence=0.74,
                    evidence={
                        "detected_from_sender": sender_domain,
                        "subject": subject,
                        "source": "sender_domain",
                    },
                )
            )

    if sender_domain.endswith(".greenhouse-mail.io"):
        company = company_from_subject
        if company:
            slug = _slugify_company(company)
            detections.append(
                DetectedATS(
                    ats="greenhouse",
                    slug=slug,
                    board_url=f"https://boards.greenhouse.io/{slug}",
                    original_job_url="",
                    company_name=company,
                    company_domain=sender_domain,
                    confidence=0.68,
                    evidence={
                        "detected_from_sender": sender_domain,
                        "subject": subject,
                        "source": "sender_domain_subject",
                    },
                )
            )

    if sender_domain == "myworkday.com":
        local = sender.split("@", 1)[0]
        company = local.strip() if local and " " not in local else company_from_subject
        slug = _slugify_company(company)
        if slug:
            detections.append(
                DetectedATS(
                    ats="workday",
                    slug=slug,
                    board_url=f"https://{slug}.wd3.myworkdayjobs.com",
                    original_job_url="",
                    company_name=company or _display_name(slug),
                    company_domain=sender_domain,
                    confidence=0.64,
                    evidence={
                        "detected_from_sender": sender_domain,
                        "subject": subject,
                        "source": "sender_domain",
                    },
                )
            )

    return detections


def _company_registry_keys() -> tuple[set[tuple[str, str]], set[str], set[str]]:
    boards = load_company_boards(include_disabled=True)
    provider_slug = {(board.provider.lower(), board.slug.lower()) for board in boards}
    slugs = {board.slug.lower() for board in boards}
    company_names = {
        re.sub(r"[^a-z0-9]+", "", board.company.lower()) for board in boards
    }
    return provider_slug, slugs, company_names


def _discovery_seed_for_record(
    record: ExtractedApplicationRecord,
) -> tuple[str, str, str] | None:
    """Return ``(provider, slug, url)`` compatible with discovery seeds."""
    ats = (record.ats or "").lower()
    slug = (record.ats_slug or "").strip()
    if not ats or not slug:
        return None

    if ats in {"ashby", "greenhouse", "personio", "lever", "workable"}:
        return ats, slug, record.ats_board_url

    # These public career pages commonly expose JobPosting JSON-LD. The normal
    # discovery validator decides whether the page is actually usable.
    if ats in {"teamtailor", "recruitee", "join", "onlyfy", "softgarden"}:
        return "jsonld", slug, record.ats_board_url

    # Workday and SAP SuccessFactors need dedicated adapters before they can
    # become scheduled sources in this repo.
    return None


def _existing_seed_lines(path: Path) -> set[str]:
    if not path.exists():
        return set()
    lines: set[str] = set()
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.add(line.lower())
    return lines


def append_zoho_discovery_candidates(
    records: list[ExtractedApplicationRecord],
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> int:
    """Append new email-discovered company boards to a discovery seed file."""
    if not config.ZOHO_COMPANY_DISCOVERY_ENABLED:
        return 0

    path = path or Path(config.ZOHO_DISCOVERY_SEED_FILE)
    now = now or datetime.now(timezone.utc)
    provider_slug, slugs, company_names = _company_registry_keys()
    existing_seed_lines = _existing_seed_lines(path)
    seen: set[tuple[str, str]] = set()
    appended: list[str] = []

    for record in records:
        if record.confidence < config.ZOHO_DISCOVERY_MIN_CONFIDENCE:
            continue
        seed = _discovery_seed_for_record(record)
        if seed is None:
            continue
        provider, slug, url = seed
        provider_key = (provider.lower(), slug.lower())
        original_key = (record.ats.lower(), record.ats_slug.lower())
        company_key = re.sub(r"[^a-z0-9]+", "", (record.company_name or "").lower())
        if (
            provider_key in provider_slug
            or original_key in provider_slug
            or slug.lower() in slugs
            or (company_key and company_key in company_names)
        ):
            continue
        if provider_key in seen:
            continue
        seed_line = f"{provider}:{slug} {url}".strip()
        if seed_line.lower() in existing_seed_lines:
            continue
        seen.add(provider_key)
        existing_seed_lines.add(seed_line.lower())
        appended.extend(
            [
                (
                    f"# first_seen={now.isoformat(timespec='seconds')} "
                    f"source=zoho_mail ats={record.ats} "
                    f"company={record.company_name!r} confidence={record.confidence:.2f}"
                ),
                seed_line,
            ]
        )

    if not appended:
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = ""
    if path.exists() and path.read_text(encoding="utf-8", errors="ignore").strip():
        prefix = "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(prefix + "\n".join(appended).rstrip() + "\n")
    logger.info(
        "Zoho company discovery appended {} candidate boards to {}",
        len(appended) // 2,
        path,
    )
    return len(appended) // 2


def infer_status(subject: str, body: str) -> str:
    text = f"{subject}\n{body}".lower()
    if any(token in text for token in ("offer", "contract")):
        return "offer"
    if any(token in text for token in ("interview", "call", "screening", "meet")):
        return "interview"
    if any(
        token in text
        for token in ("unfortunately", "not move forward", "rejected", "declined")
    ):
        return "rejected"
    if any(
        token in text
        for token in (
            "received your application",
            "application received",
            "thank you for applying",
            "we received",
        )
    ):
        return "applied"
    if any(token in text for token in ("recruiter", "talent", "opportunity")):
        return "recruiter_outreach"
    return "unknown"


def infer_job_title(subject: str, body: str) -> str:
    patterns = [
        r"(?:application|applied|interview|position|role)\s+(?:for|to|as)\s+[:\-–—]?\s*([A-Z][^\n|]{3,90})",
        r"your application\s+[:\-–—]\s*([A-Z][^\n|]{3,90})",
        r"re:\s*([A-Z][^\n|]{3,90})",
    ]
    haystack = f"{subject}\n{body[:1000]}"
    for pattern in patterns:
        match = re.search(pattern, haystack, flags=re.I)
        if match:
            title = re.split(r"\s+(?:at|with)\s+", match.group(1).strip())[0]
            return title.strip(" .,-–—")
    return ""


def extract_sender_domain(sender: str) -> str:
    email = parseaddr(sender)[1]
    if "@" not in email:
        return ""
    domain = email.rsplit("@", 1)[1].lower()
    if domain in {
        "linkedin.com",
        "indeed.com",
        "mail.ashbyhq.com",
        "greenhouse.io",
        "personio.de",
    }:
        return ""
    return domain


def extract_application_records(
    *,
    account_id: str,
    message: ZohoMessageSummary,
    cleaned_content: str,
) -> list[ExtractedApplicationRecord]:
    detections = detect_ats_links(
        "\n".join([message.summary, cleaned_content, " ".join(message.links)])
    )
    status = infer_status(message.subject, cleaned_content)
    title = infer_job_title(message.subject, cleaned_content)
    sender_domain = extract_sender_domain(message.sender)
    application_date = message.message_date.isoformat() if message.message_date else ""

    if not detections:
        detections = detect_ats_from_message_metadata(message)

    if not detections:
        confidence = 0.45 if is_likely_job_email(message) else 0.2
        company_name = ""
        if sender_domain:
            company_name = _display_name(sender_domain.split(".")[0])
            confidence += 0.1
        return [
            ExtractedApplicationRecord(
                account_id=account_id,
                message_id=message.message_id,
                company_name=company_name,
                company_domain=sender_domain,
                job_title=title,
                application_date=application_date,
                status=status,
                evidence={
                    "subject": message.subject,
                    "sender": message.sender,
                    "reason": "likely job email but no supported ATS link detected",
                },
                confidence=confidence,
                needs_review=True,
            )
        ]

    records: list[ExtractedApplicationRecord] = []
    for detected in detections:
        confidence = detected.confidence
        if title:
            confidence += 0.04
        if status != "unknown":
            confidence += 0.03
        needs_review = (
            confidence < 0.75 or not detected.slug or not detected.original_job_url
        )
        records.append(
            ExtractedApplicationRecord(
                account_id=account_id,
                message_id=message.message_id,
                company_name=detected.company_name,
                company_domain=sender_domain or detected.company_domain,
                ats=detected.ats,
                ats_slug=detected.slug,
                ats_board_url=detected.board_url,
                original_job_url=detected.original_job_url,
                job_title=title,
                application_date=application_date,
                status=status,
                evidence={
                    **detected.evidence,
                    "subject": message.subject,
                    "sender": message.sender,
                    "status_signal": status,
                },
                confidence=min(confidence, 0.99),
                needs_review=needs_review,
            )
        )
    return records


@dataclass(frozen=True, slots=True)
class _AccountSyncPlan:
    account: ZohoAccount
    boundary: datetime | None
    dry_run: bool


class ZohoMailIngestionWorker:
    def __init__(
        self,
        api: ZohoMailAPI | None = None,
        *,
        page_limit: int | None = None,
        overlap_hours: int | None = None,
        parser_registry: AlertParserRegistry | None = None,
    ) -> None:
        self.api = api or ZohoOAuthMailClient(allow_token_cache_write=False)
        self.page_limit = page_limit or config.ZOHO_FOLDER_PAGE_LIMIT
        self.overlap = timedelta(
            hours=(
                overlap_hours
                if overlap_hours is not None
                else config.ZOHO_SYNC_OVERLAP_HOURS
            )
        )
        self.parser_registry = parser_registry or AlertParserRegistry()

    def _set_token_cache_policy(
        self,
        allowed: bool,
        *,
        persist_current: bool = False,
    ) -> None:
        setter = getattr(self.api, "set_token_cache_write_allowed", None)
        if callable(setter):
            setter(allowed, persist_current=persist_current)

    async def run(self, *, dry_run: bool | None = None) -> ZohoSyncResult:
        started_at = datetime.now(timezone.utc)
        self._set_token_cache_policy(dry_run is False)
        try:
            accounts = await self.api.list_accounts()
            plans: list[_AccountSyncPlan] = []
            for account in accounts:
                last_sync = await get_last_successful_sync_at(account.account_id)
                first_run = last_sync is None
                effective_dry_run = (
                    bool(dry_run)
                    if dry_run is not None
                    else (True if first_run else config.ZOHO_MAIL_SYNC_DRY_RUN)
                )
                boundary = (
                    parse_initial_sync_from()
                    if first_run
                    else last_sync - self.overlap
                )
                plans.append(_AccountSyncPlan(account, boundary, effective_dry_run))

            write_plans = [plan for plan in plans if not plan.dry_run]
            if write_plans:
                await init_zoho_mail_db()
                await init_db()
                self._set_token_cache_policy(True, persist_current=True)
            else:
                self._set_token_cache_policy(False)

            totals = _new_sync_totals(len(accounts))
            account_complete = {
                plan.account.account_id: True for plan in plans
            }
            discovery_records: list[ExtractedApplicationRecord] = []
            provider_health: dict[str, str] = {}
            encountered: set[tuple[str, str]] = set()
            processors = {
                mode: SharedMailPostFetchProcessor(
                    dry_run=mode,
                    parser_registry=self.parser_registry,
                    max_alert_items=MAX_ALERT_ITEMS_PER_SYNC,
                    pipeline=process_discovered_jobs,
                    terminal_callback=complete_alert_item_results,
                )
                for mode in {plan.dry_run for plan in plans}
            }

            for plan in plans:
                folders = await self.api.list_folders(plan.account.account_id)
                relevant = [folder for folder in folders if should_process_folder(folder)]
                totals["folders"] += len(relevant)
                logger.info(
                    "Zoho sync folders={} dry_run={} boundary={}",
                    len(relevant),
                    plan.dry_run,
                    plan.boundary.isoformat() if plan.boundary else "full-history",
                )
                for folder in relevant:
                    backlog = await self._sync_folder(
                        plan=plan,
                        folder=folder,
                        totals=totals,
                        discovery_records=discovery_records,
                        processor=processors[plan.dry_run],
                        encountered=encountered,
                    )
                    if backlog:
                        account_complete[plan.account.account_id] = False
                        break

            for processor in processors.values():
                shared = await processor.finalize()
                for key, value in shared.counts.items():
                    totals[key] += value
                for entry in shared.provider_health:
                    provider, _, status = entry.partition(":")
                    provider_health[provider] = status
                for completion in shared.completions:
                    await mark_message_processed(
                        account_id=completion.mailbox_key,
                        message_id=completion.message_id,
                        dry_run=processor.dry_run,
                        processing_version=1,
                        intent=completion.intent,
                        provider=completion.provider,
                        result=completion.result,
                        reason=completion.reason,
                    )

            if discovery_records:
                totals["discovery_candidates"] = append_zoho_discovery_candidates(
                    discovery_records
                )

            if write_plans:
                await process_pending_immediate_deliveries()

            checkpoint_targets = [
                plan.account.account_id
                for plan in write_plans
                if account_complete[plan.account.account_id]
            ]
            for account_id in checkpoint_targets:
                await save_successful_sync_checkpoint(
                    account_id,
                    synced_at=started_at,
                    api_domain=self.api.api_domain,
                )

            if write_plans and len(checkpoint_targets) == len(write_plans):
                await cleanup_processed_alert_items(dry_run=False)

            return ZohoSyncResult(
                dry_run=not bool(write_plans),
                checkpoint_advanced=bool(checkpoint_targets),
                provider_health=tuple(
                    f"{provider}:{status}"
                    for provider, status in sorted(provider_health.items())[:10]
                ),
                **totals,
            )
        finally:
            await self.api.close()

    async def _sync_folder(
        self,
        *,
        plan: _AccountSyncPlan,
        folder: ZohoFolder,
        totals: dict[str, int],
        discovery_records: list[ExtractedApplicationRecord],
        processor: SharedMailPostFetchProcessor,
        encountered: set[tuple[str, str]],
    ) -> bool:
        start = 1
        while True:
            page = await self.api.list_messages(
                plan.account.account_id,
                folder.folder_id,
                start=start,
                limit=self.page_limit,
            )
            if not page:
                break

            page_older_than_boundary = True
            for raw_message in page:
                message = replace(
                    raw_message,
                    folder_name=raw_message.folder_name or folder.name,
                    folder_id=raw_message.folder_id or folder.folder_id,
                    subject=bounded_text(raw_message.subject, 500),
                    sender=bounded_text(raw_message.sender, 320),
                    summary=bounded_text(raw_message.summary, MAX_SUMMARY),
                    links=bounded_values(
                        raw_message.links,
                        count=MAX_LINKS,
                        length=MAX_URL,
                    ),
                )
                if (
                    plan.boundary is not None
                    and message.message_date is not None
                    and message.message_date < plan.boundary
                ):
                    continue
                page_older_than_boundary = False
                totals["messages_seen"] += 1
                likely = is_likely_job_email(message)
                await upsert_message_summary(
                    account_id=plan.account.account_id,
                    message_id=message.message_id,
                    folder_id=folder.folder_id,
                    folder_name=folder.name,
                    subject=message.subject,
                    sender=message.sender,
                    message_date=message.message_date,
                    likely_job=likely,
                    dry_run=plan.dry_run,
                )
                state = await get_message_processing_state(
                    account_id=plan.account.account_id,
                    message_id=message.message_id,
                )
                if not likely:
                    continue
                if is_current_processing_state(state):
                    totals["current_version_skipped"] += 1
                    continue
                message_key = (plan.account.account_id, message.message_id)
                if message_key in encountered:
                    continue
                encountered.add(message_key)

                if (
                    is_stale_legacy_processing_state(state, message.message_date)
                ):
                    processor.add_completion(
                        transport="zoho",
                        mailbox_key=plan.account.account_id,
                        message_id=message.message_id,
                        intent=MailIntent.UNKNOWN_JOB_EMAIL.value,
                        result="legacy_reclassification_window_skipped",
                        reason="legacy_message_outside_14_day_alert_window",
                    )
                    processor.counts["unknown_job_messages"] += 1
                    continue

                if processor.backlog:
                    processor.counts["backlog_deferred"] += 1
                    return True

                raw_content = await self.api.get_message_content(
                    plan.account.account_id,
                    folder.folder_id,
                    message.message_id,
                )
                totals["full_messages_fetched"] += 1
                content = build_bounded_mail_content(
                    raw_content,
                    link_limit=MAX_LINKS - len(message.links),
                )
                del raw_content
                combined_links = tuple(
                    dict.fromkeys((*message.links, *content.links))
                )[:MAX_LINKS]
                content = replace(content, links=combined_links)
                metadata = MailMessageMetadata(
                    account_id=plan.account.account_id,
                    message_id=message.message_id,
                    folder_id=folder.folder_id,
                    folder_name=folder.name,
                    subject=message.subject,
                    sender=message.sender,
                    summary=message.summary,
                    message_date=message.message_date,
                )
                envelope = MailTransportEnvelope(
                    transport="zoho",
                    mailbox_key=plan.account.account_id,
                    message=metadata,
                    content=content,
                    summary_links=message.links,
                )

                async def write_routing(
                    intent: str,
                    provider: str,
                    result: str,
                    reason: str,
                ) -> None:
                    await set_message_routing(
                        account_id=plan.account.account_id,
                        message_id=message.message_id,
                        intent=intent,
                        provider=provider,
                        result=result,
                        reason=reason,
                        dry_run=plan.dry_run,
                    )

                async def process_application(
                    _: MailTransportEnvelope,
                ) -> ApplicationProcessingResult:
                    records = extract_application_records(
                        account_id=plan.account.account_id,
                        # Preserve the established application extractor input:
                        # bounded summary links plus the single cleaned text.
                        # Full-body links belong to alert parser contracts.
                        message=message,
                        cleaned_content=content.cleaned_text,
                    )
                    review_count = 0
                    for record in records:
                        if not plan.dry_run:
                            discovery_records.append(record)
                        application_id = await upsert_application_record(
                            record,
                            dry_run=plan.dry_run,
                        )
                        if record.needs_review:
                            await enqueue_review(
                                application_id=application_id,
                                account_id=plan.account.account_id,
                                message_id=message.message_id,
                                reason="low_confidence_or_missing_required_field",
                                payload=record.evidence,
                                dry_run=plan.dry_run,
                            )
                            review_count += 1
                    return ApplicationProcessingResult(
                        extracted_records=len(records),
                        review_records=review_count,
                    )

                handled = await processor.process_message(
                    envelope,
                    routing_writer=write_routing,
                    application_handler=process_application,
                )
                if not handled:
                    return True

            if len(page) < self.page_limit:
                break
            if plan.boundary is not None and page_older_than_boundary:
                break
            start += self.page_limit
            await asyncio.sleep(0)
        return False


def _new_sync_totals(accounts: int) -> dict[str, int]:
    return {
        "accounts": accounts,
        "folders": 0,
        "messages_seen": 0,
        "full_messages_fetched": 0,
        "extracted_records": 0,
        "review_records": 0,
        "discovery_candidates": 0,
        "application_messages": 0,
        "alert_messages": 0,
        "unknown_job_messages": 0,
        "parsed_alert_items": 0,
        "valid_alert_items": 0,
        "invalid_alert_items": 0,
        "pending_alert_items": 0,
        "processed_alert_items": 0,
        "provider_failures": 0,
        "pipeline_accepted": 0,
        "pipeline_rejected": 0,
        "current_version_skipped": 0,
        "backlog_deferred": 0,
    }


def _add_pipeline_counts(totals: dict[str, int], results: tuple[Any, ...]) -> None:
    for result in results:
        status = str(getattr(result.status, "value", result.status))
        if status == "rejected":
            totals["pipeline_rejected"] += 1
        else:
            totals["pipeline_accepted"] += 1
