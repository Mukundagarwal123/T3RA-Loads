"""Configuration loading and its safety rails."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from t3ra_unified.config import Settings, get_settings


def test_settings_load_from_the_environment():
    settings = get_settings()
    assert settings.db_name == "covered_loads_test"
    assert settings.turvo_base_url == "https://turvo.test"
    assert settings.shipments_table == "shipments_test"


def test_secrets_are_not_exposed_by_repr():
    settings = get_settings()
    assert "test_password" not in repr(settings)
    assert settings.db_password.get_secret_value() == "test_password"


def test_the_startup_summary_carries_no_secrets():
    serialized = str(get_settings().safe_summary())
    for secret in ("test_password", "test-api-key", "test-gs-secret", "test-bearer-token-0123456789abcdef"):
        assert secret not in serialized


def test_dat_urls_follow_the_selected_environment():
    settings = get_settings()
    assert "staging.dat.com" in settings.dat_org_token_url
    assert "staging.dat.com" in settings.dat_rate_lookup_url


@pytest.mark.parametrize("table_name", ["shipments; DROP TABLE users", "ship ments", "ship-ments", "1_table", ""])
def test_a_table_name_that_is_not_an_identifier_is_rejected(table_name, monkeypatch):
    monkeypatch.setenv("SHIPMENTS_TABLE", table_name)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("table_name", ["shipments", "shipments_staging", "public.shipments"])
def test_valid_table_names_are_accepted(table_name, monkeypatch):
    monkeypatch.setenv("SHIPMENTS_TABLE", table_name)
    assert Settings().shipments_table == table_name


def test_a_missing_required_setting_fails_loudly(monkeypatch):
    monkeypatch.delenv("TURVO_WEBHOOK_SHARED_TOKEN", raising=False)
    monkeypatch.delenv("WEBHOOK_SHARED_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_legacy_t3ra_constants_resolve_from_settings():
    from t3ra_unified import config

    assert config.TABLE_NAME == "shipments_test"
    assert config.DB_HOST == "localhost"
    assert config.TRIGGER_STATUSES == {"route complete", "completed"}
    assert config.WEBHOOK_PATH == "/webhooks/turvo"
    with pytest.raises(AttributeError):
        config.NOT_A_SETTING  # noqa: B018


def test_the_covered_view_name_is_validated_as_an_identifier(monkeypatch):
    monkeypatch.setenv("COVERED_VIEW", "covered_loads.covered_loads; DROP")
    with pytest.raises(ValidationError):
        Settings()


def test_the_old_covered_prefixed_names_still_work(monkeypatch):
    """An existing Covered .env (COVERED_GS_*, COVERED_DAT_*, ...) must load unchanged."""
    for name in ("GS_CLIENT_ID", "GS_CLIENT_SECRET", "DAT_USER_USERNAME", "DAT_USER_PASSWORD", "REFRESH_INTERVAL_MINUTES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COVERED_GS_CLIENT_ID", "old-gs-id")
    monkeypatch.setenv("COVERED_GS_CLIENT_SECRET", "old-gs-secret")
    monkeypatch.setenv("COVERED_DAT_USER_USERNAME", "old-dat-user")
    monkeypatch.setenv("COVERED_DAT_USER_PASSWORD", "old-dat-pass")
    monkeypatch.setenv("COVERED_REFRESH_INTERVAL_MINUTES", "45")
    s = Settings()
    assert s.gs_client_id == "old-gs-id"
    assert s.gs_client_secret.get_secret_value() == "old-gs-secret"
    assert s.dat_user_username == "old-dat-user"
    assert s.dat_user_password.get_secret_value() == "old-dat-pass"
    assert s.refresh_interval_minutes == 45


def test_the_plain_name_wins_when_both_are_set(monkeypatch):
    monkeypatch.setenv("GS_CLIENT_ID", "plain")
    monkeypatch.setenv("COVERED_GS_CLIENT_ID", "legacy")
    assert Settings().gs_client_id == "plain"


def test_the_old_webhook_and_turvo_names_are_accepted_too(monkeypatch):
    monkeypatch.delenv("TURVO_WEBHOOK_SHARED_TOKEN", raising=False)
    monkeypatch.setenv("COVERED_WEBHOOK_BEARER_TOKEN", "legacy-token")
    assert Settings().webhook_shared_token.get_secret_value() == "legacy-token"
