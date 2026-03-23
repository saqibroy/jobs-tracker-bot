"""Centralized configuration — loads .env and exposes typed settings."""

from __future__ import annotations

import os
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
