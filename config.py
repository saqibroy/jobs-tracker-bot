"""Centralized configuration — loads .env and exposes typed settings."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (job-bot/)
_env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(_env_path)


def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _get_list(key: str, default: str = "") -> list[str]:
    raw = _get(key, default)
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _validated_int(
    values: Mapping[str, str],
    key: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int,
) -> int:
    """Read one bounded integer and fail with a setting-specific message."""

    raw = values.get(key, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class SchedulingSettings:
    """Validated source scheduling and concurrency settings."""

    max_concurrent_source_adapters: int
    max_concurrent_source_components: int
    max_concurrent_http_requests: int
    group_a_interval_minutes: int
    group_b_interval_minutes: int
    group_a_startup_delay_minutes: int
    group_b_startup_delay_minutes: int
    source_group_misfire_grace_seconds: int


def load_scheduling_settings(
    values: Mapping[str, str] | None = None,
) -> SchedulingSettings:
    """Load independent source runtime settings.

    ``MAX_CONCURRENT_SOURCES`` is retained only as a deprecated fallback for
    adapter concurrency. Component and HTTP limits have independent defaults
    and never inherit it.
    """

    env = os.environ if values is None else values
    if "MAX_CONCURRENT_SOURCE_ADAPTERS" in env:
        adapter_concurrency = _validated_int(
            env,
            "MAX_CONCURRENT_SOURCE_ADAPTERS",
            3,
            maximum=32,
        )
    else:
        adapter_concurrency = _validated_int(
            env,
            "MAX_CONCURRENT_SOURCES",
            2,
            maximum=32,
        )
    settings = SchedulingSettings(
        max_concurrent_source_adapters=adapter_concurrency,
        max_concurrent_source_components=_validated_int(
            env,
            "MAX_CONCURRENT_SOURCE_COMPONENTS",
            3,
            maximum=32,
        ),
        max_concurrent_http_requests=_validated_int(
            env,
            "MAX_CONCURRENT_HTTP_REQUESTS",
            4,
            maximum=64,
        ),
        group_a_interval_minutes=_validated_int(
            env,
            "SOURCE_GROUP_A_INTERVAL_MINUTES",
            60,
            maximum=1440,
        ),
        group_b_interval_minutes=_validated_int(
            env,
            "SOURCE_GROUP_B_INTERVAL_MINUTES",
            120,
            maximum=1440,
        ),
        group_a_startup_delay_minutes=_validated_int(
            env,
            "SOURCE_GROUP_A_STARTUP_DELAY_MINUTES",
            1,
            maximum=1439,
        ),
        group_b_startup_delay_minutes=_validated_int(
            env,
            "SOURCE_GROUP_B_STARTUP_DELAY_MINUTES",
            6,
            maximum=1439,
        ),
        source_group_misfire_grace_seconds=_validated_int(
            env,
            "SOURCE_GROUP_MISFIRE_GRACE_SECONDS",
            300,
            maximum=3600,
        ),
    )
    if settings.group_a_startup_delay_minutes >= settings.group_a_interval_minutes:
        raise ValueError(
            "SOURCE_GROUP_A_STARTUP_DELAY_MINUTES must be inside the Group A cadence"
        )
    if settings.group_b_startup_delay_minutes >= settings.group_b_interval_minutes:
        raise ValueError(
            "SOURCE_GROUP_B_STARTUP_DELAY_MINUTES must be inside the Group B cadence"
        )
    offsets = {
        settings.group_a_startup_delay_minutes,
        settings.group_b_startup_delay_minutes,
    }
    if len(offsets) != 2:
        raise ValueError("source group startup delays must be distinct")
    return settings


# ── Notifications ──────────────────────────────────────────────────────────
DISCORD_WEBHOOK_URL: str = _get("DISCORD_WEBHOOK_URL")
DISCORD_WEBHOOK_URL_NGO: str = _get("DISCORD_WEBHOOK_URL_NGO")
DISCORD_BOT_TOKEN: str = _get("DISCORD_BOT_TOKEN")
DISCORD_COMMAND_CHANNEL_ID: str = _get("DISCORD_COMMAND_CHANNEL_ID")
TELEGRAM_BOT_TOKEN: str = _get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: str = _get("TELEGRAM_CHAT_ID")

# ── Scheduling ─────────────────────────────────────────────────────────────
SCAN_INTERVAL_MINUTES: int = int(_get("SCAN_INTERVAL_MINUTES", "45"))
DIGEST_INTERVAL_HOURS: int = int(_get("DIGEST_INTERVAL_HOURS", "6"))

SCHEDULING: SchedulingSettings = load_scheduling_settings()
SOURCE_GROUP_A_INTERVAL_MINUTES: int = SCHEDULING.group_a_interval_minutes
SOURCE_GROUP_B_INTERVAL_MINUTES: int = SCHEDULING.group_b_interval_minutes
SOURCE_GROUP_A_STARTUP_DELAY_MINUTES: int = SCHEDULING.group_a_startup_delay_minutes
SOURCE_GROUP_B_STARTUP_DELAY_MINUTES: int = SCHEDULING.group_b_startup_delay_minutes
SOURCE_GROUP_MISFIRE_GRACE_SECONDS: int = SCHEDULING.source_group_misfire_grace_seconds

# Lightweight daily status embed. This does not send jobs; it reports whether
# scans are running and how many jobs were fetched/accepted in the latest scan.
DAILY_STATUS_ENABLED: bool = _get("DAILY_STATUS_ENABLED", "true").lower() in ("true", "1", "yes")
DAILY_STATUS_HOUR: int = int(_get("DAILY_STATUS_HOUR", "18"))  # UTC hour

# Weekly NGO digest (Monday morning summary)
WEEKLY_DIGEST_ENABLED: bool = _get("WEEKLY_DIGEST_ENABLED", "true").lower() in ("true", "1", "yes")
WEEKLY_DIGEST_DAY: str = _get("WEEKLY_DIGEST_DAY", "mon")  # mon, tue, wed, ...
WEEKLY_DIGEST_HOUR: int = int(_get("WEEKLY_DIGEST_HOUR", "8"))  # UTC hour

# ── Filters ────────────────────────────────────────────────────────────────
LOCATION_ALLOWLIST: list[str] = _get_list(
    "LOCATION_ALLOWLIST", "worldwide,eu,europe,germany,berlin,remote"
)
LOCATION_BLOCKLIST: list[str] = _get_list(
    "LOCATION_BLOCKLIST", "uk only,united kingdom,london,us only,canada only"
)
MIN_NGO_SCORE: int = int(_get("MIN_NGO_SCORE", "1"))
MAX_JOB_AGE_DAYS: int = int(_get("MAX_JOB_AGE_DAYS", "14"))

# Per-source age overrides — sources with longer hiring cycles get more time.
# Format: {"source_name": days}  (overrides MAX_JOB_AGE_DAYS for that source)
SOURCE_MAX_AGE_DAYS: dict[str, int] = {
    "reliefweb": int(_get("MAX_JOB_AGE_DAYS_RELIEFWEB", "30")),
}

# ── Company blocklist ──────────────────────────────────────────────────────
# Comma-separated company names to always skip (case-insensitive).
COMPANY_BLOCKLIST: list[str] = _get_list("COMPANY_BLOCKLIST")

# ── Legacy ATS environment lists ───────────────────────────────────────────
# Direct employer boards now live in validated companies.toml entries. These
# values remain readable for backward compatibility with older scripts/tests,
# but scheduled v2 scans do not use them.
#
# Ashby: the slug is the last path segment of https://jobs.ashbyhq.com/<slug>
ASHBY_COMPANIES: list[str] = [
    s.strip() for s in _get("ASHBY_COMPANIES", "").split(",") if s.strip()
]
# Personio: the subdomain of https://<slug>.jobs.personio.de
PERSONIO_COMPANIES: list[str] = _get_list("PERSONIO_COMPANIES")
# BambooHR: the subdomain of https://<slug>.bamboohr.com
BAMBOOHR_COMPANIES: list[str] = _get_list("BAMBOOHR_COMPANIES")

# ── Optional quality filters ──────────────────────────────────────────────
FILTER_SENIOR_ONLY: bool = _get("FILTER_SENIOR_ONLY", "false").lower() in ("true", "1", "yes")
MIN_SALARY_EUR: int = int(_get("MIN_SALARY_EUR", "0"))

# Minimum match score (0–100).  Jobs below this threshold are still
# accepted but clearly marked as low match.  Set > 0 to hard-reject
# very low-match jobs.
MINIMUM_MATCH_SCORE: int = int(_get("MINIMUM_MATCH_SCORE", "0"))

# Accept on-site Germany jobs (no remote/hybrid signal).
# When false: reject Germany-scope jobs that lack remote/hybrid signals.
ACCEPT_ONSITE_GERMANY: bool = _get("ACCEPT_ONSITE_GERMANY", "false").lower() in ("true", "1", "yes")

# ── Concurrency ────────────────────────────────────────────────────────────
MAX_CONCURRENT_SOURCES: int = int(_get("MAX_CONCURRENT_SOURCES", "3"))
MAX_CONCURRENT_SOURCE_ADAPTERS: int = SCHEDULING.max_concurrent_source_adapters
MAX_CONCURRENT_SOURCE_COMPONENTS: int = SCHEDULING.max_concurrent_source_components
MAX_CONCURRENT_HTTP_REQUESTS: int = SCHEDULING.max_concurrent_http_requests

# ── Passive company discovery ──────────────────────────────────────────────
# Mine aggregator apply links for known ATS board URLs and append newly-seen
# boards to data/discovery/sniffed_from_jobs.txt for the scheduled discovery
# workflow to validate/promote later. This performs no extra HTTP requests.
ENABLE_ATS_SNIFFING: bool = _get("ENABLE_ATS_SNIFFING", "true").lower() in ("true", "1", "yes")

# ── Zoho Mail ingestion ────────────────────────────────────────────────────
# Optional read-only worker for application/recruiter email history. It is off
# by default; run ``python main.py --zoho-sync --dry-run`` locally first.
ZOHO_MAIL_SYNC_ENABLED: bool = _get("ZOHO_MAIL_SYNC_ENABLED", "false").lower() in ("true", "1", "yes")
ZOHO_MAIL_SYNC_INTERVAL_MINUTES: int = int(_get("ZOHO_MAIL_SYNC_INTERVAL_MINUTES", "180"))
ZOHO_CLIENT_ID: str = _get("ZOHO_CLIENT_ID")
ZOHO_CLIENT_SECRET: str = _get("ZOHO_CLIENT_SECRET")
ZOHO_REFRESH_TOKEN: str = _get("ZOHO_REFRESH_TOKEN")
ZOHO_ACCOUNT_ID: str = _get("ZOHO_ACCOUNT_ID")
ZOHO_ACCOUNTS_URL: str = _get("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.com").rstrip("/")
ZOHO_MAIL_API_BASE: str = _get("ZOHO_MAIL_API_BASE").rstrip("/")
ZOHO_OAUTH_TOKEN_FILE: str = _get("ZOHO_OAUTH_TOKEN_FILE", "./data/private/zoho_oauth_token.json")
ZOHO_INITIAL_SYNC_FROM: str = _get("ZOHO_INITIAL_SYNC_FROM")
ZOHO_SYNC_OVERLAP_HOURS: int = int(_get("ZOHO_SYNC_OVERLAP_HOURS", "48"))
ZOHO_FOLDER_PAGE_LIMIT: int = int(_get("ZOHO_FOLDER_PAGE_LIMIT", "200"))
ZOHO_MAIL_SYNC_DRY_RUN: bool = _get("ZOHO_MAIL_SYNC_DRY_RUN", "true").lower() in ("true", "1", "yes")
ZOHO_COMPANY_DISCOVERY_ENABLED: bool = _get("ZOHO_COMPANY_DISCOVERY_ENABLED", "true").lower() in ("true", "1", "yes")
ZOHO_DISCOVERY_SEED_FILE: str = _get("ZOHO_DISCOVERY_SEED_FILE", "./data/discovery/zoho_mail_candidates.txt")
ZOHO_DISCOVERY_MIN_CONFIDENCE: float = float(_get("ZOHO_DISCOVERY_MIN_CONFIDENCE", "0.65"))

ZOHO_SKIP_FOLDERS: list[str] = _get_list(
    "ZOHO_SKIP_FOLDERS",
    "drafts,spam,trash,templates,outbox",
)

# ── Health endpoint ────────────────────────────────────────────────────────
HEALTH_PORT: int = int(_get("HEALTH_PORT", "8080"))

# ── Database ───────────────────────────────────────────────────────────────
DATABASE_PATH: str = _get("DATABASE_PATH", "./data/jobs.db")

# ── Logging ────────────────────────────────────────────────────────────────
LOG_LEVEL: str = _get("LOG_LEVEL", "INFO")
LOG_FILE: str = _get("LOG_FILE", "./logs/job_bot.log")

# ── HTTP defaults ──────────────────────────────────────────────────────────
HTTP_TIMEOUT: int = 10  # seconds
HTTP_MAX_RETRIES: int = 3
