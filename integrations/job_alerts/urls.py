"""Offline-only alert URL safety, normalization, and identity helpers."""

from __future__ import annotations

import hashlib
import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

MAX_URL_LENGTH = 1_000
MAX_WRAPPER_LAYERS = 3
_TRACKING_KEYS = {
    "campaign",
    "campaignid",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "refid",
    "session",
    "sessionid",
    "tracking",
    "trackingid",
    "trk",
    "trkemail",
    "eid",
    "lipi",
    "midtoken",
    "midsig",
    "otptoken",
}

_LINKEDIN_JOB_PATH_RE = re.compile(r"/(?:comm/)?jobs/view/(\d+)(?:/|$)")
_INDEED_KEY_RE = re.compile(r"[A-Za-z0-9_-]{6,100}")


@dataclass(frozen=True, slots=True)
class IndeedJobLink:
    provider_item_id: str
    canonical_url: str


def normalize_alert_url(
    value: object,
    *,
    wrapper_hosts: tuple[str, ...] = (),
    wrapper_query_params: tuple[str, ...] = (),
) -> str | None:
    """Return one safe normalized HTTP(S) URL without making a request."""

    candidate = str(value or "").strip()
    if not candidate or len(candidate) > MAX_URL_LENGTH:
        return None
    allowed_wrapper_hosts = {
        host.strip().lower() for host in wrapper_hosts if host.strip()
    }
    wrapper_keys = {key.lower() for key in wrapper_query_params}
    for _ in range(MAX_WRAPPER_LAYERS + 1):
        normalized = _normalize_one(candidate)
        if normalized is None:
            return None
        parsed = urlsplit(normalized)
        if (
            not wrapper_keys
            or not allowed_wrapper_hosts
            or parsed.hostname not in allowed_wrapper_hosts
        ):
            return normalized
        query = parse_qsl(parsed.query, keep_blank_values=True)
        wrapped = next(
            (item for key, item in query if key.lower() in wrapper_keys and item),
            "",
        )
        if not wrapped or wrapped == candidate:
            return normalized
        candidate = wrapped
    return None


def _normalize_one(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
    except (UnicodeError, ValueError):
        return None
    if not host:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{netloc}:{port}"
    query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in _TRACKING_KEYS:
            continue
        query.append((key, item))
    normalized = urlunsplit((scheme, netloc, parsed.path or "/", urlencode(query), ""))
    return normalized if len(normalized) <= MAX_URL_LENGTH else None


def alert_content_hash(title: str, company: str, location: str) -> str:
    composite = "|".join(
        " ".join(value.lower().split()) for value in (title, company, location)
    )
    return hashlib.sha256(composite.encode("utf-8")).hexdigest()


def alert_identity_key(
    *,
    provider_item_id: str,
    canonical_url: str,
    title: str,
    company: str,
    location: str,
) -> tuple[str, str]:
    """Return ``(identity_key, content_hash)`` in the approved precedence."""

    content_hash = alert_content_hash(title, company, location)
    native = " ".join(provider_item_id.split())[:200]
    if native:
        return f"id:{native}", content_hash
    normalized = normalize_alert_url(canonical_url)
    if normalized:
        return f"url:{normalized}", content_hash
    return f"hash:{content_hash}", content_hash


def canonicalize_linkedin_job_url(value: object) -> tuple[str, str] | None:
    """Return the stable numeric LinkedIn identity and public jobs URL."""

    normalized = normalize_alert_url(value)
    if normalized is None:
        return None
    parsed = urlsplit(normalized)
    host = (parsed.hostname or "").lower()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    match = _LINKEDIN_JOB_PATH_RE.search(parsed.path)
    if match is None:
        return None
    job_id = match.group(1)
    return job_id, f"https://www.linkedin.com/jobs/view/{job_id}"


def canonicalize_indeed_job_url(value: object) -> IndeedJobLink | None:
    """Resolve a fixture-proven Indeed URL locally; never perform I/O."""

    normalized = normalize_alert_url(value)
    if normalized is None:
        return None
    parsed = urlsplit(normalized)
    host = (parsed.hostname or "").lower()
    if host == "cts.indeed.com" and parsed.path.startswith("/v3/"):
        return _decode_indeed_v3_wrapper(parsed.path.removeprefix("/v3/"))
    if host != "indeed.com" and not host.endswith(".indeed.com"):
        return None
    if parsed.path.rstrip("/") != "/viewjob":
        return None
    query = dict(parse_qsl(parsed.query, keep_blank_values=False))
    return _indeed_job_link(query.get("jk", ""))


def _decode_indeed_v3_wrapper(encoded_payload: str) -> IndeedJobLink | None:
    """Decode only the sanitized-sample base64url JSON wrapper structure."""

    encoded = unquote(encoded_payload).strip()
    if not encoded or len(encoded) > MAX_URL_LENGTH:
        return None
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode(encoded + padding)
        if len(raw) > MAX_URL_LENGTH:
            return None
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    target = _find_payload_string(
        payload,
        ("url", "jobUrl", "targetUrl", "redirectUrl"),
    )
    target_key = ""
    if target:
        normalized_target = normalize_alert_url(target)
        if normalized_target is None:
            return None
        target_parts = urlsplit(normalized_target)
        target_host = (target_parts.hostname or "").lower()
        if target_host != "indeed.com" and not target_host.endswith(".indeed.com"):
            return None
        if target_parts.path.rstrip("/") != "/viewjob":
            return None
        target_key = dict(parse_qsl(target_parts.query)).get("jk", "")

    stable_key = target_key or _find_payload_string(
        payload,
        ("jk", "aggJobId", "jobKey"),
    )
    return _indeed_job_link(stable_key)


def _find_payload_string(payload: Any, keys: tuple[str, ...]) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in payload.values():
        if isinstance(value, dict):
            found = _find_payload_string(value, keys)
            if found:
                return found
    return ""


def _indeed_job_link(job_key: str) -> IndeedJobLink | None:
    key = job_key.strip()
    if _INDEED_KEY_RE.fullmatch(key) is None:
        return None
    return IndeedJobLink(
        provider_item_id=key,
        canonical_url=f"https://de.indeed.com/viewjob?jk={key}",
    )
