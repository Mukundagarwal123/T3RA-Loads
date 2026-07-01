import json
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2

import config

STATE_FILE = Path(__file__).parent / ".watchdog_state.json"
SERVICES = ["t3ra-webhook", "t3ra-worker"]
HEALTH_URL = "http://localhost:8000/health"
QUEUE_BACKLOG_MINUTES = 15
REALERT_MINUTES = 30


def check_services() -> list:
    failed = []
    for svc in SERVICES:
        result = subprocess.run(
            ["systemctl", "is-active", svc], capture_output=True, text=True,
        )
        if result.stdout.strip() != "active":
            failed.append(svc)
    return failed


def check_health_endpoint() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def check_queue_backlog():
    """Returns count of stuck events, or None if the DB couldn't be reached."""
    try:
        conn = psycopg2.connect(
            host=config.DB_HOST, port=config.DB_PORT, database=config.DB_NAME,
            user=config.DB_USER, password=config.DB_PASSWORD, connect_timeout=5,
        )
        cur = conn.cursor()
        cur.execute(
            """
            SELECT count(*) FROM public.webhook_queue
            WHERE state IN ('pending', 'retry')
              AND received_at < NOW() - INTERVAL '%s minutes';
            """ % QUEUE_BACKLOG_MINUTES
        )
        count = cur.fetchone()[0]
        conn.close()
        return count
    except Exception:
        return None


def send_teams_alert(text: str) -> None:
    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {"type": "TextBlock", "text": text, "wrap": True, "weight": "Bolder", "size": "Medium"},
        ],
    }
    data = json.dumps(card).encode("utf-8")
    req = urllib.request.Request(
        config.TEAMS_WEBHOOK_URL, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    urllib.request.urlopen(req, timeout=10)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"status": "ok", "last_alert": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state))


def main() -> None:
    problems = []

    failed_services = check_services()
    if failed_services:
        problems.append(f"Service(s) down: {', '.join(failed_services)}")

    if not check_health_endpoint():
        problems.append("Webhook /health endpoint not responding")

    backlog = check_queue_backlog()
    if backlog is None:
        problems.append("Could not query webhook_queue (DB unreachable?)")
    elif backlog > 0:
        problems.append(f"{backlog} webhook event(s) stuck pending/retry for over {QUEUE_BACKLOG_MINUTES} min")

    state = load_state()
    now = datetime.now(timezone.utc)

    if problems:
        message = "\U0001F534 T3RA Loads ALERT (13.63.89.90):\n" + "\n".join(f"- {p}" for p in problems)

        should_alert = state["status"] == "ok"
        if not should_alert:
            last_alert = state.get("last_alert")
            should_alert = not last_alert or (
                now - datetime.fromisoformat(last_alert)
            ) > timedelta(minutes=REALERT_MINUTES)

        if should_alert:
            send_teams_alert(message)
            state["last_alert"] = now.isoformat()
        state["status"] = "down"
    else:
        if state["status"] == "down":
            send_teams_alert("✅ T3RA Loads RECOVERED (13.63.89.90): all checks passing again.")
        state["status"] = "ok"
        state["last_alert"] = None

    save_state(state)


if __name__ == "__main__":
    main()
