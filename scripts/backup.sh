#!/bin/bash
set -e
BOT_DIR="${BOT_DIR:-$HOME/jobs-tracker-bot}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/job-bot-backups}"
mkdir -p "$BACKUP_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
cd "$BOT_DIR"

if [ ! -f data/jobs.db ]; then
  echo "No database found at $BOT_DIR/data/jobs.db"
  exit 0
fi

sudo docker compose exec -T job-bot python -c \
  "import sqlite3; src=sqlite3.connect('/app/data/jobs.db'); dst=sqlite3.connect('/app/data/backup_$TIMESTAMP.db'); src.backup(dst); dst.close(); src.close()"
cp "$BOT_DIR/data/backup_$TIMESTAMP.db" "$BACKUP_DIR/"
rm "$BOT_DIR/data/backup_$TIMESTAMP.db"
echo "Backup saved to $BACKUP_DIR/backup_$TIMESTAMP.db"
# Keep only last 7 backups
ls -t "$BACKUP_DIR"/*.db | tail -n +8 | xargs -r rm
