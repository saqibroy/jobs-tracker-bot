# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.3.0] — 2026-03-15

### Added

- **GitHub Actions CI/CD** (`.github/workflows/deploy.yml`):
  - Runs all tests on push to `main` (Python 3.11 + Playwright)
  - Auto-deploys to Oracle Cloud via SSH if tests pass
  - Uses `appleboy/ssh-action` with `ORACLE_HOST` and `ORACLE_SSH_KEY` secrets

- **Health endpoint** (`health.py`):
  - `GET /health` on port 8080 returns JSON: status, uptime, last scan, jobs tracked, next scan countdown
  - Paused state support (shows `"status": "paused"` when scanning is paused)
  - Runs alongside the scheduler via aiohttp

- **Startup & crash Discord notifications**:
  - Startup embed: source count, server info, blocklist/Playwright status
  - Crash embed: error message + auto-restart notice
  - Sent via Discord webhook on bot start and unhandled exceptions

- **Company blocklist** (`COMPANY_BLOCKLIST` env var):
  - Comma-separated company names to always skip (case-insensitive substring match)
  - Integrated into `_apply_filters()` pipeline

- **Senior-only filter** (`FILTER_SENIOR_ONLY` env var, default off):
  - Accepts senior/lead/staff/principal titles
  - Rejects junior/mid-level titles
  - No seniority mention → assume senior (accept)

- **Salary filter** (`MIN_SALARY_EUR` env var, default 0 = off):
  - Parses salary strings, annualizes monthly values
  - Rejects jobs with explicit salary below threshold
  - Unparseable salary → accept (benefit of the doubt)

- **Telegram /commands** (`/scan`, `/stats`, `/help`, `/pause`, `/resume`):
  - Full python-telegram-bot Application with CommandHandlers
  - `register_commands()` registers commands with BotFather API
  - `/pause` and `/resume` control scanning via health module paused state
  - Integrated into `main()` event loop alongside scheduler and Discord bot

- **DISABLE_PLAYWRIGHT** env var:
  - When `true`, all Playwright sources are skipped (saves ~50MB RAM)
  - Dockerfile conditionally installs Playwright via `INSTALL_PLAYWRIGHT` build arg

- **MAX_CONCURRENT_SOURCES** env var (default 6):
  - Limits concurrent source fetches to reduce peak memory
  - Sources run in batches when set lower than total source count

- **Docker log rotation** in docker-compose.yml:
  - `json-file` driver with `max-size: 10m`, `max-file: 3` (30MB cap)

- **Server scripts**:
  - `scripts/update.sh` — git pull + docker rebuild + health check
  - `scripts/backup.sh` — SQLite backup, keeps last 7

- **Digest improvements**:
  - Only sends digest if there are jobs OR no scan ran in last 2 hours (health alert)
  - Health alert embed (red) when no scans completed recently

- **45 new tests** (`tests/test_v13_features.py`):
  - Health endpoint JSON, paused state
  - Company blocklist (6 tests including pipeline integration)
  - Senior filter (8 tests)
  - Salary filter (6 tests)
  - DISABLE_PLAYWRIGHT (3 tests)
  - Startup/crash notifications (3 tests)
  - Telegram commands (4 tests)
  - Concurrency batching (1 test)
  - Config defaults (6 tests)

### Changed

- **Dockerfile** — multi-stage with `INSTALL_PLAYWRIGHT` build arg; conditionally installs system deps + Chromium
- **docker-compose.yml** — port 8080 exposed, build args, log rotation, healthcheck via `/health` endpoint
- **config.py** — 6 new env vars: `COMPANY_BLOCKLIST`, `FILTER_SENIOR_ONLY`, `MIN_SALARY_EUR`, `DISABLE_PLAYWRIGHT`, `MAX_CONCURRENT_SOURCES`, `HEALTH_PORT`
- **main.py** — Telegram bot integration in event loop, scan concurrency batching, paused state check in scheduled scan
- **requirements.txt** — added `aiohttp>=3.9.0`
- **.env.example** — all new variables documented
- **README.md** — CI/CD, Monitoring, Telegram Commands, Scripts sections; updated features list, config table, project structure

### Stats

- Total tests: **520+** (was 475)
- Test files: **7** (was 6)

## [1.2.0] — 2025-07-12

### Added

- **5 new job sources** (total: 11):
  - **Tech Jobs for Good** — Playwright + BeautifulSoup scraper for Cloudflare-protected NGO/impact tech board; all listings classified as `is_ngo=True`
  - **EuroBrussels** — httpx + BeautifulSoup scraper for EU-focused NGO/policy/civil society jobs; link dedup preferring text links, company from `div.companyName`, location from `div.location`, NGO classification from category tags
  - **80,000 Hours** — Playwright-based scraper for JS-rendered Effective Altruism job board; card parser with `p.font-bold span` for title, text-line parsing for company/location/tags, relative time parsing
  - **GoodJobs.eu** — httpx + BeautifulSoup scraper for German/EU mission-driven organisations; title from `h3`, company from `div.mb-1 > p`, salary extraction, German legal form NGO detection (gGmbH, e.V., Stiftung)
  - **Devex** — JSON API scraper (`/api/public/search/jobs`) for international development sector; structured JSON parsing with places array for location, topics for tags; all listings `is_ngo=True`

- **Playwright infrastructure** (`sources/playwright_base.py`):
  - Shared async Playwright browser context manager for headless Chromium
  - `get_playwright_page()` — standalone page context manager
  - `shared_browser_context()` — shared browser for multiple sources
  - `new_page_from_browser()` — create pages from shared browser
  - Resource blocking (images/fonts) for faster scraping
  - Realistic fingerprint (User-Agent, viewport 1280×800, locale en-US)

- **Playwright source orchestration** in `main.py`:
  - `_PLAYWRIGHT_SOURCES` set for hours80k and techjobsforgood
  - `_run_playwright_sources()` — launches one shared Chromium browser, runs all Playwright sources concurrently, 90s combined timeout
  - Automatic separation of httpx vs Playwright sources in `run_scan()`

- **Impact board scope defaults** — hours80k and idealist jobs with `remote_scope="unknown"` now default to `"worldwide"`

- **Modern Discord embed styling**:
  - New colour scheme: emerald green (NGO), indigo (general), amber (high match ≥ 60%)
  - Description-based layout with company, location, salary, and match score
  - Source-specific emoji icons (🟣 remotive, 🔴 arbeitnow, 🟠 remoteok, etc.)
  - Match score labels: 🔥 Excellent (≥80%), ⭐ Strong (≥50%), 📊 Moderate (≥20%)
  - Tag chips in `code` formatting
  - `set_author()` for category badges (🏛️ NGO / Nonprofit, 💼 General)
  - Batch header message for multi-job notifications with source list
  - Relative time display in footer (a few minutes ago, X hours ago, X days ago)
  - Modern digest embed with source-specific icons and violet colour

### Changed

- Discord notifier completely rewritten for modern embed design
- Digest embed in `main.py` updated with source icons, database field, violet colour
- Docker image includes Playwright + Chromium (`playwright install --with-deps chromium`)
- `docker-compose.yml` updated with `shm_size: 256mb` for Chromium

### Fixed

- **hours80k 0 jobs** — `.job-card` selector now prioritized over `a[href*='/job/']` (which matched icon elements); card parser rewritten for actual HTML structure
- **techjobsforgood tuple unpacking** — `new_page_from_browser()` returns `(context, page)`, fixed destructuring + added `context.close()`
- **techjobsforgood Cloudflare** — Added hard IP block detection with clear warning message, graceful skip
- **eurobrussels link dedup** — Prefers text links over image/logo links; fixed title/company/location CSS selectors
- **goodjobs title/company** — Title from `h3` instead of full card text; company from `div.mb-1 > p` with GoodCompany/Nachhaltigkeits filter
- **devex complete rewrite** — From HTML scraping to JSON API for reliable structured data

### Testing

- 475 tests across 6 test files
- New `test_new_sources.py` with 200+ tests covering all 5 new sources, Playwright base, source registration, filter integration, Discord relative time formatting, and company display

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
