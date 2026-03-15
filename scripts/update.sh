#!/bin/bash
set -e
echo "Pulling latest code..."
cd ~/jobs-tracker-bot
git pull origin main
echo "Rebuilding Docker image..."
sudo docker compose up -d --build
echo "Waiting for health check..."
sleep 15
sudo docker compose ps
echo ""
echo "Last 20 log lines:"
sudo docker compose logs --tail=20
echo ""
echo "Update complete!"
