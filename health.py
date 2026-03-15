"""Lightweight aiohttp health endpoint.

GET /health → 200 with JSON stats (uptime, last scan, jobs tracked).
Runs on port 8080 alongside the APScheduler. Does NOT expose sensitive data.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from aiohttp import web
from loguru import logger

import config

# ── Module-level state (updated by main.py) ────────────────────────────────
_start_time: float = time.monotonic()
_last_scan_time: datetime | None = None
_next_scan_seconds: int = config.SCAN_INTERVAL_MINUTES * 60
_jobs_tracked: int = 0
_paused: bool = False


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
    global _next_scan_seconds
    _next_scan_seconds = seconds


def set_paused(paused: bool) -> None:
    """Update paused state."""
    global _paused
    _paused = paused


def is_paused() -> bool:
    """Return current paused state."""
    return _paused


async def _health_handler(request: web.Request) -> web.Response:
    """Handle GET /health requests."""
    uptime = time.monotonic() - _start_time

    data = {
        "status": "paused" if _paused else "ok",
        "uptime_seconds": int(uptime),
        "last_scan": _last_scan_time.isoformat() if _last_scan_time else None,
        "jobs_tracked": _jobs_tracked,
        "next_scan_in_seconds": _next_scan_seconds,
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
