"""Small HTML helpers shared by fixture-backed alert parsers."""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from integrations.job_alerts.contracts import BoundedMailContent, bounded_text
from integrations.job_alerts.urls import normalize_alert_url

_DIRECT_LINK_TEXT = re.compile(
    r"(?:apply|bewerben|company site|employer site|karriereseite)", re.I
)


def content_soup(content: BoundedMailContent) -> BeautifulSoup:
    return BeautifulSoup(content.sanitized_html, "html.parser")


def first_text(card: Tag, selectors: tuple[str, ...], limit: int) -> str:
    for selector in selectors:
        element = card.select_one(selector)
        if element is not None:
            value = bounded_text(element.get_text(" ", strip=True), limit)
            if value:
                return value
    return ""


def associated_card(anchor: Tag, selectors: tuple[str, ...]) -> Tag:
    fallback: Tag = anchor
    for parent in anchor.parents:
        if not isinstance(parent, Tag):
            continue
        fallback = parent
        classes = {str(value).lower() for value in parent.get("class", [])}
        if parent.has_attr("data-job-card") or any(
            "job-card" in value or "recommendation-card" in value
            for value in classes
        ):
            return parent
        if any(parent.select_one(selector) is not None for selector in selectors):
            return parent
        if parent.name in {"li", "tr"}:
            return parent
    return fallback


def explicit_datetime(card: Tag) -> datetime | None:
    element = card.select_one("time[datetime], [data-posted-at]")
    if element is None:
        return None
    raw = str(element.get("datetime") or element.get("data-posted-at") or "").strip()
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def workplace_evidence(*values: str) -> tuple[str, bool | None, str | None]:
    text = " ".join(value for value in values if value).lower()
    if re.search(r"\b(hybrid|hybrides arbeiten|hybridarbeit)\b", text):
        return "hybrid", False, None
    if re.search(r"\b(remote|homeoffice|home office)\b", text):
        scope = (
            "germany"
            if re.search(r"\b(germany|deutschland|german)\b", text)
            else "eu"
            if re.search(r"\b(eu|europe|european|emea|dach)\b", text)
            else "worldwide"
            if re.search(r"\b(worldwide|anywhere|global)\b", text)
            else None
        )
        return "remote", True, scope
    if re.search(r"\b(on[ -]?site|vor ort|präsenz)\b", text):
        return "onsite", False, None
    return "unknown", None, None


def associated_direct_url(card: Tag, provider_domain: str) -> str:
    for anchor in card.find_all("a", href=True, limit=20):
        classes = {str(value).lower() for value in anchor.get("class", [])}
        explicitly_direct = (
            anchor.has_attr("data-direct-job-link")
            or "direct-job-link" in classes
            or _DIRECT_LINK_TEXT.search(anchor.get_text(" ", strip=True)) is not None
        )
        if not explicitly_direct:
            continue
        normalized = normalize_alert_url(anchor.get("href"))
        if normalized is None:
            continue
        host = (urlsplit(normalized).hostname or "").lower()
        if host == provider_domain or host.endswith(f".{provider_domain}"):
            continue
        return normalized
    return ""
