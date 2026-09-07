import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _req(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


DB_HOST = _req("DB_HOST")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = _req("DB_NAME")
DB_USER = _req("DB_USER")
DB_PASSWORD = _req("DB_PASSWORD")
TABLE_NAME = os.getenv("TABLE_NAME", "route_complete_shipments")
CARRIER_TABLE_NAME = os.getenv("CARRIER_TABLE_NAME", "carriers")

TURVO_BASE_URL = _req("TURVO_BASE_URL")
TURVO_CLIENT_ID = _req("TURVO_CLIENT_ID")
TURVO_CLIENT_SECRET = _req("TURVO_CLIENT_SECRET")
TURVO_API_KEY = _req("TURVO_API_KEY")
TURVO_USERNAME = _req("TURVO_USERNAME")
TURVO_PASSWORD = _req("TURVO_PASSWORD")

# Turvo has no refresh-token flow for us: every token acquisition is a full
# login event on their side. Cache the token on disk so all processes (webhook,
# worker, ad-hoc scripts) and restarts share one login instead of each making
# their own.
TOKEN_CACHE_PATH = os.getenv(
    "TOKEN_CACHE_PATH", str(Path(__file__).parent / ".turvo_token.json"),
)

# After Turvo rejects our credentials, refuse to attempt another login for this
# long. Without it, the queue's retry ladder replays a bad password until the
# account locks out.
AUTH_COOLDOWN_SECONDS = int(os.getenv("AUTH_COOLDOWN_SECONDS", "900"))

WEBHOOK_SHARED_TOKEN = _req("WEBHOOK_SHARED_TOKEN")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhooks/turvo")

# Only these statuses trigger processing (case-insensitive).
TRIGGER_STATUSES = {"route complete", "completed"}

# Monitoring (optional - only needed by healthcheck.py)
TEAMS_WEBHOOK_URL = os.getenv("TEAMS_WEBHOOK_URL", "")
