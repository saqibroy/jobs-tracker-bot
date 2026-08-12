#!/usr/bin/env python3
"""Print bounded structure for one configured Gmail StepStone message.

The report excludes bodies, sender/recipient addresses, subjects, message or
account IDs, OAuth values, raw URLs, wrapper paths, and personalized tokens.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

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
from integrations.job_alerts.contracts import MailMessageMetadata  # noqa: E402
from integrations.job_alerts.registry import AlertParserRegistry  # noqa: E402
from integrations.job_alerts.routing import route_mail_intent  # noqa: E402
from integrations.job_alerts.stepstone import (  # noqa: E402
    StepStoneAlertParser,
    _parse_observed_table_cards,
    _span_values_after_title,
    is_stepstone_click_wrapper,
)

_STEPSTONE_SENDER = "info@jobagent.stepstone.de"
_SAFE_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}")
_SUBJECT_SHAPE_RE = re.compile(
    r"^.+?\s+and\s+\d+\s+other compan(?:y|ies)\s+are looking for candidates like you$",
    re.I,
)
_KNOWN_TEXT = (
    ("digest_heading", re.compile(r"\bcheck out your latest matches\b", re.I)),
    (
        "matching_jobs_body",
        re.compile(r"\bwe found these new jobs that match your search for\b", re.I),
    ),
    ("viewed_jobs_heading", re.compile(r"\bcandidates like you also viewed these jobs\b", re.I)),
    ("matching_jobs_cta", re.compile(r"\bsee all matching jobs\b", re.I)),
    ("fit_badge", re.compile(r"^(?:strong fit|good fit)$", re.I)),
    ("field_company", re.compile(r"^company$", re.I)),
    ("field_location", re.compile(r"^location$", re.I)),
    ("field_contract", re.compile(r"^contract type$", re.I)),
    ("field_time", re.compile(r"^time$", re.I)),
    ("field_salary", re.compile(r"^salary$", re.I)),
)
_SAFE_VALUE_ROLES = (
    ("salary_value", re.compile(r"(?:€|\beur\b|/jahr|per year)", re.I)),
    ("employee_count_value", re.compile(r"\b\d[\d.,+\s–—-]*\s+(?:employees|mitarbeiter)", re.I)),
    ("workplace_time_value", re.compile(r"\b(?:home\s*office|remote|hybrid|vollzeit|teilzeit)\b", re.I)),
    ("contract_value", re.compile(r"\b(?:feste anstellung|befristet|freelance|freiberuflich|vertrag)\b", re.I)),
)


def _safe_name(value: object) -> str:
    candidate = str(value or "")
    return candidate if _SAFE_NAME_RE.fullmatch(candidate) else ""


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


def _text_role(value: str) -> str:
    normalized = " ".join(value.split())
    for role, pattern in _KNOWN_TEXT:
        if pattern.search(normalized):
            return role
    for role, pattern in _SAFE_VALUE_ROLES:
        if pattern.search(normalized):
            return role
    return "other_short_text" if len(normalized) <= 80 else "other_long_text"


def _link_shape(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "invalid"
    host = (parsed.hostname or "").lower()
    if not host:
        return "invalid"
    if host == "click.stepstone.de":
        return "click.stepstone.de/{opaque}"
    if host == "stepstone.de" or host.endswith(".stepstone.de"):
        return "stepstone.de/{path}"
    return "external/{path}"


def _element_shape(element: Tag) -> str:
    attributes = sorted(
        {
            name
            for name in (_safe_name(key) for key in element.attrs)
            if name and name not in {"class", "href", "id", "style", "title"}
        }
    )[:8]
    return (
        element.name + ("[" + ",".join(attributes) + "]" if attributes else "")
    )[:160]


def _anchor_context(anchor: Tag) -> list[str]:
    result = [_element_shape(anchor)]
    for parent in anchor.parents:
        if not isinstance(parent, Tag) or parent.name in {"html", "body"}:
            break
        result.append(_element_shape(parent))
        if len(result) >= 8:
            break
    return result


def _text_role_sequence(soup: BeautifulSoup) -> tuple[list[str], list[tuple[Tag, int]]]:
    roles: list[str] = []
    anchor_positions: list[tuple[Tag, int]] = []
    for node in soup.descendants:
        if not isinstance(node, NavigableString) or isinstance(node, Comment):
            continue
        value = " ".join(str(node).split())
        if not value:
            continue
        role = _text_role(value)
        roles.append(role)
        parent = node.parent
        if isinstance(parent, Tag) and parent.name == "a" and parent.has_attr("href"):
            if _link_shape(str(parent.get("href") or "")) == "click.stepstone.de/{opaque}":
                anchor_positions.append((parent, len(roles) - 1))
    return roles, anchor_positions


def _leaf_texts(element: Tag) -> list[tuple[NavigableString, str]]:
    result: list[tuple[NavigableString, str]] = []
    for node in element.descendants:
        if not isinstance(node, NavigableString):
            continue
        value = " ".join(str(node).split())
        if value:
            result.append((node, value))
    return result


def _length_bucket(value: str) -> str:
    length = len(value)
    return "1-4" if length <= 4 else "5-12" if length <= 12 else "13-30" if length <= 30 else "31+"


def _fit_card_reports(
    soup: BeautifulSoup,
) -> tuple[list[dict[str, object]], list[list[dict[str, object]]]]:
    ancestry: list[dict[str, object]] = []
    sequences: list[list[dict[str, object]]] = []
    for text_node in soup.find_all(
        string=lambda value: bool(
            value
            and re.fullmatch(r"\s*(?:Strong\s+Fit|Good\s+Fit)\s*", value, re.I)
        )
    )[:10]:
        if not isinstance(text_node, NavigableString):
            continue
        tables = [parent for parent in text_node.parents if isinstance(parent, Tag) and parent.name == "table"][:8]
        levels: list[dict[str, int]] = []
        selected: Tag | None = None
        for table in tables:
            texts = _leaf_texts(table)
            wrapper_count = sum(
                1
                for anchor in table.find_all("a", href=True, limit=100)
                if _link_shape(str(anchor.get("href") or "")) == "click.stepstone.de/{opaque}"
            )
            fit_count = sum(1 for _node, value in texts if _text_role(value) == "fit_badge")
            levels.append(
                {
                    "text_nodes": len(texts),
                    "unique_text_nodes": len({value.casefold() for _node, value in texts}),
                    "wrapper_links": wrapper_count,
                    "fit_badges": fit_count,
                }
            )
            if selected is None and wrapper_count >= 1 and len(texts) >= 4 and fit_count == 1:
                selected = table
        ancestry.append({"table_levels": levels})
        if selected is None:
            sequences.append([])
            continue
        texts = _leaf_texts(selected)
        counts = Counter(value.casefold() for _node, value in texts)
        duplicate_groups = {
            value: index + 1
            for index, value in enumerate(
                sorted(value for value, count in counts.items() if count > 1)
            )
        }
        abstract: list[dict[str, object]] = []
        for node, value in texts:
            role = _text_role(value)
            parent_anchor = next(
                (
                    parent
                    for parent in node.parents
                    if isinstance(parent, Tag) and parent.name == "a"
                ),
                None,
            )
            wrapper = bool(
                parent_anchor is not None
                and _link_shape(str(parent_anchor.get("href") or ""))
                == "click.stepstone.de/{opaque}"
            )
            folded = value.casefold()
            abstract.append(
                {
                    "role": role,
                    "wrapper": wrapper,
                    "duplicate_group": duplicate_groups.get(folded, 0),
                    "length_bucket": _length_bucket(value),
                    "word_count": min(20, len(value.split())),
                    "has_digit": any(character.isdigit() for character in value),
                    "parent_shape": _element_shape(node.parent) if isinstance(node.parent, Tag) else "",
                }
            )
        sequences.append(abstract[:40])
    return ancestry, sequences


def build_safe_report(
    raw: dict[str, Any], decoded: GmailDecodedMessage
) -> dict[str, object]:
    """Build a report whose values cannot contain message or wrapper content."""

    soup = BeautifulSoup(decoded.content.sanitized_html, "html.parser")
    tags = soup.find_all(True, limit=2_000)
    roles, anchor_positions = _text_role_sequence(soup)
    wrapper_anchors: list[Tag] = []
    for anchor in soup.find_all("a", href=True, limit=200):
        if _link_shape(str(anchor.get("href") or "")) == "click.stepstone.de/{opaque}":
            wrapper_anchors.append(anchor)
        if len(wrapper_anchors) >= 20:
            break
    role_counts = Counter(roles)
    mimes = _mime_counts(raw.get("payload"))
    fit_ancestry, fit_sequences = _fit_card_reports(soup)
    metadata = MailMessageMetadata(
        "inspection",
        "inspection",
        subject=decoded.subject,
        sender=decoded.sender,
        message_date=decoded.received_at,
    )
    parser_match = StepStoneAlertParser().matches(metadata, decoded.content)
    parsed = StepStoneAlertParser().parse(metadata, decoded.content)
    structural_cards = _parse_observed_table_cards(decoded.content)
    route = route_mail_intent(metadata, decoded.content, AlertParserRegistry())
    return {
        "sender_domain_match": _STEPSTONE_SENDER in decoded.sender.casefold(),
        "subject_shape_match": _SUBJECT_SHAPE_RE.fullmatch(decoded.subject.strip()) is not None,
        "parser_match": parser_match.matched,
        "parser_evidence_codes": list(parser_match.evidence),
        "routing_intent": route.intent.value,
        "routing_evidence_codes": list(route.evidence),
        "parse_status": parsed.status.value,
        "parse_examined_count": parsed.examined_count,
        "parse_valid_count": parsed.valid_count,
        "parse_invalid_count": parsed.invalid_count,
        "parse_issue_codes": list(parsed.issues),
        "structural_card_field_shapes": [
            {
                "title_present": bool(card.title),
                "title_length": _length_bucket(card.title),
                "company_present": bool(card.company),
                "company_length": _length_bucket(card.company),
                "location_present": bool(card.location),
                "location_length": _length_bucket(card.location),
                "contract_present": bool(card.contract),
                "workplace_time_present": bool(card.workplace_time),
                "salary_present": bool(card.salary),
            }
            for card in structural_cards[:20]
        ],
        "strong_wrapper_table_shapes": [
            [
                {
                    "span_values_after": len(_span_values_after_title(table, anchor)),
                    "strong_wrapper_count": sum(
                        1
                        for candidate in table.find_all("a", href=True, limit=20)
                        if is_stepstone_click_wrapper(candidate.get("href"))
                        and candidate.find("strong") is not None
                    ),
                    "wrapper_count": sum(
                        1
                        for candidate in table.find_all("a", href=True, limit=50)
                        if is_stepstone_click_wrapper(candidate.get("href"))
                    ),
                }
                for table in (
                    parent
                    for parent in anchor.parents
                    if isinstance(parent, Tag) and parent.name == "table"
                )
            ][:8]
            for anchor in wrapper_anchors[:20]
            if anchor.find("strong") is not None
        ],
        "mime_type_counts": dict(sorted(mimes.items())[:20]),
        "has_html_body": mimes["text/html"] > 0,
        "has_plain_body": mimes["text/plain"] > 0,
        "body_truncated": decoded.content.truncated,
        "tag_counts": dict(Counter(tag.name for tag in tags).most_common(30)),
        "safe_attribute_names": sorted(
            {
                name
                for tag in tags
                for name in (_safe_name(key) for key in tag.attrs)
                if name and name not in {"class", "href", "id", "style", "title"}
            }
        )[:40],
        "link_count": len(decoded.content.links),
        "link_host_path_shapes": dict(
            sorted(Counter(_link_shape(link) for link in decoded.content.links).items())[:10]
        ),
        "known_text_role_counts": {
            role: role_counts[role] for role, _pattern in _KNOWN_TEXT
        },
        "wrapper_anchor_count": len(wrapper_anchors),
        "fit_badge_table_ancestry": fit_ancestry,
        "selected_fit_card_abstract_sequences": fit_sequences,
        "wrapper_anchor_contexts": [
            {
                "anchor_text_role": _text_role(anchor.get_text(" ", strip=True)),
                "ancestor_shapes": _anchor_context(anchor),
            }
            for anchor in wrapper_anchors[:12]
        ],
        "wrapper_text_neighborhoods": [
            roles[max(0, index - 8) : index + 18]
            for _anchor, index in anchor_positions[:12]
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
        if _STEPSTONE_SENDER not in decoded.sender.casefold():
            raise RuntimeError("selected_message_is_not_from_stepstone")
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
        raise SystemExit(f"StepStone inspection failed: {type(exc).__name__}") from None
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
