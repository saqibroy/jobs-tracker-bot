"""Build one bounded, cleaned mail document from a full-content response."""

from __future__ import annotations

import re
from urllib.parse import unquote

from bs4 import BeautifulSoup

from integrations.job_alerts.contracts import (
    BoundedMailContent,
    MAX_CONTENT_BYTES,
    MAX_LINKS,
    MAX_URL,
)

_URL_RE = re.compile(r"https?://[^\s<>'\")]+", re.I)
_SIGNATURE_MARKERS = (
    "\n-- ",
    "\nbest regards",
    "\nkind regards",
    "\nregards,",
    "\nsent from my",
)
_QUOTE_MARKERS = (
    "\non ",
    "\nfrom:",
    "\n> ",
    "\n-----original message-----",
    "\n---------- forwarded message ---------",
)


def _truncate_utf8(value: str, limit: int = MAX_CONTENT_BYTES) -> tuple[str, bool]:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def build_bounded_mail_content(
    content: object,
    *,
    link_limit: int = MAX_LINKS,
) -> BoundedMailContent:
    bounded, truncated = _truncate_utf8(str(content or ""))
    soup = BeautifulSoup(bounded, "html.parser")
    for tag in soup(["script", "style", "noscript", "blockquote"]):
        tag.decompose()
    for image in soup.find_all("img"):
        image.decompose()

    sanitized_source = str(soup)
    text = soup.get_text("\n", strip=True)
    links: list[str] = []
    seen: set[str] = set()
    examined = 0
    limit = max(0, min(MAX_LINKS, int(link_limit)))

    def append_link(raw: str) -> None:
        link = unquote(raw[:MAX_URL]).rstrip(".,);]")[:MAX_URL]
        if link and link not in seen:
            seen.add(link)
            links.append(link)

    if limit:
        for tag in soup.find_all("a", limit=limit):
            append_link(str(tag.get("href") or ""))
            examined += 1
        for match in _URL_RE.finditer(text):
            if examined >= limit:
                break
            append_link(match.group(0))
            examined += 1

    if len(links) > limit:
        links = links[:limit]

    text = re.sub(r"\n{3,}", "\n\n", text)
    lowered = text.lower()
    cut = len(text)
    for marker in _SIGNATURE_MARKERS + _QUOTE_MARKERS:
        index = lowered.find(marker)
        if index > 20:
            cut = min(cut, index)
    cleaned, text_truncated = _truncate_utf8(text[:cut].strip())
    sanitized_html, html_truncated = _truncate_utf8(sanitized_source)
    return BoundedMailContent(
        sanitized_html=sanitized_html,
        cleaned_text=cleaned,
        links=tuple(links),
        truncated=truncated or text_truncated or html_truncated,
    )
