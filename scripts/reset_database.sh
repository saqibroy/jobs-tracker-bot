#!/bin/bash
# One-time clean cutover for the eligibility/CV-fit rebuild.
set -e

if [ "${1:-}" != "--confirm" ]; then
  echo "Usage: bash scripts/reset_database.sh --confirm"
  echo "Archives the current database and starts a clean one."
  exit 2
fi

BOT_DIR="${BOT_DIR:-$HOME/jobs-tracker-bot}"
ARCHIVE_DIR="${BACKUP_DIR:-$HOME/job-bot-backups}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
cd "$BOT_DIR"
mkdir -p "$ARCHIVE_DIR"

bash scripts/backup.sh
sudo docker compose stop job-bot
if [ -f data/jobs.db ]; then
  mv data/jobs.db "$ARCHIVE_DIR/pre_rebuild_$TIMESTAMP.db"
fi
sudo docker compose up -d job-bot
echo "Clean database started; archive: $ARCHIVE_DIR/pre_rebuild_$TIMESTAMP.db"
