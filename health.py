"""Lightweight aiohttp health endpoint.

GET /health → 200 with JSON stats (uptime, last scan, jobs tracked).
Runs on port 8080 alongside the APScheduler. Does NOT expose sensitive data.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

from aiohttp import web
from loguru import logger

import config
from models.scan import sanitize_source_error

# ── Module-level state (updated by main.py) ────────────────────────────────
_start_time: float = time.monotonic()
_last_scan_time: datetime | None = None
_next_scan_seconds: int = config.SOURCE_GROUP_A_STARTUP_DELAY_MINUTES * 60
_next_scan_time: datetime | None = None
_jobs_tracked: int = 0
_paused: bool = False
_scan_summary: dict = {}
_ready: bool = False
_ready_at: datetime | None = None

_LEGACY_SUMMARY_KEYS = (
    "raw",
    "eligible_role_matches",
    "rejected",
    "immediate",
    "digest",
    "explore",
    "diagnostic",
)


def set_last_scan(dt: datetime) -> None:
    """Record when the last scan completed."""
    global _last_scan_time
    _last_scan_time = dt


def set_jobs_tracked(count: int) -> None:
    """Update the total jobs tracked count."""
    global _jobs_tracked
    _jobs_tracked = count


def set_next_scan_seconds(seconds: int) -> None:
    """Update time until next scan."""
    global _next_scan_seconds, _next_scan_time
    _next_scan_seconds = max(0, int(seconds))
    _next_scan_time = datetime.now(timezone.utc) + timedelta(seconds=_next_scan_seconds)


def set_next_scan_time(when: datetime | None) -> None:
    """Record the soonest source-group trigger for dynamic health output."""

    global _next_scan_time, _next_scan_seconds
    _next_scan_time = when
    if when is None:
        _next_scan_seconds = 0
        return
    normalized = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    _next_scan_seconds = max(
        0,
        int((normalized - datetime.now(timezone.utc)).total_seconds()),
    )


def set_core_ready(ready: bool) -> None:
    """Publish deterministic local-core readiness transitions."""

    global _ready, _ready_at
    _ready = bool(ready)
    _ready_at = datetime.now(timezone.utc) if _ready else None


def is_core_ready() -> bool:
    return _ready


def set_paused(paused: bool) -> None:
    """Update paused state."""
    global _paused
    _paused = paused


def is_paused() -> bool:
    """Return current paused state."""
    return _paused


def set_scan_summary(summary: dict) -> None:
    """Publish non-sensitive counts from the most recent completed scan."""

    global _scan_summary
    compact: dict = {
        key: max(0, int(summary.get(key, 0) or 0))
        for key in _LEGACY_SUMMARY_KEYS
    }
    compact["accepted"] = max(
        0,
        int(summary.get("accepted", compact["eligible_role_matches"]) or 0),
    )
    compact["unseen"] = max(0, int(summary.get("unseen", 0) or 0))
    compact["saved"] = max(0, int(summary.get("saved", 0) or 0))
    compact["scope"] = str(summary.get("scope") or "legacy_all")[:40]

    group_last_completed = summary.get("group_last_completed", {})
    compact["group_last_completed"] = (
        {
            str(scope)[:40]: str(completed_at)[:40]
            for scope, completed_at in list(group_last_completed.items())[:3]
            if completed_at
        }
        if isinstance(group_last_completed, dict)
        else {}
    )
    source_http = summary.get("source_http", {})
    compact["source_http"] = (
        {
            "configured_limit": max(0, int(source_http.get("configured_limit", 0) or 0)),
            "observed_peak": max(0, int(source_http.get("observed_peak", 0) or 0)),
            "attempts": max(0, int(source_http.get("attempts", 0) or 0)),
            "retries": max(0, int(source_http.get("retries", 0) or 0)),
            "rate_limits": max(0, int(source_http.get("rate_limits", 0) or 0)),
        }
        if isinstance(source_http, dict)
        else {
            "configured_limit": 0,
            "observed_peak": 0,
            "attempts": 0,
            "retries": 0,
            "rate_limits": 0,
        }
    )

    sources = summary.get("sources", {})
    if isinstance(sources, dict):
        compact["sources"] = {
            str(name)[:80]: max(0, int(count or 0))
            for name, count in list(sources.items())[:50]
        }
    else:
        compact["sources"] = {}

    rejection_counts = summary.get("rejection_counts", {})
    compact["rejection_counts"] = (
        {
            str(code)[:80]: max(0, int(count or 0))
            for code, count in list(rejection_counts.items())[:20]
        }
        if isinstance(rejection_counts, dict)
        else {}
    )

    source_health = summary.get("source_health", {})
    clean_health: dict[str, dict] = {}
    if isinstance(source_health, dict):
        for name, item in list(source_health.items())[:50]:
            if not isinstance(item, dict):
                continue
            clean_health[str(name)[:80]] = {
                key: item.get(key)
                for key in (
                    "status",
                    "raw",
                    "accepted",
                    "saved",
                    "issue_count",
                    "last_completed_at",
                    "last_usable_at",
                    "last_fully_successful_at",
                )
            }
            clean_health[str(name)[:80]]["sanitized_error"] = sanitize_source_error(
                item.get("sanitized_error")
            )
    compact["source_health"] = clean_health
    _scan_summary = compact


def get_scan_summary() -> dict:
    """Return a copy of the latest non-sensitive scan summary."""
    return dict(_scan_summary)


def get_last_scan_time() -> datetime | None:
    """Return the latest successful scan timestamp."""
    return _last_scan_time


async def _health_handler(request: web.Request) -> web.Response:
    """Handle GET /health requests."""
    uptime = time.monotonic() - _start_time

    next_scan_seconds = _next_scan_seconds
    if _next_scan_time is not None:
        normalized = (
            _next_scan_time
            if _next_scan_time.tzinfo
            else _next_scan_time.replace(tzinfo=timezone.utc)
        )
        next_scan_seconds = max(
            0,
            int((normalized - datetime.now(timezone.utc)).total_seconds()),
        )

    data = {
        "status": "paused" if _paused else "ok",
        "ready": _ready,
        "ready_at": _ready_at.isoformat() if _ready_at else None,
        "uptime_seconds": int(uptime),
        "last_scan": _last_scan_time.isoformat() if _last_scan_time else None,
        "jobs_tracked": _jobs_tracked,
        "next_scan_in_seconds": next_scan_seconds,
        "last_scan_summary": _scan_summary,
    }
    return web.json_response(data)


async def start_health_server(port: int | None = None) -> web.AppRunner:
    """Start the health HTTP server on the given port.

    Returns the AppRunner so the caller can clean it up on shutdown.
    """
    port = port or config.HEALTH_PORT

    app = web.Application()
    app.router.add_get("/health", _health_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logger.info("Health endpoint running on http://0.0.0.0:{}/health", port)
    return runner
