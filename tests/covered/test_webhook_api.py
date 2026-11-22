"""The unified webhook: auth and enqueue behaviour (queue stubbed, no database).

The receiver keeps the T3RA way of presenting the shared token (query string,
``Authorization: Bearer`` or a ``token`` / ``x-turvo-token`` header) and queues
every shipment event for the worker to classify.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from t3ra_unified.config import get_settings

TOKEN = "test-bearer-token-0123456789abcdef"
WEBHOOK_PATH = "/webhooks/turvo"
PAYLOAD = {"eventName": "SHIPMENT_UPDATE", "eventPayload": {"id": 115792084, "status": {"code": {"value": "Covered"}}}}


@pytest.fixture
def client(monkeypatch):
    get_settings.cache_clear()
    from t3ra_unified.api import app as api_module
    from t3ra_unified.intake import webhook as webhook_module

    enqueued: list[dict] = []

    def fake_enqueue(raw_body, payload, shipment_id, status_value):
        key = hashlib.sha256(raw_body).hexdigest()
        duplicate = any(e["event_key"] == key for e in enqueued)
        record = {"event_key": key, "inserted": not duplicate, "event_id": len(enqueued) + 1,
                  "shipment_id": shipment_id, "status_value": status_value}
        enqueued.append(record)
        return record

    monkeypatch.setattr(webhook_module, "enqueue_webhook_event", fake_enqueue)
    c = TestClient(api_module.create_app())
    c.enqueued = enqueued  # type: ignore[attr-defined]
    yield c
    get_settings.cache_clear()


def _post(client, *, headers=None, params=None, body=None):
    raw = json.dumps(body if body is not None else PAYLOAD).encode()
    return client.post(WEBHOOK_PATH, content=raw, headers={"Content-Type": "application/json", **(headers or {})},
                       params=params)


def test_health_reports_ok(client):
    assert client.get("/health").json()["ok"] is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"headers": {"Authorization": f"Bearer {TOKEN}"}},
        {"headers": {"Authorization": f"bearer {TOKEN}"}},
        {"headers": {"Authorization": TOKEN}},
        {"headers": {"token": TOKEN}},
        {"headers": {"x-turvo-token": TOKEN}},
        {"params": {"token": TOKEN}},
    ],
)
def test_every_accepted_way_of_presenting_the_token(client, kwargs):
    response = _post(client, **kwargs)
    assert response.status_code == 200
    assert response.json()["queued"] is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"headers": {"Authorization": "Bearer wrong-token"}},
        {"headers": {"Authorization": "Bearer "}},
        {"headers": {"Authorization": f"Bearer {TOKEN[:-1]}"}},
        {"params": {"token": "nope"}},
    ],
)
def test_a_missing_or_wrong_token_is_rejected(client, kwargs):
    assert _post(client, **kwargs).status_code == 401
    assert client.enqueued == []


def test_the_same_body_twice_is_reported_as_a_duplicate(client):
    _post(client, headers={"Authorization": f"Bearer {TOKEN}"})
    body = _post(client, headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert body["queued"] is False and body["duplicate"] is True


def test_every_shipment_event_is_queued_whatever_its_status(client):
    """Unlike the old T3RA receiver, filtering is the worker's job, not the door's."""
    for status in ("Tendered", "Route Complete", "Cancelled"):
        payload = {"eventName": "SHIPMENT_UPDATE", "eventPayload": {"id": 1, "status": {"code": {"value": status}}}}
        assert _post(client, headers={"Authorization": f"Bearer {TOKEN}"}, body=payload).json()["queued"] is True
    assert len(client.enqueued) == 3
