# Job Tracker Bot — v1.1 Improvement Prompt

This is a continuation of the job-bot project. The bot is live and running.
Here are all the improvements needed. Work through them in order, testing each
before moving to the next.

---

## 🔴 CRITICAL: Fix location filter — US/Canada jobs slipping through

The following job types are appearing in Discord notifications and must be rejected:

- "Canada (Remote)" → scope=unknown → currently ACCEPTED (wrong)
- "United States" → scope=unknown → currently ACCEPTED (wrong)  
- "Remote, US" → scope=unknown → currently ACCEPTED (wrong)
- "Remote - US" → scope=unknown → currently ACCEPTED (wrong)
- "Tampa, FL" → scope=unknown → currently ACCEPTED (wrong)
- "Worldwide (worldwide)" from remotive — needs stricter validation

### Fix 1 — scope=unknown must DEFAULT TO REJECT

Change the pipeline so that `scope=unknown` is REJECTED by default.
Only accept a job if scope is explicitly one of: `worldwide`, `eu`, `germany`, 
`continent` (when continent includes Europe).

This is the most important fix. Unknown scope = we don't know where it's 
accessible from = don't notify.

### Fix 2 — RemoteOK location parsing

RemoteOK jobs are arriving with raw locations like:
- "Canada (Remote)" → must be parsed as restricted (non-EU) → REJECT
- "United States" → REJECT
- "Remote, US" → REJECT  
- "Worldwide" → ACCEPT as worldwide
- "Europe" → ACCEPT as eu
- "Germany" → ACCEPT as germany
- "Remote" (no country) → ACCEPT as worldwide (benefit of the doubt for 
  RemoteOK since it's a remote-only board)

Add a `_parse_remoteok_location()` method in `remoteok.py` that maps raw 
location strings to remote_scope BEFORE handing off to the main location filter.

### Fix 3 — "Worldwide" text needs corroboration

A job saying "Worldwide" in the location field should only be accepted as 
worldwide if:
- The source is a remote-only board (RemoteOK, WeWorkRemotely, Remotive), OR
- The raw location explicitly says "Worldwide", "Work from anywhere", 
  "Remote - Worldwide", "Global"

Do NOT accept "Worldwide" if it appears to be a default/empty value. 
Add a confidence check: if location is just "Worldwide" with no other 
signal and source is arbeitnow, classify as germany (not worldwide).

### Fix 4 — Add explicit country blocklist to location filter

Add a COUNTRY_BLOCKLIST in `filters/location.py`. If the location field 
contains any of these as the primary location, REJECT:

```python
COUNTRY_BLOCKLIST = [
    "united states", "usa", "us only", "remote us", "remote - us",
    "canada", "canada only", "remote canada",
    "australia", "new zealand", "brazil", "india", "nigeria",
    "singapore", "japan", "south korea", "china",
    "united kingdom", "uk only", "london", "england",
    "mexico", "argentina", "colombia",
]
```

Exception: if location ALSO contains "worldwide" or "europe" or an EU country 
name → override blocklist and ACCEPT.

Add 15+ tests for these new cases in test_filters.py.

---

## 🟡 Role Filter — Tighten and expand tech keywords

Replace the current role keyword list with this expanded, specific list.
A job must match at least ONE keyword from the ACCEPT list AND must NOT 
match any REJECT keyword.

### ACCEPT keywords (title OR tags OR description first 200 chars):

```python
ROLE_ACCEPT_KEYWORDS = [
    # Core titles
    "full stack", "fullstack", "full-stack",
    "frontend", "front-end", "front end",
    "backend", "back-end", "back end",
    "software engineer", "software developer",
    "web developer", "web engineer",
    
    # Languages & frameworks
    "react", "next.js", "nextjs", "vue", "vue.js", "nuxt",
    "typescript", "javascript", "node.js", "nodejs",
    "python", "django", "fastapi", "flask",
    "ruby on rails", "ruby", "rails", "php", "symfony", "laravel",
    
    # Infrastructure & tools
    "docker", "kubernetes", "ci/cd", "gitlab ci", "github actions",
    "devops", "platform engineer", "site reliability",
    
    # Data & AI
    "postgresql", "mysql", "graphql", "apollo graphql", "rest api",
    "llm", "rag", "ai engineer", "machine learning engineer",
    
    # Other tech
    "tailwindcss", "tailwind",
    "accessibility", "a11y",
    "seo engineer", "seo developer",
]
```

### REJECT keywords (title only — hard reject before positive match):

```python
ROLE_REJECT_KEYWORDS = [
    # Non-dev roles
    "office assistant", "executive assistant", "virtual assistant",
    "brand manager", "marketing manager", "growth marketing",
    "sales", "account executive", "account manager", "business development",
    "customer success", "customer support", "support agent",
    "recruiter", "talent acquisition", "hr manager", "people ops",
    "finance manager", "accountant", "bookkeeper",
    "content writer", "copywriter", "social media",
    "graphic designer", "ui designer", "ux designer",  # keep "ux engineer"
    "project manager",  # keep "technical project manager" via positive match
    "scrum master", "agile coach",
    
    # Seniority/type we don't want
    "intern", "internship", "working student", "werkstudent",
    "praktikum", "praktikant", "apprentice", "trainee",
    "c-suite", "chief", "vp of", "vice president",
    "android engineer", "ios engineer", "mobile engineer",  # native mobile
    "embedded", "firmware", "hardware engineer",
    "data analyst", "business analyst", "financial analyst",
    "seo specialist", "seo manager",  # not seo engineer/developer
]
```

---

## 🟡 Add Job Match Score

Add a match score (0–100%) to every job based on how well it matches 
the user's specific tech stack. Show it in notifications.

### Implementation in `filters/match.py` (new file):

```python
# User's tech stack — weighted by preference
STACK_WEIGHTS = {
    # High value (the core stack)
    "react": 15, "typescript": 12, "nextjs": 12, "next.js": 12,
    "python": 10, "django": 10, "fastapi": 8,
    "postgresql": 8, "graphql": 8, "rest api": 6,
    "node": 8, "nodejs": 8,
    
    # Medium value
    "docker": 6, "ci/cd": 5, "github actions": 5, "gitlab ci": 5,
    "tailwind": 4, "tailwindcss": 4,
    "vue": 4, "javascript": 5,
    "llm": 7, "rag": 7, "ai": 5,
    
    # Bonus signals
    "ngo": 10, "nonprofit": 10, "social impact": 8,
    "open source": 5, "digital rights": 6, "civic tech": 8,
    
    # Stack mentions in description
    "ruby": 4, "rails": 4, "php": 3, "symfony": 3,
    "mysql": 3, "redis": 3,
}
```

Score = sum of matched keyword weights, capped at 100.
Normalize: if raw score > 60 → 95%+ match, scale accordingly.

### Show in notifications:

Discord embed — add a new field:
```
📊 Match   ████████░░  78%
```
Use Unicode block characters for the bar:
- `█` for filled portion
- `░` for empty portion  
- 10 blocks total, so 78% = 8 filled + 2 empty

Telegram:
```
📊 78% match ████████░░
```

Add `match_score: int` field to the Job model.
Calculate score in the filter pipeline after NGO classification, 
before deduplication.

Sort final accepted jobs by match_score DESC before sending notifications
so highest-match jobs appear first in Discord.

---

## 🟡 Add Company Location Details

For arbeitnow jobs, the API often includes city and sometimes postal code.
For other sources, try to extract from the location string.

### Add fields to Job model:
```python
company_city: str | None = None
company_postal_code: str | None = None  
company_country: str | None = None
```

### In arbeitnow.py — parse from API response:
The arbeitnow API returns a `location` field. Parse it:
- "Berlin" → city=Berlin, country=Germany
- "13086 Berlin" → postal=13086, city=Berlin, country=Germany
- "Hamburg, Germany" → city=Hamburg, country=Germany

### Show in Discord embed:
```
🏢 Company    Velsa UG
📍 Location   13086 Berlin, Germany · Remote
```

For sources that don't provide city/postal, show what's available 
from the location string. Don't show empty fields.

---

## 🟡 Discord Stats Command

Add a `/stats` command to the Discord bot (or use a webhook-based approach):

When the bot receives a message containing just `stats` in the designated 
Discord channel, it should reply with a stats embed showing:

```
📊 Job Tracker Stats
━━━━━━━━━━━━━━━━━━━━
📦 Total tracked    1,247
🆕 Last 24 hours    43
🟢 NGO jobs         89
🔵 General          1,158

📡 Sources (all time)
remotive        ████████  312
arbeitnow       ██████    241  
remoteok        █████     198
...

🏆 Top companies
Mozilla Foundation  12
Wikimedia          8
...

🕐 Last scan    4 minutes ago
🔄 Next scan    in 41 minutes
```

Send this as a Discord embed (not a plain message).

### Implementation approach:
Use a Discord bot (discord.py) alongside the existing webhook notifications.
The bot listens for messages in a configured channel.

Add to .env:
```
DISCORD_BOT_TOKEN=         # separate from webhook URL
DISCORD_COMMAND_CHANNEL_ID=  # channel where bot listens for commands
```

---

## 🟡 Manual Scan Trigger

When the Discord bot receives a message containing just `r` (or `refresh` 
or `scan`) in the command channel, it should:

1. Reply immediately: "🔄 Scanning now..."
2. Trigger a full scan cycle (same as the scheduled scan)
3. Send results as normal job notifications
4. Reply with a summary: "✅ Scan complete — 3 new jobs found"
   or "✅ Scan complete — no new jobs"

This works because discord.py runs in the same asyncio event loop as 
APScheduler, so the scan coroutine can be called directly.

Commands to support:
- `r` or `refresh` or `scan` → trigger immediate scan
- `stats` → show stats embed
- `help` → show available commands

---

## 🟡 Docker Setup

Create a production-ready Docker setup for local use first, 
then cloud deployment.

### Files to create:

**`Dockerfile`:**
```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for Playwright (for future scrapers)
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create data and logs directories
RUN mkdir -p data logs

CMD ["python", "main.py"]
```

**`docker-compose.yml`:**
```yaml
version: '3.8'

services:
  job-bot:
    build: .
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./data:/app/data      # persist SQLite DB
      - ./logs:/app/logs      # persist logs
    healthcheck:
      test: ["CMD", "python", "-c", "import sqlite3; sqlite3.connect('data/jobs.db').execute('SELECT 1')"]
      interval: 5m
      timeout: 10s
      retries: 3
```

**`.dockerignore`:**
```
venv/
__pycache__/
*.pyc
*.pyo
.env
.git/
tests/
*.md
```

### Local Docker commands to document in README:
```bash
# Build and start
docker-compose up -d --build

# View logs
docker-compose logs -f

# Trigger manual scan (dry run inside container)
docker-compose exec job-bot python main.py --dry-run

# Stop
docker-compose down

# Update after code changes
docker-compose up -d --build
```

---

## 🟡 Deployment Recommendation

Add a section to README.md with honest deployment advice:

### Recommended: Hetzner CX22 (€4.51/month)
- 2 vCPU, 4GB RAM, 40GB disk
- Located in Germany (low latency for EU job boards)
- No free tier limits, no sleep/cold starts
- Full control, systemd for auto-restart
- Best for an always-on bot that scans every 45 minutes

### Why free tiers don't work well for this bot:
- **Railway free tier**: 500 hours/month = ~20 days. Bot sleeps after that.
- **Render free tier**: Spins down after 15 minutes of inactivity. 
  A background worker with no HTTP traffic = always sleeping.
- **Fly.io free tier**: 3 shared VMs, but memory limits are tight for 
  Playwright (future scrapers need 512MB+).

### Recommended cloud path:
1. Run in Docker locally (docker-compose up -d) to validate 
2. When ready for server: Hetzner CX22 + Docker + systemd 
   (or just docker-compose restart policy)
3. If you want managed: Railway Hobby ($5/month) is the 
   cheapest paid tier that actually stays alive

---

## Build Order

1. Fix location filter (scope=unknown reject + country blocklist) — CRITICAL
2. Expand role keywords
3. Add match score (match.py + Job model field + Discord bar)
4. Add company city/postal to Job model + arbeitnow parsing
5. Discord bot setup (discord.py) — stats command + manual scan trigger
6. Docker setup (Dockerfile + docker-compose.yml)
7. Update README with Docker instructions + deployment recommendations
8. Run full test suite — must stay green throughout

After each step, run:
```bash
python main.py --dry-run --verbose
```
and confirm no US/Canada jobs appear before moving to the next step.

---

## Test Requirements

After all changes:
- All existing 210 tests must still pass
- Add 15+ new tests for location blocklist
- Add 10+ tests for match score calculation  
- Add 5+ tests for company location parsing
- Total target: 240+ tests
