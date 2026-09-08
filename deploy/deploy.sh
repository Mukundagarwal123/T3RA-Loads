#!/bin/bash
set -e

# Manual fallback. The normal path is GitHub Actions on push to main
# (.github/workflows/deploy.yml); use this when you need to deploy from the box
# itself, e.g. Actions cannot reach the host.
echo "=== T3RA Loads - Production Deploy ==="
echo "Branch: $(git rev-parse --abbrev-ref HEAD)"
echo "Pulling latest from main..."
git pull origin main

echo "Installing dependencies..."
.venv/bin/pip install -e . -q

# Before the restart, never after: the new code writes columns the old schema
# may not have, and set -e aborts here rather than restarting into failure.
echo "Applying migrations..."
.venv/bin/python scripts/migrate.py

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
