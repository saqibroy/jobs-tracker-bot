#!/usr/bin/env python3
"""Print bounded structure for one configured Gmail Indeed message.

The report excludes bodies, recipients, IDs, OAuth values, raw URLs, query
values, and wrapper payloads. Run with ``--index N`` to select one list result.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from integrations.gmail_mail import (  # noqa: E402
    GmailDecodedMessage,
    GmailOAuthClient,
    decode_gmail_message,
)
from integrations.job_alerts.contracts import bounded_text  # noqa: E402
from integrations.job_alerts.urls import canonicalize_indeed_job_url  # noqa: E402

_INDEED_SENDER = "donotreply@match.indeed.com"
_CTA_TEXT = ("Job anzeigen", "Passt nicht", "Hybrides Arbeiten")
_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}")
_SUBJECT_RE = re.compile(r"^(?P<title>.+?)\s+bei\s+(?P<company>.+)$", re.I)
_SEMANTIC_CLASS_RE = re.compile(
    r"(?:job[-_](?:card|title|company|location|workplace|employment|summary|salary)|"
    r"recommendation[-_]card|view[-_]job|job_seen_beacon)",
    re.I,
)
_SAFE_PATH_SEGMENTS = {"jobs", "viewjob", "preferences", "v3"}
_SAFE_WRAPPER_KEYS = {
    "aggJobId",
    "clickType",
    "jk",
    "jobKey",
    "jobUrl",
    "metadata",
    "redirectUrl",
    "targetUrl",
    "url",
}


def _safe_name(value: object) -> str:
    candidate = str(value or "")
    return candidate if _NAME_RE.fullmatch(candidate) else ""


def _mime_counts(payload: object) -> Counter[str]:
    counts: Counter[str] = Counter()
    pending = [payload]
    while pending:
        part = pending.pop()
        if not isinstance(part, dict):
            continue
        mime = bounded_text(part.get("mimeType"), 80).lower()
        if mime:
            counts[mime] += 1
        children = part.get("parts")
        if isinstance(children, list):
            pending.extend(children[:100])
    return counts


def _path_shape(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "invalid"
    host = (parsed.hostname or "").lower()
    if not host:
        return "invalid"
    if host == "cts.indeed.com" and parsed.path.startswith("/v3/"):
        return "cts.indeed.com/v3/{encoded}"
    segments = [
        segment if segment.casefold() in _SAFE_PATH_SEGMENTS else "{segment}"
        for segment in parsed.path.split("/")
        if segment
    ]
    return bounded_text(f"{host}/{'/'.join(segments)}" if segments else f"{host}/", 160)


def _wrapper_summary(links: tuple[str, ...]) -> dict[str, object]:
    counts = Counter(
        total=0,
        base64url_decoded=0,
        utf8_decoded=0,
        json_object=0,
        canonical_identity_available=0,
    )
    json_keys: set[str] = set()
    for value in links[:200]:
        try:
            parsed = urlsplit(value)
        except ValueError:
            continue
        if (parsed.hostname or "").lower() != "cts.indeed.com":
            continue
        if not parsed.path.startswith("/v3/"):
            continue
        counts["total"] += 1
        if canonicalize_indeed_job_url(value) is not None:
            counts["canonical_identity_available"] += 1
        encoded = unquote(parsed.path.removeprefix("/v3/")).strip()
        if not encoded or len(encoded) > 1_000:
            continue
        try:
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            counts["base64url_decoded"] += 1
            decoded = raw.decode("utf-8")
            counts["utf8_decoded"] += 1
            payload = json.loads(decoded)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            counts["json_object"] += 1
            json_keys.update(key for key in payload if key in _SAFE_WRAPPER_KEYS)
    return {**dict(counts), "json_keys": sorted(json_keys)[:20]}


def _subject_parts(subject: str) -> tuple[str, str]:
    matched = _SUBJECT_RE.fullmatch(subject.strip())
    return (
        (matched.group("title").strip(), matched.group("company").strip())
        if matched
        else ("", "")
    )


def _text_role(value: str, *, title: str, company: str) -> str:
    normalized = " ".join(value.split())
    folded = normalized.casefold()
    if not normalized:
        return "empty"
    if title and folded == title.casefold():
        return "subject_title"
    if company and folded == company.casefold():
        return "subject_company"
    if any(folded == cta.casefold() for cta in _CTA_TEXT):
        return "known_cta"
    return "other_short_text" if len(normalized) <= 80 else "other_long_text"


def _subject_neighborhood(soup: BeautifulSoup, subject: str) -> list[str]:
    title, company = _subject_parts(subject)
    roles: list[str] = []
    title_index: int | None = None
    for node in soup.descendants:
        if not isinstance(node, NavigableString):
            continue
        value = " ".join(str(node).split())
        if not value:
            continue
        parent = node.parent if isinstance(node.parent, Tag) else soup
        role = _text_role(value, title=title, company=company)
        roles.append(f"{parent.name}:{role}")
        if role == "subject_title" and title_index is None:
            title_index = len(roles) - 1
    if title_index is None:
        return []
    return roles[max(0, title_index - 8) : title_index + 13]


def _element_shape(element: Tag) -> str:
    classes = [
        name
        for name in (_safe_name(value) for value in element.get("class", []))
        if name and _SEMANTIC_CLASS_RE.fullmatch(name)
    ][:4]
    attributes = sorted(
        {
            name
            for name in (_safe_name(key) for key in element.attrs)
            if name and name not in {"class", "href", "style"}
        }
    )[:6]
    return (
        element.name
        + ("." + ".".join(classes) if classes else "")
        + ("[" + ",".join(attributes) + "]" if attributes else "")
    )[:160]


def _anchor_context(anchor: Tag) -> list[str]:
    result = [_element_shape(anchor)]
    for parent in anchor.parents:
        if not isinstance(parent, Tag) or parent.name in {"html", "body"}:
            break
        result.append(_element_shape(parent))
        if len(result) >= 7:
            break
    return result


def build_safe_report(
    raw: dict[str, Any], decoded: GmailDecodedMessage
) -> dict[str, object]:
    """Build a bounded report whose values cannot include bodies or URL values."""

    mimes = _mime_counts(raw.get("payload"))
    soup = BeautifulSoup(decoded.content.sanitized_html, "html.parser")
    tags = soup.find_all(True, limit=2_000)
    wrappers = _wrapper_summary(decoded.content.links)
    title, company = _subject_parts(decoded.subject)
    relevant: list[Tag] = []
    for anchor in soup.find_all("a", href=True, limit=200):
        href = str(anchor.get("href") or "")
        role = _text_role(anchor.get_text(" ", strip=True), title=title, company=company)
        if _path_shape(href) == "cts.indeed.com/v3/{encoded}" or role == "known_cta":
            relevant.append(anchor)
        if len(relevant) >= 12:
            break
    cleaned = decoded.content.cleaned_text.casefold()
    return {
        "sender": bounded_text(decoded.sender, 320),
        "subject": bounded_text(decoded.subject, 500),
        "mime_type_counts": dict(sorted(mimes.items())[:20]),
        "has_html_body": mimes["text/html"] > 0,
        "has_plain_body": mimes["text/plain"] > 0,
        "body_truncated": decoded.content.truncated,
        "tag_counts": dict(Counter(tag.name for tag in tags).most_common(30)),
        "semantic_class_names": sorted(
            {
                name
                for tag in tags
                for name in (_safe_name(value) for value in tag.get("class", []))
                if name and _SEMANTIC_CLASS_RE.fullmatch(name)
            }
        )[:40],
        "data_or_aria_attribute_names": sorted(
            {
                name
                for tag in tags
                for name in (_safe_name(key) for key in tag.attrs)
                if name and (name.startswith("data-") or name.startswith("aria-"))
            }
        )[:40],
        "link_count": len(decoded.content.links),
        "link_host_path_shapes": dict(
            sorted(Counter(_path_shape(link) for link in decoded.content.links).items())[:30]
        ),
        "known_cta_present": {
            cta: cta.casefold() in cleaned for cta in _CTA_TEXT
        },
        "cts_indeed_url_count": wrappers["total"],
        "wrapper_facts": wrappers,
        "subject_text_neighborhood": _subject_neighborhood(soup, decoded.subject),
        "relevant_anchor_contexts": [
            {
                "anchor_text_role": _text_role(
                    anchor.get_text(" ", strip=True), title=title, company=company
                ),
                "ancestor_shapes": _anchor_context(anchor),
            }
            for anchor in relevant
        ],
    }


async def _inspect(index: int) -> dict[str, object]:
    client = GmailOAuthClient(allow_token_cache_write=False)
    try:
        page = await client.list_messages(
            label_ids=tuple(config.GMAIL_LABEL_IDS),
            query=config.GMAIL_QUERY,
            page_token=None,
            max_results=max(1, min(50, index + 1)),
        )
        references = page.get("messages") or []
        if not isinstance(references, list) or index >= len(references):
            raise RuntimeError("selected_message_not_available")
        reference = references[index]
        message_id = (
            bounded_text(reference.get("id"), 200)
            if isinstance(reference, dict)
            else ""
        )
        if not message_id:
            raise RuntimeError("selected_message_invalid")
        raw = await client.get_message(message_id)
        decoded = await decode_gmail_message(client, raw, message_id)
        if _INDEED_SENDER not in decoded.sender.casefold():
            raise RuntimeError("selected_message_is_not_from_indeed")
        return build_safe_report(raw, decoded)
    finally:
        await client.close()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=int, default=0)
    args = parser.parse_args(argv)
    if args.index < 0 or args.index > 49:
        raise SystemExit("--index must be between 0 and 49")
    try:
        report = asyncio.run(_inspect(args.index))
    except Exception as exc:
        raise SystemExit(f"Indeed inspection failed: {type(exc).__name__}") from None
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
