# 🔍 Job Tracker Bot

A fully async Python bot that monitors **11 remote job boards**, filters for full-stack developer positions accessible from the EU/Germany, classifies NGO/nonprofit roles, and sends modern rich notifications to Discord and Telegram.

Built for a specific use case: finding remote tech roles at NGOs and impact-driven organizations, while also catching good general remote positions.

## Features

### Sources (11 job boards)

- **Remotive** — JSON API, all remote tech jobs
- **Arbeitnow** — JSON API, Germany/EU focused
- **RemoteOK** — JSON feed with User-Agent requirement
- **We Work Remotely** — RSS/XML feed parsing
- **Idealist** — Algolia search API with multi-index queries
- **ReliefWeb** — RSS feeds (3 career categories: ICT, PPM, IM)
- **Tech Jobs for Good** — Playwright + BeautifulSoup (Cloudflare-protected, NGO/impact)
- **EuroBrussels** — httpx + BeautifulSoup (EU-focused NGO/policy/civil society)
- **80,000 Hours** — Playwright-based scraper (Effective Altruism / impact sector)
- **GoodJobs.eu** — httpx + BeautifulSoup (DE/EU mission-driven organisations)
- **Devex** — JSON API (international development sector, NGO/INGO)

### Filtering & Classification

- **Smart location filter** — accepts worldwide remote, EU remote, and Germany-based roles; rejects UK-only, US-only, and other restricted postings. Unknown scope defaults to reject, country blocklist, worldwide corroboration
- **Role filter** — two-stage: rejects non-dev titles (HR, marketing, intern, native mobile, etc.), then requires a positive dev keyword match (50+ keywords including React, Next.js, Django, Docker, LLM)
- **Language filter** — English-only postings (uses `langdetect`, defaults to accept on uncertainty)
- **NGO classifier** — score-based detection using company name, description keywords, and a curated org list
- **Match score** — 0–100% score based on tech stack keyword weights, shown as a visual bar in notifications
- **Company location details** — parses city/postal/country from arbeitnow location strings
- **Recency filter** — configurable max age (14 days default, 30 for ReliefWeb)
- **Content dedup** — both in-memory (per scan) and database-backed (across scans) using URL hash + content hash
- **Per-company cap** — max 2 jobs per employer per scan to prevent flooding

### Notifications

- **Discord notifications** — modern rich embeds with colour-coded cards:
  - 🟢 **Emerald green** = NGO/nonprofit/humanitarian
  - 🟣 **Indigo** = general remote tech
  - 🟡 **Amber** = high match score (≥ 60%)
  - Batch header summarizing incoming jobs
  - Source-specific emoji icons for visual differentiation
  - Match score labels (🔥 Excellent, ⭐ Strong, 📊 Moderate)
  - Tag chips in `code` formatting, relative time display
  - Optional separate NGO webhook channel
  - **Startup/crash notifications** — Discord embeds on bot start/restart and crashes
- **Discord bot** — `stats`, `scan`/`r`/`refresh`, and `help` commands via discord.py
- **Telegram notifications** — HTML-formatted messages with rate limit handling and match score
- **Telegram bot commands** — `/scan`, `/stats`, `/help`, `/pause`, `/resume`

### Filtering Extras

- **Company blocklist** — skip jobs from configured companies (e.g. body-shopping firms)
- **Senior-only filter** — optional, only accept senior/lead/staff titles
- **Salary filter** — optional, reject jobs with salary below threshold

### Infrastructure

- **GitHub Actions CI/CD** — auto-deploy to Oracle Cloud on push to `main` (tests run first)
- **Health endpoint** — `GET /health` returns JSON status (uptime, last scan, jobs tracked)
- **Playwright + Chromium** — headless browser for JS-rendered sites, with optional `DISABLE_PLAYWRIGHT` for low-memory servers
- **APScheduler** — 45-minute scan cycle, 6-hour digest summary, hourly health check
- **Docker ready** — multi-stage Dockerfile with optional Playwright, log rotation (30MB cap), memory limits
- **Concurrency control** — `MAX_CONCURRENT_SOURCES` to limit peak RAM usage
- **520+ tests** across 7 test files

## Quick Start

### 1. Clone and install

```bash
git clone <your-repo-url>
cd job-bot

python3 -m venv venv
source venv/bin/activate    # Linux/macOS
# venv\Scripts\activate     # Windows

pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your notification credentials (see setup guides below). The bot runs without notifications configured — it just won't send any.

### 3. Test with a dry run

```bash
# Scan all sources, print results, don't save or notify
python main.py --dry-run

# Test a single source
python main.py --dry-run --source remotive

# See why jobs were rejected
python main.py --dry-run --source reliefweb --verbose

# Check database stats
python main.py --stats
```

### 4. Run the bot

```bash
python main.py
```

This starts the scheduler. The bot will:
- Run an immediate scan on startup
- Scan all sources every 45 minutes
- Send a digest summary every 6 hours
- Log a health check every hour

Press `Ctrl+C` to stop gracefully.

## CLI Reference

```
python main.py [OPTIONS]

Options:
  --dry-run          One-shot scan, print results, no DB writes or notifications
  --source NAME      Test a single source (remotive, arbeitnow, remoteok,
                     weworkremotely, idealist, reliefweb, techjobsforgood,
                     eurobrussels, hours80k, goodjobs, devex)
  --max-age DAYS     Override MAX_JOB_AGE_DAYS for this run
  --verbose          Show all rejected jobs with reasons (use with --dry-run)
  --stats            Print database statistics and exit
```

## Setting Up Discord Notifications

1. Open your Discord server and go to **Server Settings → Integrations → Webhooks**
2. Click **New Webhook**
3. Name it (e.g. "Job Tracker"), pick the channel where job posts should appear
4. Click **Copy Webhook URL**
5. Paste it into `.env`:
   ```
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1234567890/abcdefg...
   ```

**Optional: Separate NGO channel**

If you want NGO/nonprofit jobs in a different Discord channel:
1. Create another webhook in your NGO channel
2. Add it to `.env`:
   ```
   DISCORD_WEBHOOK_URL_NGO=https://discord.com/api/webhooks/0987654321/hijklmn...
   ```

Jobs are colour-coded with modern embeds:
- 🟢 **Emerald green** embeds = NGO/nonprofit/humanitarian
- � **Indigo** embeds = general remote tech
- 🟡 **Amber** embeds = high match score (≥ 60%)

Each embed includes the company name, location with remote scope, match score with visual bar, tag chips, source icon, and relative posting time. Multi-job notifications include a batch header summarizing the count and sources.

## Setting Up Telegram Notifications

### Create a bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts to name your bot
3. BotFather gives you a **bot token** like `7123456789:AAH...` — copy it
4. Add to `.env`:
   ```
   TELEGRAM_BOT_TOKEN=7123456789:AAHxyz...
   ```

### Get your chat ID

1. Start a conversation with your new bot (send it any message)
2. Open this URL in your browser (replace `YOUR_TOKEN`):
   ```
   https://api.telegram.org/botYOUR_TOKEN/getUpdates
   ```
3. Look for `"chat":{"id":123456789}` in the JSON response
4. Add to `.env`:
   ```
   TELEGRAM_CHAT_ID=123456789
   ```

**For a group chat**: Add the bot to the group, send a message, then check `getUpdates`. Group chat IDs are negative numbers (e.g. `-1001234567890`).

## Setting Up the Discord Bot (Commands)

The Discord bot lets you interact with the tracker from Discord (stats, trigger scans). This is separate from webhook notifications.

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application**, name it (e.g. "Job Tracker Bot")
3. Go to **Bot** → click **Reset Token** → copy the token
4. Under **Privileged Gateway Intents**, enable **Message Content Intent**
5. Go to **OAuth2 → URL Generator**, select `bot` scope with permissions: `Send Messages`, `Read Message History`, `Embed Links`
6. Open the generated URL to invite the bot to your server
7. Right-click the channel where you want commands → **Copy Channel ID** (enable Developer Mode in Discord settings if needed)
8. Add to `.env`:
   ```
   DISCORD_BOT_TOKEN=MTIz...your-bot-token
   DISCORD_COMMAND_CHANNEL_ID=1234567890123456789
   ```

**Commands** (type in the configured channel):
- `stats` — Shows total jobs, 24h stats, NGO breakdown, source distribution, top companies
- `scan` / `r` / `refresh` — Triggers an immediate scan cycle
- `help` — Lists available commands

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | — | Main Discord webhook URL |
| `DISCORD_WEBHOOK_URL_NGO` | — | Optional separate webhook for NGO jobs |
| `DISCORD_BOT_TOKEN` | — | Discord bot token for commands (stats, scan) |
| `DISCORD_COMMAND_CHANNEL_ID` | — | Channel ID where bot listens for commands |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token from BotFather |
| `TELEGRAM_CHAT_ID` | — | Target chat/group ID |
| `SCAN_INTERVAL_MINUTES` | `45` | Minutes between scan cycles |
| `DIGEST_INTERVAL_HOURS` | `6` | Hours between digest summaries |
| `MAX_JOB_AGE_DAYS` | `14` | Reject jobs older than this (days) |
| `MAX_JOB_AGE_DAYS_RELIEFWEB` | `30` | Override for ReliefWeb (UN hiring is slow) |
| `LOCATION_ALLOWLIST` | `worldwide,eu,...` | Comma-separated location accept keywords |
| `LOCATION_BLOCKLIST` | `uk only,...` | Comma-separated location reject keywords |
| `MIN_NGO_SCORE` | `1` | Minimum score to classify as NGO |
| `COMPANY_BLOCKLIST` | — | Comma-separated company names to always skip |
| `FILTER_SENIOR_ONLY` | `false` | Only accept senior/lead/staff titles |
| `MIN_SALARY_EUR` | `0` | Reject jobs with salary below this (0 = off) |
| `DISABLE_PLAYWRIGHT` | `false` | Skip Playwright sources (saves ~50MB RAM) |
| `MAX_CONCURRENT_SOURCES` | `6` | Max sources fetched in parallel (lower = less RAM) |
| `HEALTH_PORT` | `8080` | Port for the health HTTP endpoint |
| `DATABASE_PATH` | `./data/jobs.db` | SQLite database file |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG` for verbose logs) |
| `LOG_FILE` | `./logs/job_bot.log` | Log file (rotates at 10MB, 7 days retention) |

## Project Structure

```
job-bot/
├── main.py                       # Entry point, CLI, scheduler, filter pipeline
├── config.py                     # Environment config loader
├── discord_bot.py                # Discord bot (stats, scan, help commands)
├── health.py                     # Lightweight aiohttp health endpoint (/health)
├── requirements.txt
├── .env.example
├── Dockerfile                    # Multi-stage container (optional Playwright)
├── docker-compose.yml            # One-command deployment with log limits
├── .dockerignore
│
├── .github/
│   └── workflows/
│       └── deploy.yml            # CI/CD: test + deploy to Oracle Cloud on push
│
├── scripts/
│   ├── update.sh                 # Manual update script (git pull + rebuild)
│   └── backup.sh                 # SQLite backup script (keeps last 7)
│
├── sources/
│   ├── base.py                   # Abstract BaseSource with retry + rate-limit
│   ├── playwright_base.py        # Shared Playwright browser context manager
│   ├── remotive.py               # Remotive JSON API
│   ├── arbeitnow.py              # Arbeitnow JSON API (DE/EU focus)
│   ├── remoteok.py               # RemoteOK JSON feed
│   ├── weworkremotely.py         # We Work Remotely RSS
│   ├── idealist.py               # Idealist via Algolia search API
│   ├── reliefweb.py              # ReliefWeb RSS feeds (3 career categories)
│   ├── techjobsforgood.py        # Tech Jobs for Good (Playwright + BS4)
│   ├── eurobrussels.py           # EuroBrussels (httpx + BS4, EU/NGO focus)
│   ├── hours80k.py               # 80,000 Hours (Playwright, EA/impact)
│   ├── goodjobs.py               # GoodJobs.eu (httpx + BS4, DE/EU impact)
│   └── devex.py                  # Devex JSON API (intl development sector)
│
├── filters/
│   ├── location.py               # Remote scope classification + location filter
│   ├── role.py                   # Tech role keyword filter (two-stage)
│   ├── language.py               # English-only via langdetect
│   ├── ngo.py                    # NGO/nonprofit score-based classifier
│   └── match.py                  # Weighted match score (0–100%) computation
│
├── models/
│   └── job.py                    # Pydantic Job model with content hashing
│
├── storage/
│   └── database.py               # SQLite via aiosqlite — dedup, stats, digest
│
├── notifiers/
│   ├── base.py                   # Abstract BaseNotifier
│   ├── discord_notifier.py       # Discord modern embeds (emerald/indigo/amber)
│   └── telegram_notifier.py      # Telegram HTML messages + /commands support
│
└── tests/
    ├── test_filters.py           # 120+ tests — location, role, language, NGO, match
    ├── test_main_fixes.py        # 55+ tests — pipeline, recency, verbose, stats
    ├── test_new_sources.py       # 200+ tests — all v1.2 sources, Playwright, integration
    ├── test_idealist.py          # 50 tests — Algolia parsing, multi-query
    ├── test_reliefweb.py         # 27 tests — RSS parsing, PPM/IM feeds
    ├── test_database.py          # 10 tests — stats, dedup, persistence
    └── test_v13_features.py      # 45 tests — health, blocklist, filters, CI/CD
```

## How to Add a New Job Source

1. Create `sources/my_source.py`:

```python
from __future__ import annotations

from loguru import logger
from pydantic import ValidationError

from models.job import Job
from sources.base import BaseSource


class MySource(BaseSource):
    name = "mysource"

    async def fetch(self) -> list[Job]:
        resp = await self._get("https://api.example.com/jobs")
        if resp.status_code == 429:
            return []

        data = resp.json()
        jobs: list[Job] = []

        for item in data.get("results", []):
            try:
                job = Job(
                    title=item["title"],
                    company=item["company"],
                    location=item.get("location", "Remote"),
                    url=item["url"],
                    source=self.name,
                    # ... fill other fields
                )
                jobs.append(job)
            except (ValidationError, KeyError) as exc:
                logger.debug("[{}] Skipping bad item: {}", self.name, exc)

        return jobs
```

2. Register it in `main.py`:

```python
from sources.my_source import MySource

ALL_SOURCES = {
    # ... existing sources ...
    "mysource": MySource,
}
```

3. Test it:

```bash
python main.py --dry-run --source mysource --verbose
```

Key things to know:
- `BaseSource` gives you `self._get()` with retries, timeouts, and 429 handling
- Return raw `Job` objects — don't filter. The pipeline in `main.py` handles all filtering
- Set `is_ngo=True` if the source is exclusively NGO (like ReliefWeb, Devex, 80,000 Hours)
- For JS-rendered sites, use `playwright_base.py` — see `hours80k.py` for an example. Add the source name to `_PLAYWRIGHT_SOURCES` in `main.py`
- Use `safe_fetch()` (called by the scheduler) — it catches all exceptions so one broken source never crashes the bot
- Write tests with mocked HTTP responses

## Running Tests

```bash
source venv/bin/activate
python -m pytest tests/ -v
```

## CI/CD (GitHub Actions)

The project includes automatic deployment via GitHub Actions. On every push to `main`:

1. **Tests run** in CI (Python 3.11, all 520+ tests)
2. **If tests pass**, the workflow SSHes into the Oracle Cloud server and redeploys

### Setup

1. In your GitHub repo, go to **Settings → Secrets → Actions → New repository secret**
2. Add these secrets:
   - `ORACLE_HOST` = `158.180.30.164` (your server IP)
   - `ORACLE_SSH_KEY` = full contents of your SSH private key file (including BEGIN/END lines)

3. On the server, make sure git pull works without interaction:
   ```bash
   cd ~/jobs-tracker-bot
   git checkout main
   git branch --set-upstream-to=origin/main main
   git pull  # should work without prompts
   ```

The workflow file is at `.github/workflows/deploy.yml`.

## Monitoring

### Health Endpoint

The bot exposes a lightweight HTTP health endpoint at `http://<server>:8080/health`.

Response:
```json
{
  "status": "ok",
  "uptime_seconds": 3600,
  "last_scan": "2026-03-15T22:00:00+00:00",
  "jobs_tracked": 1247,
  "next_scan_in_seconds": 1800
}
```

When scanning is paused (via Telegram `/pause`), `status` changes to `"paused"`.

### UptimeRobot (free monitoring)

1. Register at [uptimerobot.com](https://uptimerobot.com) (free tier)
2. Add monitor:
   - Type: HTTP(s)
   - URL: `http://158.180.30.164:8080/health`
   - Interval: 5 minutes
   - Alert: email when down

### Firewall

Open port 8080 on the server:
```bash
sudo firewall-cmd --permanent --add-port=8080/tcp
sudo firewall-cmd --reload
```

### Startup & Crash Notifications

- **Startup**: when the bot starts (or restarts), a Discord embed is sent with source count and config
- **Crash**: if the bot catches an unhandled exception, a Discord alert is sent before exiting
- Docker's `restart: unless-stopped` ensures automatic restart after crashes

## Telegram Commands

The Telegram bot supports these commands (register with BotFather):

| Command | Description |
|---------|-------------|
| `/scan` | Trigger an immediate scan |
| `/stats` | Show job tracking statistics |
| `/help` | List available commands |
| `/pause` | Pause scanning |
| `/resume` | Resume scanning |

## Server Scripts

### Manual update (`scripts/update.sh`)
```bash
~/jobs-tracker-bot/scripts/update.sh
```
Pulls latest code, rebuilds Docker image, shows health status.

### Database backup (`scripts/backup.sh`)
```bash
~/jobs-tracker-bot/scripts/backup.sh
```
Backs up SQLite DB, keeps last 7 backups. Add to crontab for weekly:
```
0 2 * * 0 /home/opc/jobs-tracker-bot/scripts/backup.sh
```

## Deployment Options

The bot is a single long-running Python process. No web server, no ports to open.

### Option 1: Run on your laptop (simplest)

```bash
# Start in background with nohup
nohup python main.py > /dev/null 2>&1 &

# Or use tmux / screen
tmux new -s jobbot
python main.py
# Ctrl+B, D to detach
```

### Option 2: Railway

1. Push your repo to GitHub
2. Go to [railway.app](https://railway.app), create a new project from your repo
3. Add environment variables in the Railway dashboard (same as `.env`)
4. Railway auto-detects Python — set the start command to `python main.py`
5. Free tier gives ~500 hours/month (enough for a bot)

### Option 3: Render

1. Push to GitHub
2. On [render.com](https://render.com), create a new **Background Worker** (not a web service)
3. Set build command: `pip install -r requirements.txt`
4. Set start command: `python main.py`
5. Add env vars in the Render dashboard
6. Free tier available with limitations

### Option 4: Hetzner VPS (recommended for always-on)

1. Get a CX22 VPS (~€4.51/month) at [hetzner.com](https://www.hetzner.com/cloud)
2. SSH in and set up:
   ```bash
   sudo apt update && sudo apt install python3-venv python3-pip
   git clone <your-repo> ~/job-bot
   cd ~/job-bot
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   nano .env  # fill in your tokens
   ```
3. Create a systemd service for auto-restart:
   ```bash
   sudo nano /etc/systemd/system/jobbot.service
   ```
   ```ini
   [Unit]
   Description=Job Tracker Bot
   After=network.target

   [Service]
   Type=simple
   User=your-username
   WorkingDirectory=/home/your-username/job-bot
   ExecStart=/home/your-username/job-bot/venv/bin/python main.py
   Restart=always
   RestartSec=10
   EnvironmentFile=/home/your-username/job-bot/.env

   [Install]
   WantedBy=multi-user.target
   ```
   ```bash
   sudo systemctl enable jobbot
   sudo systemctl start jobbot
   sudo journalctl -u jobbot -f  # watch logs
   ```

### Option 5: Docker (recommended for production)

The Docker image includes Python 3.11, Playwright, and Chromium for scraping JS-rendered sites.

```bash
# Build and start
docker-compose up -d --build

# View logs
docker-compose logs -f

# Run a dry-run inside the container
docker-compose exec job-bot python main.py --dry-run

# Check stats
docker-compose exec job-bot python main.py --stats

# Stop
docker-compose down
```

Data and logs are persisted via Docker volumes (`./data` and `./logs`). The container includes a health check that verifies the process is running.

For a VPS deployment with Docker:
```bash
sudo apt install docker.io docker-compose
git clone <your-repo> ~/job-bot
cd ~/job-bot
cp .env.example .env
nano .env  # fill in your tokens
docker-compose up -d --build
```

## License

MIT
