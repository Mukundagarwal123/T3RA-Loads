#!/bin/bash
# One-time setup on the production host, moving from the old flat layout to the
# packaged one. Run it once, by hand, over SSH. After this, GitHub Actions
# deploys on every push to main and you should not need to touch the box.
#
#   cd /home/ubuntu/app && bash deploy/server-setup.sh
#
# Safe to re-run. It never touches .env.

set -euo pipefail

APP_DIR=/home/ubuntu/app
cd "$APP_DIR"

echo "== 1. Checking .env is present (it is gitignored, so a pull cannot restore it) =="
test -f .env || { echo "FATAL: $APP_DIR/.env is missing. Restore it before continuing."; exit 1; }
cp .env ".env.backup.$(date +%Y%m%d%H%M%S)"
echo "   backed up"

echo "== 2. Stopping anything started by hand =="
# The old worker was launched over SSH and writes worker.log / webhook_server.log
# in this directory. systemd cannot manage a process it did not start, so those
# have to go or you end up with two workers competing for the same queue.
pkill -f "python.*worker.py" 2>/dev/null || true
pkill -f "python.*webhook_server.py" 2>/dev/null || true
sudo systemctl stop t3ra-worker t3ra-webhook 2>/dev/null || true
echo "   stopped"

echo "== 3. Pulling the new layout =="
git fetch origin main
git reset --hard origin/main

echo "== 4. Installing the package =="
# requirements.txt is gone; dependencies now live in pyproject.toml and the
# package is installed editable so 'python -m turvo_db.worker' resolves.
.venv/bin/pip install -e . -q

echo "== 5. Applying migrations =="
.venv/bin/python scripts/migrate.py

echo "== 6. Installing systemd units =="
sudo cp deploy/t3ra-webhook.service deploy/t3ra-worker.service /etc/systemd/system/
sudo cp deploy/t3ra-maintenance.service deploy/t3ra-maintenance.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable t3ra-webhook t3ra-worker
sudo systemctl enable --now t3ra-maintenance.timer

echo "== 7. Allowing the deploy user to restart the services without a password =="
# GitHub Actions runs non-interactively, so a sudo password prompt would hang
# until the job times out.
echo 'ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl restart t3ra-webhook t3ra-worker, /bin/systemctl is-active t3ra-webhook t3ra-worker' \
  | sudo tee /etc/sudoers.d/t3ra-deploy > /dev/null
sudo chmod 440 /etc/sudoers.d/t3ra-deploy
sudo visudo -c -f /etc/sudoers.d/t3ra-deploy

echo "== 8. Starting =="
sudo systemctl restart t3ra-webhook t3ra-worker
sleep 3
systemctl is-active t3ra-webhook t3ra-worker
curl -fsS http://localhost:8000/health && echo

echo "== 9. Stamping markets on anything ingested while the old code was running =="
.venv/bin/python scripts/backfill_kma.py

cat <<'DONE'

Setup complete.

Logs now go to journald, not to files in this directory:
    journalctl -u t3ra-worker -f
    journalctl -u t3ra-webhook -f

The old *.log files are left in place so you can read them; delete them and any
logrotate rule pointing at them once you are happy.
DONE
