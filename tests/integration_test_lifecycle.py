"""End-to-end against the local Postgres (docker-compose, port 5441).

Runs the real repositories and queue; only the Turvo fetch and the GS/DAT rate
engines are stubbed. Verifies that one row follows a shipment from "covered" to
"route complete", and that the two compat views Spot Bid reads show it correctly.

Run:  pytest tests/integration_test_lifecycle.py -o addopts="" -m integration
"""
from __future__ import annotations

import copy
import json
import os
import pathlib

import pytest

pytestmark = pytest.mark.integration

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "shipment_two_stop.json"


@pytest.fixture(scope="module")
def db(request):
    """Point settings at the local compose DB, from .env, and run migrations on a clean schema."""
    from dotenv import dotenv_values

    env = dotenv_values(pathlib.Path(__file__).parents[1] / ".env")
    if not env.get("DB_PORT"):
        pytest.skip("no local DB configured in .env")
    for k in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_SSLMODE"):
        os.environ[k] = env[k]
    os.environ["DB_NAME"] = "t3ra_unified"
    os.environ["SHIPMENTS_TABLE"] = "shipments"
    from t3ra_unified.config import get_settings

    get_settings.cache_clear()
    from t3ra_unified.db import engine

    try:
        assert engine.healthcheck()
    except Exception:
        pytest.skip("local Postgres not reachable")
    with engine.cursor(commit=True) as cur:
        cur.execute("TRUNCATE shipments, webhook_queue, carriers, job_run_status RESTART IDENTITY")
    yield engine
    with engine.cursor(commit=True) as cur:
        cur.execute("TRUNCATE shipments, webhook_queue, carriers, job_run_status RESTART IDENTITY")


@pytest.fixture
def payload():
    """The Covered fixture, plus what a real Turvo payload carries for the completion phase:
    zips on customerOrder[0].route and a carrier id."""
    data = json.loads(FIXTURE.read_text(encoding="utf8"))
    details = data["details"]
    details["customerOrder"][0]["route"] = [
        {"sequence": s["sequence"], "deleted": False, "address": dict(s["address"])} for s in details["globalRoute"]
    ]
    details["carrierOrder"][0]["carrier"]["id"] = 777001
    return data


@pytest.fixture
def stubs(monkeypatch, payload):
    from t3ra_unified import worker as w
    from t3ra_unified.covered.services import shipment_processor as sp

    monkeypatch.setattr(sp, "run_gs", lambda *a, **k: (2000.0, 0.9))
    monkeypatch.setattr(sp, "run_dat", lambda *a, **k: 1900.0)
    monkeypatch.setattr(sp, "fill_missing_zips", lambda stops: stops)
    state = {"payload": payload}
    monkeypatch.setattr(w, "fetch_shipment_details", lambda sid: state["payload"])
    # carrier sync would call Turvo; the carrier id is looked up locally only
    from t3ra_unified.completion import service as svc

    monkeypatch.setattr(svc, "fetch_carrier_details", lambda cid: {"details": {"id": cid, "name": "Carrier X"}})
    return state


def _event(status, name="SHIPMENT_UPDATE", sid=115792084, n=[0]):
    n[0] += 1
    return {"id": n[0], "event_key": f"k{n[0]}", "attempt_count": 1, "shipment_id": sid, "status_value": status,
            "payload_json": {"eventName": name, "eventPayload": {"id": sid, "status": {"code": {"value": status}}}}}


def _enqueue_and_claim(db, event):
    with db.cursor(commit=True) as cur:
        cur.execute("INSERT INTO webhook_queue (id, event_key, shipment_id, status_value, payload_json, state) "
                    "VALUES (%s,%s,%s,%s,%s,'processing')",
                    (event["id"], event["event_key"], event["shipment_id"], event["status_value"],
                     json.dumps(event["payload_json"])))


def _row(db, view="shipments"):
    with db.cursor(dict_rows=True) as cur:
        cur.execute(f"SELECT * FROM {view} ORDER BY 1")
        return [dict(r) for r in cur.fetchall()]


def _run(db, status):
    from t3ra_unified import worker as w

    ev = _event(status)
    _enqueue_and_claim(db, ev)
    w.process_one(ev, max_attempts=3)
    with db.cursor() as cur:
        cur.execute("SELECT state, last_error FROM webhook_queue WHERE id=%s", (ev["id"],))
        return cur.fetchone()


def test_full_lifecycle_one_row_two_views(db, stubs):
    # 1. covered: priced, visible to the Covered view only
    assert _run(db, "Covered")[0] == "done"
    rows = _row(db)
    assert len(rows) == 1
    r = rows[0]
    assert r["is_covered"] and r["gs_rate"] == 2000 and r["dat_rate"] == 1900 and r["covered_date"] is not None
    assert r["finalized_at"] is None and r["origin_kma"] is None
    assert len(_row(db, "covered_loads.covered_loads")) == 1
    assert _row(db, "route_complete_shipments") == []

    # shared columns follow the T3RA rule
    assert str(r["pickup_date"]) == "2025-04-08" and str(r["delivery_date"]) == "2025-04-10"
    assert r["customer_name"] and r["carrier_name"]
    assert float(r["customer_total_cost"]) == 2400.0 and float(r["carrier_total_cost"]) == 1800.0
    assert float(r["carrier_freight_cost"]) == 1600.0 and float(r["customer_freight_cost"]) == 2200.0
    covered_gs_rate, covered_date = r["gs_rate"], r["covered_date"]

    # 2. route complete: completion phase fills T3RA columns on the SAME row
    assert _run(db, "Route Complete")[0] == "done"
    rows = _row(db)
    assert len(rows) == 1, "must stay one row per shipment"
    r = rows[0]
    assert r["finalized_at"] is not None and r["total_stops"] == 3 and r["carrier_id"] is not None, {k: r[k] for k in ("finalized_at", "total_stops", "carrier_id", "kma_mapped_at", "equipment", "origin_zip")}
    assert r["carrier_id_source"] == "turvo" and r["kma_mapped_at"] is not None
    assert r["equipment"] and r["origin_city"] and r["destination_zip"]
    # covered-owned values survive the completion phase untouched
    assert r["gs_rate"] == covered_gs_rate and r["covered_date"] == covered_date and r["is_covered"]
    assert float(r["carrier_freight_cost"]) == 1600.0  # same value from both phases

    rcs = _row(db, "route_complete_shipments")
    assert len(rcs) == 1 and len(rcs[0]) == 27 and rcs[0]["shipment_num"] == r["shipment_num"]
    cov = _row(db, "covered_loads.covered_loads")
    assert len(cov) == 1 and set(("customer", "carrier", "equipment")) <= set(cov[0])
    assert cov[0]["customer"] == r["customer_name"] and cov[0]["carrier"] == r["carrier_name"]
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM carriers")
        assert cur.fetchone()[0] == 1


def test_route_complete_for_a_never_tracked_shipment_creates_only_a_t3ra_row(db, stubs, payload):
    with db.cursor(commit=True) as cur:
        cur.execute("TRUNCATE shipments, webhook_queue RESTART IDENTITY")
    stubs["payload"] = copy.deepcopy(payload)
    assert _run(db, "Route Complete")[0] == "done"
    assert len(_row(db, "route_complete_shipments")) == 1
    assert _row(db, "covered_loads.covered_loads") == []
