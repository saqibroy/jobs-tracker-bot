"""Shared helpers for direct ATS adapters."""

from __future__ import annotations

import html
import re
from datetime import datetime

from bs4 import BeautifulSoup


def clean_html(value: str | None) -> str:
    if not value:
        return ""
    decoded = html.unescape(html.unescape(value))
    return BeautifulSoup(decoded, "html.parser").get_text(" ", strip=True)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def infer_workplace(value: str, is_remote: bool | None = None) -> str:
    text = value.lower()
    if "hybrid" in text:
        return "hybrid"
    has_onsite = any(token in text for token in ("on-site", "onsite", "on site", "in-office"))
    has_remote = is_remote is True or "remote" in text
    if has_onsite and has_remote:
        return "hybrid"
    if has_onsite:
        return "onsite"
    if has_remote:
        return "remote"
    return "unknown"


def country_codes_from_text(value: str) -> list[str]:
    text = value.lower()
    mapping = {
        "germany": "de", "deutschland": "de", "berlin": "de",
        "france": "fr", "spain": "es", "poland": "pl", "portugal": "pt",
        "italy": "it", "netherlands": "nl", "united kingdom": "gb",
        "united states": "us", "canada": "ca",
        "nantes": "fr", "paris": "fr", "lyon": "fr",
        "madrid": "es", "barcelona": "es", "lisbon": "pt",
        "warsaw": "pl", "wrocław": "pl", "wroclaw": "pl",
        "kraków": "pl", "krakow": "pl", "bucharest": "ro", "london": "gb",
    }
    return sorted({code for token, code in mapping.items() if token in text})


def regions_from_text(value: str) -> list[str]:
    text = value.lower()
    regions = []
    for token in ("worldwide", "global", "anywhere", "europe", "eu", "eea", "emea", "dach"):
        if re.search(rf"\b{re.escape(token)}\b", text):
            regions.append(token)
    return regions
