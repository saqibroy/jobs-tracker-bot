"""Offline parser for sanitized, user-owned StepStone alert structures."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

from bs4 import Comment, NavigableString, Tag

from integrations.job_alerts.contracts import (
    AlertMatch,
    AlertParseStatus,
    BoundedMailContent,
    JobAlertItem,
    JobAlertParseResult,
    MAX_CONTENT_BYTES,
    MailMessageMetadata,
)
from integrations.job_alerts.provider_parsing import content_soup, workplace_evidence
from integrations.job_alerts.urls import alert_content_hash

_SENDER_RE = re.compile(r"(?:^|<)info@jobagent\.stepstone\.de(?:>|$)", re.I)
_DIGEST_HEADING_RE = re.compile(r"\bcheck out your latest matches\b", re.I)
_MATCHING_JOBS_RE = re.compile(
    r"\bwe found these new jobs that match your search for\b", re.I
)
_FOOTER_CTA_RE = re.compile(r"\bsee all matching jobs\b", re.I)
_LIFECYCLE_RE = re.compile(
    r"\b(your application|application (?:received|update|status)|"
    r"applied to|your interview|recruiter (?:message|outreach)|"
    r"deine bewerbung|ihre bewerbung|bewerbung (?:eingegangen|status))\b",
    re.I,
)
_FIT_BADGE_RE = re.compile(r"^(?:strong fit|good fit)$", re.I)
_FIELD_LABELS = ("company", "location", "contract type", "time", "salary")
_FIELD_LINE_RE = re.compile(
    r"^(company|location|contract type|time|salary)(?:\s*[:|-]\s*|\s+)(.*)$",
    re.I,
)
_HOME_OFFICE_POSSIBLE_RE = re.compile(r"\bhome\s*office\s+m[oö]glich\b", re.I)
_CONTRACT_VALUE_RE = re.compile(
    r"\b(feste anstellung|befristet|freelance|freiberuflich|"
    r"werkstudent|praktikum|vertrag)\b",
    re.I,
)
_WORKPLACE_TIME_VALUE_RE = re.compile(
    r"\b(home\s*office|remote|hybrid|vollzeit|teilzeit)\b", re.I
)
_SALARY_VALUE_RE = re.compile(r"(?:€|\beur\b|/jahr|per year)", re.I)
_NON_LOCATION_VALUE_RE = re.compile(
    r"^(?:corporate_fare|location_on|groups?|work|schedule|payments?)$|"
    r"^\d[\d.,+\s–—-]*$|"
    r"\b(?:employees?|mitarbeiter(?:innen)?|mitarbeitende)\b",
    re.I,
)


@dataclass(frozen=True, slots=True)
class _CardFields:
    title: str
    company: str
    location: str
    contract: str = ""
    workplace_time: str = ""
    salary: str = ""


@dataclass(frozen=True, slots=True)
class StepStoneAlertParser:
    provider: str = "stepstone"

    def matches(
        self,
        message: MailMessageMetadata,
        content: BoundedMailContent,
    ) -> AlertMatch:
        text = content.cleaned_text
        sender = bool(_SENDER_RE.search(message.sender.strip()))
        heading = bool(_DIGEST_HEADING_RE.search(text))
        digest = bool(_MATCHING_JOBS_RE.search(text))
        footer = bool(_FOOTER_CTA_RE.search(text)) or _html_has_matching_jobs_cta(
            content
        )
        wrapper = any(is_stepstone_click_wrapper(link) for link in content.links)
        excluded = bool(_LIFECYCLE_RE.search(" ".join((message.subject, text))))
        matched = sender and heading and digest and footer and wrapper and not excluded
        evidence = tuple(
            code
            for code, present in (
                ("stepstone_alert_sender", sender),
                ("stepstone_digest_heading", heading),
                ("stepstone_matching_jobs_body", digest),
                ("stepstone_matching_jobs_cta", footer),
                ("stepstone_click_wrapper_structure", wrapper),
            )
            if present
        )
        return AlertMatch(
            self.provider,
            matched,
            confidence=99 if matched else 0,
            evidence=evidence,
        )

    def parse(
        self,
        message: MailMessageMetadata,
        content: BoundedMailContent,
    ) -> JobAlertParseResult:
        if not self.matches(message, content).strong:
            return JobAlertParseResult(self.provider, AlertParseStatus.UNSUPPORTED)

        cards = _parse_observed_table_cards(content)
        if not cards:
            cards = _parse_labeled_cards(content.cleaned_text)
        items: list[JobAlertItem] = []
        issues: list[str] = []
        seen_identities: set[str] = set()
        invalid = 0
        for card in cards[:50]:
            if (
                not card.title
                or not card.company
                or not card.location
                or not _valid_location_value(card.location)
            ):
                invalid += 1
                _append_issue(issues, "missing_required_fields")
                continue
            content_identity = alert_content_hash(
                card.title, card.company, card.location
            )
            if content_identity in seen_identities:
                _append_issue(issues, "duplicate_provider_item")
                continue
            seen_identities.add(content_identity)
            workplace, is_remote, remote_scope = _stepstone_workplace_evidence(
                card.location, card.workplace_time
            )
            employment = ", ".join(
                value for value in (card.contract, card.workplace_time) if value
            )
            items.append(
                JobAlertItem(
                    provider=self.provider,
                    provider_item_id=f"content-{content_identity}",
                    title=card.title,
                    company=card.company,
                    location=card.location,
                    canonical_url=safe_stepstone_search_url(
                        card.title, card.company, card.location
                    ),
                    account_id=message.account_id,
                    message_id=message.message_id,
                    posted_at=None,
                    workplace_type=workplace,
                    is_remote=is_remote,
                    remote_scope=remote_scope,
                    employment_text=employment,
                    salary=card.salary,
                    confidence=97,
                    evidence=(
                        "stepstone_structural_card",
                        "stable_content_identity",
                        "personalized_wrapper_discarded",
                    ),
                )
            )
        return JobAlertParseResult(
            self.provider,
            AlertParseStatus.PARSED if items else AlertParseStatus.NO_ITEMS,
            items=tuple(items),
            issues=tuple(issues),
            examined_count=len(cards[:50]),
            invalid_count=invalid,
        )


def is_stepstone_click_wrapper(value: object) -> bool:
    """Recognize the wrapper host without inspecting or retaining its token."""

    candidate = str(value or "")
    if not candidate or len(candidate) > MAX_CONTENT_BYTES:
        return False
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return False
    return (
        parsed.scheme.lower() in {"http", "https"}
        and (parsed.hostname or "").lower() == "click.stepstone.de"
        and parsed.username is None
        and parsed.password is None
    )


def safe_stepstone_search_url(title: str, company: str, location: str) -> str:
    """Build a bounded public search URL from explicit non-personalized fields."""

    keywords = " ".join((title[:100], company[:80])).strip()
    query = urlencode((("ke", keywords), ("ws", location[:80])))
    return f"https://www.stepstone.de/jobs?{query}"


def _html_has_matching_jobs_cta(content: BoundedMailContent) -> bool:
    soup = content_soup(content)
    html_text = " ".join(soup.get_text(" ", strip=True).split())
    return _FOOTER_CTA_RE.search(html_text) is not None


def _parse_observed_table_cards(content: BoundedMailContent) -> list[_CardFields]:
    """Parse the fixture-proven classless presentation-table structure."""

    soup = content_soup(content)
    cards: list[_CardFields] = []
    seen_containers: set[int] = set()
    for anchor in soup.find_all("a", href=True, limit=200):
        href = str(anchor.get("href") or "")
        if not is_stepstone_click_wrapper(href) or anchor.find("strong") is None:
            continue
        title = " ".join(anchor.get_text(" ", strip=True).split())[:200]
        if _FOOTER_CTA_RE.search(title):
            continue
        container = _observed_card_container(anchor)
        if container is None or id(container) in seen_containers:
            continue
        seen_containers.add(id(container))
        values = _span_values_after_title(container, anchor)
        company = values[0] if len(values) >= 1 else ""
        location = values[1] if len(values) >= 2 else ""
        cards.append(
            _CardFields(
                title=title,
                company=company,
                location=location,
                contract=_first_matching_value(values, _CONTRACT_VALUE_RE),
                workplace_time=_first_matching_value(
                    values, _WORKPLACE_TIME_VALUE_RE
                ),
                salary=_first_matching_value(values, _SALARY_VALUE_RE),
            )
        )
    return cards


def _observed_card_container(anchor: Tag) -> Tag | None:
    for parent in anchor.parents:
        if not isinstance(parent, Tag) or parent.name != "table":
            continue
        strong_wrapper_count = sum(
            1
            for candidate in parent.find_all("a", href=True, limit=20)
            if is_stepstone_click_wrapper(candidate.get("href"))
            and candidate.find("strong") is not None
        )
        span_values = _span_values_after_title(parent, anchor)
        if strong_wrapper_count == 1 and len(span_values) >= 2:
            return parent
    return None


def _span_values_after_title(container: Tag, title_anchor: Tag) -> list[str]:
    if not title_anchor.get_text(" ", strip=True):
        values = []
        for element in title_anchor.find_all_next("span", limit=100):
            if container not in element.parents or element.find("span") is not None:
                continue
            value = " ".join(element.get_text(" ", strip=True).split())
            if value:
                values.append(value[:500])
        return values

    title_seen = False
    values: list[str] = []
    for node in container.descendants:
        if not isinstance(node, NavigableString) or isinstance(node, Comment):
            continue
        if title_anchor in node.parents:
            title_seen = True
            continue
        if (
            not title_seen
            or not isinstance(node.parent, Tag)
            or node.parent.name != "span"
        ):
            continue
        value = " ".join(str(node).split())
        if value:
            values.append(value[:500])
    return values


def _first_matching_value(values: list[str], pattern: re.Pattern[str]) -> str:
    return next((value for value in values if pattern.search(value)), "")


def _valid_location_value(value: str) -> bool:
    return not (
        _NON_LOCATION_VALUE_RE.fullmatch(value)
        or _CONTRACT_VALUE_RE.search(value)
        or _SALARY_VALUE_RE.search(value)
    )


def _parse_labeled_cards(text: str) -> list[_CardFields]:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    lines = [line for line in lines if line]
    company_indexes = [
        index for index, line in enumerate(lines) if _field_name(line) == "company"
    ]
    cards: list[_CardFields] = []
    for offset, company_index in enumerate(company_indexes):
        end = (
            company_indexes[offset + 1]
            if offset + 1 < len(company_indexes)
            else len(lines)
        )
        title = _title_before_field(lines, company_index)
        company = _field_value(lines, company_index, end)
        values: dict[str, str] = {}
        for index in range(company_index + 1, end):
            field = _field_name(lines[index])
            if field and field != "company" and field not in values:
                values[field] = _field_value(lines, index, end)
        cards.append(
            _CardFields(
                title=title,
                company=company,
                location=values.get("location", ""),
                contract=values.get("contract type", ""),
                workplace_time=values.get("time", ""),
                salary=values.get("salary", ""),
            )
        )
    return cards


def _field_name(line: str) -> str:
    normalized = line.strip().casefold().rstrip(":")
    if normalized in _FIELD_LABELS:
        return normalized
    match = _FIELD_LINE_RE.fullmatch(line.strip())
    return match.group(1).casefold() if match is not None else ""


def _field_value(lines: list[str], index: int, end: int) -> str:
    match = _FIELD_LINE_RE.fullmatch(lines[index].strip())
    if match is not None and match.group(2).strip():
        return match.group(2).strip()
    next_index = index + 1
    if next_index >= end or _field_name(lines[next_index]):
        return ""
    return lines[next_index]


def _title_before_field(lines: list[str], company_index: int) -> str:
    if company_index <= 0:
        return ""
    candidate = lines[company_index - 1]
    if _FIT_BADGE_RE.fullmatch(candidate) or _field_name(candidate):
        return ""
    return candidate


def _stepstone_workplace_evidence(
    location: str, workplace_time: str
) -> tuple[str, bool | None, str | None]:
    # "Homeoffice möglich" means home-office is possible, not an explicitly
    # Germany-wide fully remote role. Treat it as hybrid and let Berlin remain
    # authoritative in the shared strict location gate.
    if _HOME_OFFICE_POSSIBLE_RE.search(workplace_time):
        return "hybrid", False, None
    return workplace_evidence(location, workplace_time)


def _append_issue(issues: list[str], issue: str) -> None:
    if issue not in issues:
        issues.append(issue)
