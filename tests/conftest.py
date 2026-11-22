"""Shared test setup.

Settings are populated from a fixed fake environment before any application
module is imported, so tests never read a developer's real ``.env`` and never
reach a real database or API.
"""

from __future__ import annotations

import json
import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

FAKE_ENV = {
    "ENVIRONMENT": "local",
    "LOGFIRE_IGNORE_NO_CONFIG": "1",
    "LOG_LEVEL": "WARNING",
    "DB_HOST": "localhost",
    "DB_PORT": "5432",
    "DB_NAME": "covered_loads_test",
    "DB_USER": "test_user",
    "DB_PASSWORD": "test_password",
    "DB_SSLMODE": "disable",
    "SHIPMENTS_TABLE": "shipments_test",
    "TURVO_BASE_URL": "https://turvo.test",
    "TURVO_API_KEY": "test-api-key",
    "TURVO_CLIENT_ID": "publicapi",
    "TURVO_CLIENT_SECRET": "test-client-secret",
    "TURVO_USERNAME": "test@example.com",
    "TURVO_PASSWORD": "test-password",
    "TURVO_WEBHOOK_SHARED_TOKEN": "test-bearer-token-0123456789abcdef",
    "GS_CLIENT_ID": "test-gs-client",
    "GS_CLIENT_SECRET": "test-gs-secret",
    "GS_AUTH_URL": "https://gs.test/auth",
    "GS_PREDICTION_URL": "https://gs.test/rates",
    "DAT_ENV": "staging",
    "DAT_ORG_USERNAME": "org@example.com",
    "DAT_ORG_PASSWORD": "org-password",
    "DAT_USER_USERNAME": "user@example.com",
    "DAT_USER_PASSWORD": "user-password",
}


@pytest.fixture(autouse=True, scope="session")
def _fake_environment() -> None:
    """Install the fake settings for the whole session."""
    import os

    for key, value in FAKE_ENV.items():
        os.environ.setdefault(key, value)

    # Ignore any .env the developer has locally.
    from t3ra_unified import config

    config.Settings.model_config["env_file"] = None
    config.get_settings.cache_clear()


@pytest.fixture
def shipment_payload() -> dict:
    """A realistic two-stop covered shipment."""
    return json.loads((FIXTURES / "shipment_two_stop.json").read_text(encoding="utf-8"))
