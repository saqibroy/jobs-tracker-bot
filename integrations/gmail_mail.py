"""Read-only Gmail transport for the shared Phase 6 job-alert pipeline."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx

import config
from integrations.job_alerts import AlertParserRegistry, MailIntent, MailMessageMetadata
from integrations.job_alerts.contracts import (
    BoundedMailContent,
    MAX_ALERT_ITEMS_PER_SYNC,
    MAX_CONTENT_BYTES,
    bounded_text,
)
from integrations.job_alerts.message import build_bounded_mail_content
from integrations.job_alerts.service import (
    MAIL_PROCESSING_VERSION,
    MailTransportEnvelope,
    SharedMailPostFetchProcessor,
    is_current_processing_state,
    is_likely_job_message,
)
from job_ingestion import process_discovered_jobs
from notifiers.delivery import process_pending_immediate_deliveries
from storage.database import init_db
from storage.gmail_mail import (
    cleanup_gmail_messages,
    get_gmail_message_state,
    get_gmail_sync_state,
    init_gmail_mail_db,
    mark_gmail_message_processed,
    save_gmail_checkpoint,
    set_gmail_message_routing,
    upsert_gmail_message,
)
from storage.zoho_mail import (
    cleanup_processed_alert_items,
    complete_alert_item_results,
)

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
_SYNC_LOCK = asyncio.Lock()
_GMAIL_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,300}")


class GmailError(RuntimeError):
    """Bounded, non-secret Gmail transport failure."""


class GmailSyncBusyError(GmailError):
    pass


class GmailAPI(Protocol):
    async def list_messages(
        self,
        *,
        label_ids: tuple[str, ...],
        query: str,
        page_token: str | None,
        max_results: int,
    ) -> dict[str, Any]: ...

    async def get_message(self, message_id: str) -> dict[str, Any]: ...

    async def get_attachment(
        self, message_id: str, attachment_id: str
    ) -> dict[str, Any]: ...

    def set_token_cache_write_allowed(
        self, allowed: bool, *, persist_current: bool = False
    ) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class GmailDecodedMessage:
    message_id: str
    label_ids: tuple[str, ...]
    subject: str
    sender: str
    snippet: str
    received_at: datetime | None
    content: BoundedMailContent
    external_body_fetches: int = 0


@dataclass(frozen=True, slots=True)
class GmailSyncResult:
    dry_run: bool
    mailbox_key: str
    pages: int = 0
    messages_seen: int = 0
    full_messages_fetched: int = 0
    external_body_fetches: int = 0
    current_version_skipped: int = 0
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
    backlog_deferred: int = 0
    checkpoint_advanced: bool = False
    scope_changed: bool = False
    provider_health: tuple[str, ...] = ()


class GmailOAuthClient:
    """GET-only Gmail REST client using installed-app OAuth refresh tokens."""

    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        *,
        client_file: str | None = None,
        token_file: str | None = None,
        refresh_token: str | None = None,
        api_base: str = GMAIL_API_BASE,
        allow_token_cache_write: bool = False,
    ) -> None:
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=20.0)
        self.client_file = Path(client_file or config.GMAIL_OAUTH_CLIENT_FILE)
        self.token_file = Path(token_file or config.GMAIL_OAUTH_TOKEN_FILE)
        self._configured_refresh_token = refresh_token or config.GMAIL_REFRESH_TOKEN
        self.api_base = api_base.rstrip("/")
        self._allow_token_cache_write = allow_token_cache_write
        self._client: dict[str, Any] | None = None
        self._token: dict[str, Any] | None = None
        self._token_loaded = False

    def set_token_cache_write_allowed(
        self, allowed: bool, *, persist_current: bool = False
    ) -> None:
        self._allow_token_cache_write = bool(allowed)
        if allowed and persist_current and self._token is not None:
            self._write_token_cache(self._token)

    async def list_messages(
        self,
        *,
        label_ids: tuple[str, ...],
        query: str,
        page_token: str | None,
        max_results: int,
    ) -> dict[str, Any]:
        params: list[tuple[str, str]] = [
            ("q", query),
            ("maxResults", str(max(1, min(500, max_results)))),
        ]
        params.extend(("labelIds", label) for label in label_ids)
        if page_token:
            params.append(("pageToken", page_token))
        return await self._get("/users/me/messages", params=params)

    async def get_message(self, message_id: str) -> dict[str, Any]:
        safe_id = bounded_text(message_id, 200)
        if not safe_id or _GMAIL_ID_RE.fullmatch(safe_id) is None:
            raise GmailError("gmail_message_id_missing")
        return await self._get(
            f"/users/me/messages/{safe_id}", params=[("format", "full")]
        )

    async def get_attachment(
        self, message_id: str, attachment_id: str
    ) -> dict[str, Any]:
        safe_message = bounded_text(message_id, 200)
        safe_attachment = bounded_text(attachment_id, 300)
        if (
            not safe_message
            or not safe_attachment
            or _GMAIL_ID_RE.fullmatch(safe_message) is None
            or _GMAIL_ID_RE.fullmatch(safe_attachment) is None
        ):
            raise GmailError("gmail_attachment_identity_missing")
        return await self._get(
            f"/users/me/messages/{safe_message}/attachments/{safe_attachment}"
        )

    async def _get(
        self, path: str, *, params: list[tuple[str, str]] | None = None
    ) -> dict[str, Any]:
        token = await self._access_token()
        response = await self._http.get(
            f"{self.api_base}{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code >= 400:
            raise GmailError(f"gmail_api_http_{response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise GmailError("gmail_api_invalid_json") from exc
        if not isinstance(payload, dict):
            raise GmailError("gmail_api_invalid_payload")
        return payload

    async def _access_token(self) -> str:
        self._load_token_once()
        assert self._token is not None
        scope = str(self._token.get("scope") or "")
        if not _has_exact_readonly_scope(scope):
            raise GmailError("gmail_oauth_token_scope_invalid")
        access_token = str(self._token.get("access_token") or "")
        expires_at = _float(self._token.get("expires_at"))
        if access_token and expires_at > datetime.now(timezone.utc).timestamp() + 60:
            return access_token
        await self._refresh_access_token()
        assert self._token is not None
        access_token = str(self._token.get("access_token") or "")
        if not access_token:
            raise GmailError("gmail_oauth_access_token_missing")
        return access_token

    def _load_token_once(self) -> None:
        if self._token_loaded:
            return
        self._token_loaded = True
        payload: dict[str, Any] = {}
        if self.token_file.is_file():
            try:
                parsed = json.loads(self.token_file.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    payload = parsed
            except (OSError, ValueError):
                raise GmailError("gmail_oauth_token_cache_unreadable")
        if not payload and self._configured_refresh_token:
            payload = {
                "refresh_token": self._configured_refresh_token,
                "scope": GMAIL_READONLY_SCOPE,
            }
        if not payload:
            raise GmailError("gmail_oauth_token_cache_missing")
        self._token = payload

    def _load_client(self) -> dict[str, Any]:
        if self._client is not None:
            return self._client
        try:
            raw = json.loads(self.client_file.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise GmailError("gmail_oauth_client_file_missing") from exc
        except (OSError, ValueError) as exc:
            raise GmailError("gmail_oauth_client_file_unreadable") from exc
        client = raw.get("installed") if isinstance(raw, dict) else None
        if not isinstance(client, dict):
            raise GmailError("gmail_oauth_installed_client_required")
        if not client.get("client_id") or not client.get("client_secret"):
            raise GmailError("gmail_oauth_client_incomplete")
        self._client = client
        return client

    async def _refresh_access_token(self) -> None:
        assert self._token is not None
        refresh_token = str(self._token.get("refresh_token") or "")
        if not refresh_token:
            raise GmailError("gmail_oauth_refresh_token_missing")
        client = self._load_client()
        token_uri = str(
            client.get("token_uri") or "https://oauth2.googleapis.com/token"
        )
        response = await self._http.post(
            token_uri,
            data={
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if response.status_code >= 400:
            raise GmailError(f"gmail_oauth_refresh_http_{response.status_code}")
        try:
            refreshed = response.json()
        except ValueError as exc:
            raise GmailError("gmail_oauth_refresh_invalid_json") from exc
        if not isinstance(refreshed, dict) or not refreshed.get("access_token"):
            raise GmailError("gmail_oauth_refresh_access_token_missing")
        returned_scope = str(refreshed.get("scope") or self._token.get("scope") or "")
        if not _has_exact_readonly_scope(returned_scope):
            raise GmailError("gmail_oauth_refresh_scope_invalid")
        self._token = {
            **self._token,
            **refreshed,
            "refresh_token": str(refreshed.get("refresh_token") or refresh_token),
            "scope": returned_scope or GMAIL_READONLY_SCOPE,
            "expires_at": datetime.now(timezone.utc).timestamp()
            + max(1, int(refreshed.get("expires_in") or 3600)),
        }
        if self._allow_token_cache_write:
            self._write_token_cache(self._token)

    def _write_token_cache(self, token: dict[str, Any]) -> None:
        if not self._allow_token_cache_write:
            return
        parent = self.token_file.parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            parent.chmod(stat.S_IRWXU)
        except OSError:
            pass
        payload = json.dumps(token, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.token_file.name}.", dir=str(parent)
        )
        try:
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.token_file)
            self.token_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()


class GmailMailIngestionWorker:
    def __init__(
        self,
        api: GmailAPI | None = None,
        *,
        mailbox_key: str | None = None,
        label_ids: tuple[str, ...] | None = None,
        query: str | None = None,
        page_size: int | None = None,
        overlap_hours: int | None = None,
        max_alert_items: int = MAX_ALERT_ITEMS_PER_SYNC,
        parser_registry: AlertParserRegistry | None = None,
    ) -> None:
        self.api = api or GmailOAuthClient(allow_token_cache_write=False)
        self.mailbox_key = bounded_text(mailbox_key or config.GMAIL_MAILBOX_KEY, 200)
        self.label_ids = tuple(
            sorted(
                {
                    bounded_text(value, 100)
                    for value in (
                        label_ids
                        if label_ids is not None
                        else tuple(config.GMAIL_LABEL_IDS)
                    )
                    if bounded_text(value, 100)
                }
            )
        )
        self.query = query if query is not None else config.GMAIL_QUERY
        self.page_size = max(1, min(500, page_size or config.GMAIL_PAGE_SIZE))
        self.overlap = timedelta(
            hours=(
                overlap_hours
                if overlap_hours is not None
                else config.GMAIL_SYNC_OVERLAP_HOURS
            )
        )
        self.parser_registry = parser_registry or AlertParserRegistry()
        self.max_alert_items = max(1, min(MAX_ALERT_ITEMS_PER_SYNC, max_alert_items))

    async def run(self, *, dry_run: bool = False) -> GmailSyncResult:
        if _SYNC_LOCK.locked():
            raise GmailSyncBusyError("gmail_sync_already_running")
        async with _SYNC_LOCK:
            return await self._run_locked(dry_run=dry_run)

    async def _run_locked(self, *, dry_run: bool) -> GmailSyncResult:
        started_at = datetime.now(timezone.utc)
        self._validate_scope()
        setter = getattr(self.api, "set_token_cache_write_allowed", None)
        if callable(setter):
            setter(not dry_run)
        fingerprint = gmail_scope_fingerprint(
            self.mailbox_key, self.label_ids, self.query
        )
        stored = await get_gmail_sync_state(self.mailbox_key)
        established = (
            stored.scope_fingerprint == fingerprint
            and stored.last_successful_sync_at is not None
        )
        scope_changed = bool(stored.scope_fingerprint) and not established
        boundary = (
            stored.last_successful_sync_at - self.overlap
            if established and stored.last_successful_sync_at is not None
            else started_at - timedelta(days=14)
        )
        effective_query = compose_gmail_query(self.query, boundary)
        if not dry_run:
            await init_gmail_mail_db()
            await init_db()

        processor = SharedMailPostFetchProcessor(
            dry_run=dry_run,
            parser_registry=self.parser_registry,
            max_alert_items=self.max_alert_items,
            pipeline=process_discovered_jobs,
            terminal_callback=complete_alert_item_results,
        )
        totals = {
            "pages": 0,
            "messages_seen": 0,
            "full_messages_fetched": 0,
            "external_body_fetches": 0,
            "current_version_skipped": 0,
        }
        page_token: str | None = None
        seen_tokens: set[str] = set()
        try:
            while True:
                page = await self.api.list_messages(
                    label_ids=self.label_ids,
                    query=effective_query,
                    page_token=page_token,
                    max_results=self.page_size,
                )
                totals["pages"] += 1
                references = page.get("messages") or []
                if not isinstance(references, list):
                    raise GmailError("gmail_list_messages_invalid")
                for reference in references:
                    if not isinstance(reference, dict):
                        continue
                    message_id = bounded_text(reference.get("id"), 200)
                    if not message_id:
                        raise GmailError("gmail_list_message_id_missing")
                    totals["messages_seen"] += 1
                    raw = await self.api.get_message(message_id)
                    totals["full_messages_fetched"] += 1
                    decoded = await decode_gmail_message(self.api, raw, message_id)
                    del raw
                    totals["external_body_fetches"] += decoded.external_body_fetches
                    metadata = MailMessageMetadata(
                        account_id=self.mailbox_key,
                        message_id=message_id,
                        folder_id=",".join(decoded.label_ids),
                        folder_name=",".join(decoded.label_ids),
                        subject=decoded.subject,
                        sender=decoded.sender,
                        summary=decoded.snippet,
                        message_date=decoded.received_at,
                    )
                    likely = is_likely_job_message(metadata, decoded.content)
                    await upsert_gmail_message(
                        mailbox_key=self.mailbox_key,
                        message_id=message_id,
                        label_ids=decoded.label_ids,
                        subject=decoded.subject,
                        sender=decoded.sender,
                        snippet=decoded.snippet,
                        received_at=decoded.received_at,
                        likely_job=likely,
                        dry_run=dry_run,
                    )
                    state = await get_gmail_message_state(
                        mailbox_key=self.mailbox_key, message_id=message_id
                    )
                    if is_current_processing_state(state):
                        totals["current_version_skipped"] += 1
                        continue
                    if not likely:
                        processor.counts["unknown_job_messages"] += 1
                        processor.add_completion(
                            transport="gmail",
                            mailbox_key=self.mailbox_key,
                            message_id=message_id,
                            intent=MailIntent.UNKNOWN_JOB_EMAIL.value,
                            result="not_likely_job",
                            reason="likely_job_signals_absent",
                        )
                        continue
                    envelope = MailTransportEnvelope(
                        "gmail",
                        self.mailbox_key,
                        metadata,
                        decoded.content,
                    )

                    async def write_routing(
                        intent: str,
                        provider: str,
                        result: str,
                        reason: str,
                        *,
                        selected_id: str = message_id,
                    ) -> None:
                        await set_gmail_message_routing(
                            mailbox_key=self.mailbox_key,
                            message_id=selected_id,
                            intent=intent,
                            provider=provider,
                            result=result,
                            reason=reason,
                            dry_run=dry_run,
                        )

                    await processor.process_message(
                        envelope,
                        routing_writer=write_routing,
                        application_handler=None,
                    )
                    await asyncio.sleep(0)

                next_token_raw = page.get("nextPageToken")
                if not next_token_raw:
                    break
                next_token = bounded_text(next_token_raw, 500)
                if not next_token or next_token in seen_tokens:
                    raise GmailError("gmail_pagination_token_repeated")
                seen_tokens.add(next_token)
                page_token = next_token
                del page

            shared = await processor.finalize()
            for completion in shared.completions:
                await mark_gmail_message_processed(
                    mailbox_key=completion.mailbox_key,
                    message_id=completion.message_id,
                    intent=completion.intent,
                    provider=completion.provider,
                    result=completion.result,
                    reason=completion.reason,
                    dry_run=dry_run,
                    processing_version=MAIL_PROCESSING_VERSION,
                )
            if not dry_run:
                await process_pending_immediate_deliveries()
            checkpoint_advanced = not dry_run and not shared.backlog
            if checkpoint_advanced:
                await cleanup_gmail_messages()
                await cleanup_processed_alert_items(dry_run=False)
                await save_gmail_checkpoint(
                    mailbox_key=self.mailbox_key,
                    scope_fingerprint=fingerprint,
                    synced_at=started_at,
                    metadata={
                        "pages": totals["pages"],
                        "messages": totals["messages_seen"],
                    },
                )
            return GmailSyncResult(
                dry_run=dry_run,
                mailbox_key=self.mailbox_key,
                checkpoint_advanced=checkpoint_advanced,
                scope_changed=scope_changed,
                provider_health=shared.provider_health,
                **totals,
                **{
                    key: shared.counts[key]
                    for key in (
                        "application_messages",
                        "alert_messages",
                        "unknown_job_messages",
                        "parsed_alert_items",
                        "valid_alert_items",
                        "invalid_alert_items",
                        "pending_alert_items",
                        "processed_alert_items",
                        "provider_failures",
                        "pipeline_accepted",
                        "pipeline_rejected",
                        "backlog_deferred",
                    )
                },
            )
        finally:
            await self.api.close()

    def _validate_scope(self) -> None:
        if not self.mailbox_key:
            raise GmailError("gmail_mailbox_key_required")
        if not self.label_ids and not self.query.strip():
            raise GmailError("gmail_bounded_label_or_query_required")
        if self.overlap < timedelta(0) or self.overlap > timedelta(days=14):
            raise GmailError("gmail_overlap_out_of_range")


def gmail_scope_fingerprint(
    mailbox_key: str, label_ids: tuple[str, ...], exact_query: str
) -> str:
    payload = json.dumps(
        {
            "mailbox_key": mailbox_key,
            "label_ids": sorted(label_ids),
            "query": exact_query,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compose_gmail_query(exact_query: str, boundary: datetime) -> str:
    epoch = int(boundary.astimezone(timezone.utc).timestamp())
    return f"after:{epoch} {exact_query}" if exact_query else f"after:{epoch}"


async def decode_gmail_message(
    api: GmailAPI,
    raw: dict[str, Any],
    expected_message_id: str,
) -> GmailDecodedMessage:
    message_id = bounded_text(raw.get("id") or expected_message_id, 200)
    if message_id != expected_message_id:
        raise GmailError("gmail_message_identity_mismatch")
    labels_raw = raw.get("labelIds") or []
    label_ids = (
        tuple(
            bounded_text(value, 100)
            for value in labels_raw[:50]
            if bounded_text(value, 100)
        )
        if isinstance(labels_raw, list)
        else ()
    )
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        raise GmailError("gmail_message_payload_missing")
    headers = _headers(payload)
    received_at = _internal_date(raw.get("internalDate"))
    collector = _MimeCollector(api, message_id)
    await collector.collect(payload)
    source = collector.rendered_source()
    content = build_bounded_mail_content(source)
    if collector.truncated and not content.truncated:
        content = replace(content, truncated=True)
    return GmailDecodedMessage(
        message_id=message_id,
        label_ids=label_ids,
        subject=bounded_text(headers.get("subject"), 500),
        sender=bounded_text(headers.get("from"), 320),
        snippet=bounded_text(raw.get("snippet"), 1_000),
        received_at=received_at,
        content=content,
        external_body_fetches=collector.external_fetches,
    )


class _MimeCollector:
    def __init__(self, api: GmailAPI, message_id: str) -> None:
        self.api = api
        self.message_id = message_id
        self.remaining = MAX_CONTENT_BYTES
        self.fragments: list[tuple[str, str]] = []
        self.truncated = False
        self.external_fetches = 0

    async def collect(self, part: dict[str, Any]) -> None:
        mime = str(part.get("mimeType") or "").lower()
        parts = part.get("parts")
        if isinstance(parts, list) and parts:
            if mime == "multipart/alternative":
                selected = self._select_alternative(parts)
                if selected is not None:
                    await self.collect(selected)
                return
            for child in parts:
                if isinstance(child, dict):
                    await self.collect(child)
            return
        if mime not in {"text/plain", "text/html"}:
            return
        filename = str(part.get("filename") or "")
        disposition = _headers(part).get("content-disposition", "").strip().lower()
        if filename or disposition.startswith("attachment"):
            return
        body = part.get("body")
        if not isinstance(body, dict):
            return
        data = body.get("data")
        if data:
            decoded, truncated = _decode_base64url(data, limit=self.remaining)
            self.truncated = self.truncated or truncated
            self._append(mime, decoded)
            return
        attachment_id = bounded_text(body.get("attachmentId"), 300)
        if not attachment_id:
            return
        declared_size = _nonnegative_int(body.get("size"))
        if declared_size is None or declared_size > self.remaining:
            self.truncated = True
            return
        external = await self.api.get_attachment(self.message_id, attachment_id)
        self.external_fetches += 1
        external_data = external.get("data") if isinstance(external, dict) else None
        if not external_data:
            raise GmailError("gmail_text_body_attachment_missing_data")
        decoded, truncated = _decode_base64url(external_data, limit=self.remaining)
        self.truncated = self.truncated or truncated
        self._append(mime, decoded)

    def _select_alternative(self, parts: list[Any]) -> dict[str, Any] | None:
        candidates = [part for part in parts if isinstance(part, dict)]
        for preferred in ("text/html", "text/plain"):
            for part in candidates:
                if (
                    str(part.get("mimeType") or "").lower() == preferred
                    and not str(part.get("filename") or "")
                    and not _headers(part)
                    .get("content-disposition", "")
                    .strip()
                    .lower()
                    .startswith("attachment")
                ):
                    return part
        return candidates[0] if candidates else None

    def _append(self, mime: str, decoded: bytes) -> None:
        if not self.remaining:
            self.truncated = True
            return
        selected = decoded[: self.remaining]
        if len(decoded) > len(selected):
            self.truncated = True
        self.remaining -= len(selected)
        text = selected.decode("utf-8", errors="replace")
        self.fragments.append((mime, text))

    def rendered_source(self) -> str:
        rendered: list[str] = []
        for mime, value in self.fragments:
            rendered.append(
                value if mime == "text/html" else f"<pre>{html.escape(value)}</pre>"
            )
        return "\n".join(rendered)


def _headers(part: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    values = part.get("headers") or []
    if not isinstance(values, list):
        return result
    for header in values[:100]:
        if isinstance(header, dict):
            name = str(header.get("name") or "").strip().lower()
            if name and name not in result:
                result[name] = str(header.get("value") or "")[:1_000]
    return result


def _internal_date(value: object) -> datetime | None:
    try:
        milliseconds = int(str(value))
    except (TypeError, ValueError):
        return None
    if milliseconds < 0:
        return None
    try:
        return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _decode_base64url(value: object, *, limit: int | None = None) -> tuple[bytes, bool]:
    try:
        encoded = str(value).encode("ascii")
        truncated = False
        if limit is not None:
            max_encoded = ((max(0, limit) + 2) // 3) * 4
            if len(encoded) > max_encoded:
                encoded = encoded[:max_encoded]
                encoded = encoded[: len(encoded) - (len(encoded) % 4)]
                truncated = True
        padding = b"=" * (-len(encoded) % 4)
        decoded = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        if limit is not None and len(decoded) > limit:
            decoded = decoded[:limit]
            truncated = True
        return decoded, truncated
    except (UnicodeEncodeError, ValueError) as exc:
        raise GmailError("gmail_mime_base64url_invalid") from exc


def _nonnegative_int(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _has_exact_readonly_scope(value: str) -> bool:
    return set(value.split()) == {GMAIL_READONLY_SCOPE}
