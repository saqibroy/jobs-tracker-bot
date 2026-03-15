#!/bin/bash
set -e
BACKUP_DIR=~/job-bot-backups
mkdir -p $BACKUP_DIR
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
sudo docker compose -f ~/jobs-tracker-bot/docker-compose.yml \
  exec -T job-bot sqlite3 data/jobs.db ".backup /app/data/backup_$TIMESTAMP.db"
cp ~/jobs-tracker-bot/data/backup_$TIMESTAMP.db $BACKUP_DIR/
echo "Backup saved to $BACKUP_DIR/backup_$TIMESTAMP.db"
# Keep only last 7 backups
ls -t $BACKUP_DIR/*.db | tail -n +8 | xargs -r rm
