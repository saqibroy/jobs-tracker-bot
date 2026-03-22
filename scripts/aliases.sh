#!/bin/bash
# scripts/aliases.sh — Quality-of-life aliases for the Oracle Cloud server
#
# Install:  echo 'source ~/jobs-tracker-bot/scripts/aliases.sh' >> ~/.bashrc
# Reload:   source ~/.bashrc
#
# All commands start with "j" for "job tracker"

BOT_DIR="${BOT_DIR:-$HOME/jobs-tracker-bot}"

# ─── Navigation ──────────────────────────────────────────────
alias jcd="cd $BOT_DIR"

# ─── Docker Compose (run from anywhere) ─────────────────────
alias jup="cd $BOT_DIR && sudo docker compose up -d && cd -"
alias jdown="cd $BOT_DIR && sudo docker compose down && cd -"
alias jrestart="cd $BOT_DIR && sudo docker compose restart && cd -"
alias jrebuild="cd $BOT_DIR && sudo docker compose up -d --build && cd -"

# ─── Logs ────────────────────────────────────────────────────
alias jlogs="cd $BOT_DIR && sudo docker compose logs --tail=50 -f"
alias jlogs100="cd $BOT_DIR && sudo docker compose logs --tail=100"
alias jlog="less $BOT_DIR/logs/job_bot.log"

# ─── Status & Health ────────────────────────────────────────
alias jstatus="cd $BOT_DIR && sudo docker compose ps && cd -"
alias jhealth="curl -sf http://localhost:8080/health | python3 -m json.tool 2>/dev/null || echo '❌ Health check failed'"

# ─── Database ────────────────────────────────────────────────
alias jbackup="cd $BOT_DIR && bash scripts/backup.sh && cd -"
alias jdb="sqlite3 $BOT_DIR/data/jobs.db"
alias jcount="sqlite3 $BOT_DIR/data/jobs.db 'SELECT COUNT(*) || \" total jobs\" FROM jobs;'"
alias jtoday="sqlite3 $BOT_DIR/data/jobs.db \"SELECT COUNT(*) || ' jobs today' FROM jobs WHERE date(created_at) = date('now');\""
alias jsources="sqlite3 $BOT_DIR/data/jobs.db \"SELECT source, COUNT(*) as cnt FROM jobs GROUP BY source ORDER BY cnt DESC;\""

# ─── Update & Deploy ────────────────────────────────────────
alias jupdate="cd $BOT_DIR && bash scripts/update.sh"

# ─── Docker Inspection ──────────────────────────────────────
alias jshell="cd $BOT_DIR && sudo docker compose exec job-bot /bin/bash"
alias jsize="sudo docker images | head -1; sudo docker images | grep job"
alias jmem="sudo docker stats --no-stream"

# ─── Quick Scan (dry run — fetches sources, prints to stdout) ─
jscan() {
    cd "$BOT_DIR"
    sudo docker compose exec job-bot python -c "
import asyncio, json
from main import scan_all_sources
async def run():
    jobs = await scan_all_sources()
    print(json.dumps({'total': len(jobs), 'by_source': {}}))
asyncio.run(run())
" 2>/dev/null || echo "⚠ Container not running. Use 'jup' first."
    cd - > /dev/null
}

# ─── Help ────────────────────────────────────────────────────
jalias() {
    echo "╔══════════════════════════════════════════════════╗"
    echo "║          Job Tracker Bot — Aliases               ║"
    echo "╠══════════════════════════════════════════════════╣"
    echo "║  jcd        cd into bot directory                ║"
    echo "║  jup        start containers                     ║"
    echo "║  jdown      stop containers                      ║"
    echo "║  jrestart   restart containers                   ║"
    echo "║  jrebuild   rebuild & restart                    ║"
    echo "║  jlogs      tail logs (live)                     ║"
    echo "║  jlogs100   last 100 log lines                   ║"
    echo "║  jlog       open log file in less                ║"
    echo "║  jstatus    docker compose ps                    ║"
    echo "║  jhealth    curl health endpoint                 ║"
    echo "║  jbackup    backup database                      ║"
    echo "║  jdb        open sqlite3 shell                   ║"
    echo "║  jcount     total job count                      ║"
    echo "║  jtoday     jobs scraped today                   ║"
    echo "║  jsources   job count by source                  ║"
    echo "║  jupdate    pull + rebuild + health check        ║"
    echo "║  jshell     exec into container                  ║"
    echo "║  jsize      docker image size                    ║"
    echo "║  jmem       docker memory usage                  ║"
    echo "║  jscan      dry-run source scan                  ║"
    echo "║  jalias     show this help                       ║"
    echo "╚══════════════════════════════════════════════════╝"
}

echo "🤖 Job Tracker aliases loaded. Type 'jalias' for help."
