#!/bin/bash
set -e

echo "=== T3RA Loads - Production Deploy ==="
echo "Branch: $(git rev-parse --abbrev-ref HEAD)"
echo "Pulling latest from main..."
git pull origin main

echo "Installing dependencies..."
.venv/bin/pip install -r requirements.txt -q

echo "Restarting services..."
sudo systemctl restart t3ra-webhook t3ra-worker

sleep 2
echo ""
echo "--- t3ra-webhook ---"
sudo systemctl status t3ra-webhook --no-pager -l | head -8
echo ""
echo "--- t3ra-worker ---"
sudo systemctl status t3ra-worker --no-pager -l | head -8
echo ""
echo "=== Deploy done ==="
