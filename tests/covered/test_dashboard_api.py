"""Tests for the read-only dashboard endpoints the portal reads.

The repository layer is stubbed out, so these run with no database. What is
under test is the auth surface, the date-window handling, and the shape of the
payload the portal's Coverage Metrics tab depends on.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from t3ra_unified.config import get_settings

DASHBOARD_TOKEN = "test-dashboard-token-0123456789abcdef"

PRICED_ROW = {
    "shipment_num": "S-1001",
    "lane": "Los Angeles, CA -> Dallas, TX",
    "covered_date": datetime(2026, 9, 1, 17, 30),
    "pickup_date": date(2026, 9, 3),
    "customer": "Acme",
    "carrier": "Blue Line",
    "carrier_am": "Turvo Bot",
    "customer_freight_cost": Decimal("2400.00"),
    "carrier_freight_cost": Decimal("2000.00"),
    "gs_rate": Decimal("1900.00"),
    "gs_confidence_score": Decimal("0.82"),
    "gs_diff": Decimal("-0.0526"),
    "dat_rate": Decimal("2100.00"),
    "dat_diff": Decimal("0.0476"),
    "avg_network_rate": Decimal("2000.00"),
    "avg_network_diff": Decimal("-0.0025"),
    "lh_margin": Decimal("0.1667"),
}

UNPRICED_ROW = {
    "shipment_num": "S-1002",
    "lane": "Reno, NV -> Boise, ID",
    "covered_date": datetime(2026, 9, 2, 9, 0),
    "pickup_date": date(2026, 9, 4),
    "customer": "Globex",
    "carrier": "Red Line",
    "carrier_am": "Dana Reyes",
    "customer_freight_cost": Decimal("1500.00"),
    "carrier_freight_cost": Decimal("1300.00"),
    "gs_rate": None,
    "gs_confidence_score": None,
}


@pytest.fixture
def client(monkeypatch):
    """An app whose dashboard reads return fixed rows."""
    monkeypatch.setenv("DASHBOARD_API_TOKEN", DASHBOARD_TOKEN)
    monkeypatch.setenv("DASHBOARD_CORS_ORIGINS", "http://localhost:5173")
    get_settings.cache_clear()

    from t3ra_unified.api import app as api_module
    from t3ra_unified.api import dashboard as routes_module

    monkeypatch.setattr(
        routes_module.dashboard_repo, "fetch_priced_loads", lambda start, end: [dict(PRICED_ROW)]
    )
    monkeypatch.setattr(
        routes_module.dashboard_repo, "fetch_unpriced_loads", lambda start, end: [dict(UNPRICED_ROW)]
    )
    monkeypatch.setattr(
        routes_module.job_status,
        "fetch_job_status",
        lambda job_name: {
            "status": "completed",
            "started_at": datetime(2026, 9, 8, 10, 0),
            "finished_at": datetime(2026, 9, 8, 10, 2),
            "last_success_at": datetime(2026, 9, 8, 10, 2),
            "next_run_at": datetime(2026, 9, 8, 10, 32),
            "message": None,
            "updated_rows": 41,
        },
    )

    monkeypatch.setattr(
        routes_module.dashboard_repo,
        "fetch_data_version",
        lambda job_name: {
            "last_webhook_at": datetime(2026, 9, 8, 10, 5, 12),
            "last_refresh_at": datetime(2026, 9, 8, 10, 2),
        },
    )

    yield TestClient(api_module.create_app())

    get_settings.cache_clear()


def _get(client, path, *, authorization=f"Bearer {DASHBOARD_TOKEN}"):
    headers = {"Authorization": authorization} if authorization is not None else {}
    return client.get(path, headers=headers)


LOADS_PATH = "/api/dashboard/loads?start=2026-09-01&end=2026-09-08"


# ----------------------------------------------------------------------
# Authentication
# ----------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/api/dashboard/job-status", "/api/dashboard/changes", LOADS_PATH])
def test_missing_authorization_is_rejected(client, path):
    assert _get(client, path, authorization=None).status_code == 401


@pytest.mark.parametrize("path", ["/api/dashboard/job-status", "/api/dashboard/changes", LOADS_PATH])
def test_wrong_token_is_rejected(client, path):
    assert _get(client, path, authorization="Bearer nope").status_code == 401


def test_non_bearer_scheme_is_rejected(client):
    response = _get(client, LOADS_PATH, authorization=f"Basic {DASHBOARD_TOKEN}")
    assert response.status_code == 401


def test_unconfigured_token_closes_the_endpoint(monkeypatch):
    """No COVERED_DASHBOARD_API_TOKEN must mean no data, not open data."""
    monkeypatch.delenv("DASHBOARD_API_TOKEN", raising=False)
    get_settings.cache_clear()

    from t3ra_unified.api import app as api_module

    unconfigured = TestClient(api_module.create_app())
    response = unconfigured.get(LOADS_PATH, headers={"Authorization": "Bearer anything"})

    get_settings.cache_clear()
    assert response.status_code == 503


# ----------------------------------------------------------------------
# Payload
# ----------------------------------------------------------------------
def test_loads_returns_both_buckets(client):
    body = _get(client, LOADS_PATH).json()

    assert [row["shipment_num"] for row in body["priced"]] == ["S-1001"]
    assert [row["shipment_num"] for row in body["unpriced"]] == ["S-1002"]
    assert body["start"] == "2026-09-01"
    assert body["end"] == "2026-09-08"
    assert body["truncated"] is False


def test_loads_serializes_decimals_and_dates(client):
    row = _get(client, LOADS_PATH).json()["priced"][0]

    assert row["carrier_freight_cost"] == 2000.0
    assert row["gs_diff"] == pytest.approx(-0.0526)
    assert row["covered_date"] == "2026-09-01T17:30:00"
    assert row["pickup_date"] == "2026-09-03"


def test_unpriced_rows_carry_no_gs_diff(client):
    """Without a rate there is nothing to compare against."""
    row = _get(client, LOADS_PATH).json()["unpriced"][0]

    assert "gs_diff" not in row
    assert row["gs_rate"] is None


def test_ratios_stay_fractions(client):
    """Scaling to percent is the portal's job, not the API's."""
    row = _get(client, LOADS_PATH).json()["priced"][0]

    assert abs(row["lh_margin"]) < 1
    assert abs(row["gs_diff"]) < 1
    assert abs(row["avg_network_diff"]) < 1


def test_the_blended_columns_reach_the_portal(client):
    """The blend is computed in SQL; the API must pass both columns through."""
    row = _get(client, LOADS_PATH).json()["priced"][0]

    assert row["avg_network_rate"] == 2000.0
    assert row["avg_network_diff"] == -0.0025


def test_unpriced_rows_carry_no_blend(client):
    """The unpriced query never selects the blend, so the columns stay absent."""
    row = _get(client, LOADS_PATH).json()["unpriced"][0]

    assert "avg_network_rate" not in row
    assert "avg_network_diff" not in row


# ----------------------------------------------------------------------
# Window validation
# ----------------------------------------------------------------------
def test_end_before_start_is_rejected(client):
    response = _get(client, "/api/dashboard/loads?start=2026-09-08&end=2026-09-01")
    assert response.status_code == 422


def test_window_wider_than_a_year_is_rejected(client):
    response = _get(client, "/api/dashboard/loads?start=2020-01-01&end=2026-09-08")
    assert response.status_code == 422


def test_missing_dates_are_rejected(client):
    assert _get(client, "/api/dashboard/loads").status_code == 422


def test_single_day_window_is_allowed(client):
    response = _get(client, "/api/dashboard/loads?start=2026-09-08&end=2026-09-08")
    assert response.status_code == 200


# ----------------------------------------------------------------------
# Job status
# ----------------------------------------------------------------------
def test_job_status_shape(client):
    body = _get(client, "/api/dashboard/job-status").json()

    assert body["status"] == "completed"
    assert body["last_success_at"] == "2026-09-08T10:02:00"
    assert body["next_run_at"] == "2026-09-08T10:32:00"
    assert body["timezone"] == get_settings().dashboard_timezone


def test_job_status_survives_a_lookup_failure(client, monkeypatch):
    """A dead heartbeat row must not take the whole tab down."""
    from t3ra_unified.api import dashboard as routes_module

    def boom(job_name):
        raise RuntimeError("no such table")

    monkeypatch.setattr(routes_module.job_status, "fetch_job_status", boom)

    body = _get(client, "/api/dashboard/job-status").json()
    assert body["status"] is None


# ----------------------------------------------------------------------
# Change feed
# ----------------------------------------------------------------------
def test_changes_shape(client):
    body = _get(client, "/api/dashboard/changes").json()

    assert body["last_webhook_at"] == "2026-09-08T10:05:12"
    assert body["last_refresh_at"] == "2026-09-08T10:02:00"
    assert body["version"] == "2026-09-08T10:05:12|2026-09-08T10:02:00"


def test_changes_version_moves_when_a_webhook_lands(client, monkeypatch):
    """A newly applied Turvo update must change the stamp the portal compares."""
    from t3ra_unified.api import dashboard as routes_module

    before = _get(client, "/api/dashboard/changes").json()["version"]
    monkeypatch.setattr(
        routes_module.dashboard_repo,
        "fetch_data_version",
        lambda job_name: {
            "last_webhook_at": datetime(2026, 9, 8, 10, 6, 0),
            "last_refresh_at": datetime(2026, 9, 8, 10, 2),
        },
    )
    after = _get(client, "/api/dashboard/changes").json()["version"]

    assert before != after


def test_changes_before_any_activity(client, monkeypatch):
    from t3ra_unified.api import dashboard as routes_module

    monkeypatch.setattr(
        routes_module.dashboard_repo,
        "fetch_data_version",
        lambda job_name: {"last_webhook_at": None, "last_refresh_at": None},
    )

    body = _get(client, "/api/dashboard/changes").json()
    assert body["version"] == "-|-"


def test_changes_lookup_failure_is_503(client, monkeypatch):
    """A failure must not look like "nothing changed"."""
    from t3ra_unified.api import dashboard as routes_module

    def boom(job_name):
        raise RuntimeError("db down")

    monkeypatch.setattr(routes_module.dashboard_repo, "fetch_data_version", boom)

    assert _get(client, "/api/dashboard/changes").status_code == 503


# ----------------------------------------------------------------------
# Read-only guarantee
# ----------------------------------------------------------------------
@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_dashboard_routes_are_get_only(client, method):
    response = getattr(client, method)(
        "/api/dashboard/loads", headers={"Authorization": f"Bearer {DASHBOARD_TOKEN}"}
    )
    assert response.status_code == 405


# ----------------------------------------------------------------------
# Notes (saved to this database only)
# ----------------------------------------------------------------------
NOTES_PATH = "/api/dashboard/loads/S-1001/notes"


@pytest.fixture
def saved_notes(monkeypatch):
    from t3ra_unified.api import dashboard as routes_module

    calls: list[tuple[str, str]] = []

    def fake_update(shipment_num, notes):
        calls.append((shipment_num, notes))
        return notes if shipment_num == "S-1001" else None

    monkeypatch.setattr(routes_module.covered_loads_repo, "update_notes", fake_update)
    return calls


def test_notes_are_saved_and_trimmed(client, saved_notes):
    response = client.put(
        NOTES_PATH, json={"notes": "  call dispatch  "}, headers={"Authorization": f"Bearer {DASHBOARD_TOKEN}"}
    )
    assert response.status_code == 200
    assert response.json() == {"shipment_num": "S-1001", "notes": "call dispatch"}
    assert saved_notes == [("S-1001", "call dispatch")]


def test_notes_for_an_unknown_shipment_are_404(client, saved_notes):
    response = client.put(
        "/api/dashboard/loads/NOPE/notes", json={"notes": "x"}, headers={"Authorization": f"Bearer {DASHBOARD_TOKEN}"}
    )
    assert response.status_code == 404


def test_notes_require_the_token(client, saved_notes):
    assert client.put(NOTES_PATH, json={"notes": "x"}).status_code == 401
    assert saved_notes == []


def test_overlong_notes_are_rejected(client, saved_notes):
    response = client.put(
        NOTES_PATH, json={"notes": "x" * 2001}, headers={"Authorization": f"Bearer {DASHBOARD_TOKEN}"}
    )
    assert response.status_code == 422
    assert saved_notes == []
