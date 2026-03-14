# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.1] — 2025-07-10

### Changed

- **ReliefWeb: migrated from POST API to RSS feeds** — The JSON API now requires a pre-approved appname (since Nov 2025). Switched to public RSS feeds (`reliefweb.int/jobs/rss.xml`) which need no authentication. Same 3 career category queries (ICT, PPM, IM), same tech-title filtering, same dedup. Removed `RELIEFWEB_APPNAME` config variable.

### Fixed

- **Scheduler double-shutdown crash** — `SchedulerNotRunningError` on exit (exit code 1). Added `if scheduler.running:` guard in both shutdown handler and finally block.
- **RemoteOK US job leakage** — Jobs with locations like "United States", "Remote - US", "Tampa, FL" got `scope=unknown` and slipped through the fallback accept rule. Added `_NON_EU_LOCATION_PATTERNS` (~45 patterns) to classify these as `restricted` instead.
- **ReliefWeb location extraction** — Org names (e.g. "Videre Est Credere") were sometimes returned as the job location. Fixed by prioritizing HTML `<div class="tag country">` parsing over tag iteration.

### Testing

- 204 tests across 5 test files (reliefweb tests fully rewritten for RSS format)

## [0.1.0] — 2025-07-10

First complete release. The bot scans 6 job boards, filters for remote EU/Germany tech roles, classifies NGO positions, and notifies via Discord and Telegram.

### Sources

- **Remotive** — JSON API, pagination, all remote tech jobs
- **Arbeitnow** — JSON API, Germany/EU focused, defaults to DE location when missing
- **RemoteOK** — JSON feed with User-Agent requirement
- **We Work Remotely** — RSS/XML feed parsing via feedparser
- **Idealist** — Algolia search API with multi-index queries (jobs + internships)
- **ReliefWeb** — RSS feeds with 3 concurrent category queries (ICT, Program/Project Management, Information Management), tech-title filtering for non-ICT categories, URL-based dedup

### Filters

- **Location** — classifies remote scope (worldwide/continent/eu/country/restricted), accepts EU-accessible roles, rejects UK-only/US-only/restricted
- **Role** — two-stage: reject non-dev titles (HR, sales, marketing, intern, etc.), then require positive dev keyword match
- **Language** — English-only via langdetect with accept-on-uncertainty fallback
- **NGO classifier** — score-based: company name keywords (+1), description keywords (+1), curated org list (+2); threshold configurable via `MIN_NGO_SCORE`
- **Recency** — configurable max age (14 days default), per-source overrides (30 days for ReliefWeb)

### Notifications

- **Discord** — rich embeds via discord-webhook; green = NGO, blue = general; optional separate NGO webhook channel
- **Telegram** — HTML-formatted messages via python-telegram-bot; rate limit handling with automatic retry

### Infrastructure

- Async throughout (httpx + aiosqlite + APScheduler AsyncIOScheduler)
- SQLite storage with content-hash deduplication across scans
- APScheduler: 45-minute scan cycle, 6-hour digest summary, hourly health check
- Graceful shutdown on SIGINT/SIGTERM
- Loguru logging with file rotation (10MB, 7-day retention)
- Per-company cap (max 2 jobs per employer per scan)

### CLI

- `--dry-run` — one-shot scan, print results, no DB writes or notifications
- `--source NAME` — test a single source in isolation
- `--max-age DAYS` — override max job age for this run
- `--verbose` — show all rejected jobs grouped by rejection reason
- `--stats` — print database statistics dashboard and exit

### Testing

- 210 tests across 5 test files (initial release)
- Full coverage of all filters, sources (mocked HTTP), pipeline logic, database stats, and CLI display
