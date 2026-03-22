#!/bin/bash
# scripts/update.sh — Production update with backup + health check
#
# Run on the Oracle Cloud server:
#   cd ~/jobs-tracker-bot && bash scripts/update.sh
set -e

BOT_DIR="${BOT_DIR:-$HOME/jobs-tracker-bot}"
cd "$BOT_DIR"

echo "═══════════════════════════════════════════"
echo "  Job Tracker Bot — Production Update"
echo "═══════════════════════════════════════════"
echo ""

# 1. Backup database before touching anything
echo "📦 Backing up database..."
bash scripts/backup.sh 2>/dev/null || echo "⚠ Backup skipped (no DB yet?)"
echo ""

# 2. Record current commit for rollback
OLD_COMMIT=$(git rev-parse --short HEAD)
echo "📌 Current commit: $OLD_COMMIT"

# 3. Pull latest code
echo "⬇️  Pulling latest code..."
git pull origin main
NEW_COMMIT=$(git rev-parse --short HEAD)
echo "📌 New commit: $NEW_COMMIT"
echo ""

if [ "$OLD_COMMIT" = "$NEW_COMMIT" ]; then
    echo "ℹ️  Already up to date. Rebuilding anyway..."
fi

# 4. Rebuild and restart
echo "🔨 Rebuilding Docker image..."
sudo docker compose up -d --build
echo ""

# 5. Wait for health check
echo "⏳ Waiting for health check..."
HEALTHY=false
for i in $(seq 1 12); do
    if curl -sf http://localhost:8080/health > /dev/null 2>&1; then
        HEALTHY=true
        break
    fi
    echo "  attempt $i/12..."
    sleep 5
done
echo ""

if $HEALTHY; then
    echo "✅ Health check passed"
else
    echo "❌ Health check FAILED after 60s"
    echo ""
    echo "Last 30 log lines:"
    sudo docker compose logs --tail=30
    echo ""
    echo "To rollback: git checkout $OLD_COMMIT && sudo docker compose up -d --build"
    exit 1
fi

# 6. Show status
echo ""
sudo docker compose ps
echo ""
echo "📋 Last 10 log lines:"
sudo docker compose logs --tail=10
echo ""
echo "✅ Update complete: $OLD_COMMIT → $NEW_COMMIT"
