"""Tests for the worker's admission rules.

Everything past the gates is stubbed -- the queue, the Turvo fetch and the
processor -- so these cover only which events are let through, and at what cost.
"""

from __future__ import annotations

import pytest

from t3ra_unified import worker as ww


def _event(event_name: str = "SHIPMENT_UPDATE", status: str = "Covered", shipment_id: int = 115792084):
    """A claimed queue row, shaped the way claim_events returns one."""
    return {
        "id": 1,
        "event_key": "abc123",
        "attempt_count": 1,
        "shipment_id": shipment_id,
        "status_value": status,
        "payload_json": {
            "eventName": event_name,
            "eventPayload": {"id": shipment_id, "status": {"code": {"value": status}}},
        },
    }


@pytest.fixture
def worker(monkeypatch):
    """Record the worker's outcome and whether it reached Turvo."""
    calls: dict = {"ignored": [], "done": [], "retried": [], "fetched": [], "processed": [], "completed": [],
                   "tracked": False}

    monkeypatch.setattr(ww, "mark_ignored", lambda event_id, reason: calls["ignored"].append(reason))
    monkeypatch.setattr(ww, "mark_done", lambda event_id: calls["done"].append(event_id))
    monkeypatch.setattr(
        ww,
        "mark_retry_or_dead",
        lambda event_id, attempts, max_attempts, error: calls["retried"].append(error),
    )
    monkeypatch.setattr(ww, "shipment_id_exists", lambda shipment_id: calls["tracked"])
    monkeypatch.setattr(
        ww, "fetch_shipment_details", lambda shipment_id: calls["fetched"].append(shipment_id) or {"details": {}}
    )
    monkeypatch.setattr(
        ww,
        "process_shipment_event",
        lambda shipment, status_value: calls["processed"].append(status_value) or {"ok": True},
    )
    monkeypatch.setattr(
        ww, "process_completed_shipment", lambda shipment: calls["completed"].append(True) or {"ok": True}
    )
    return calls


class TestEventNameFilter:
    @pytest.mark.parametrize("event_name", ["SHIPMENT_UPDATE", "SHIPMENT_STATUS_UPDATE"])
    def test_both_shipment_update_events_are_handled(self, worker, event_name):
        ww.process_one(_event(event_name=event_name), max_attempts=5)

        assert worker["processed"] == ["Covered"]
        assert worker["ignored"] == []

    @pytest.mark.parametrize("event_name", ["ORDER_UPDATE", "", "shipment_delete"])
    def test_any_other_event_is_ignored_without_a_turvo_call(self, worker, event_name):
        ww.process_one(_event(event_name=event_name), max_attempts=5)

        assert worker["processed"] == []
        assert worker["fetched"] == [], "an unhandled event must not cost a Turvo fetch"
        assert len(worker["ignored"]) == 1

    def test_the_event_name_is_matched_case_insensitively(self, worker):
        ww.process_one(_event(event_name="shipment_status_update"), max_attempts=5)
        assert worker["processed"] == ["Covered"]


class TestStatusFilter:
    """The status gate decides what to start tracking, not what to refresh."""

    @pytest.mark.parametrize("status", ["Covered", "Processing", "Completed"])
    def test_a_processable_status_admits_an_unknown_shipment(self, worker, status):
        worker["tracked"] = False
        ww.process_one(_event(status=status), max_attempts=5)

        assert worker["processed"] == [status]

    @pytest.mark.parametrize("status", ["Tendered", "Cancelled"])
    def test_another_status_on_an_unknown_shipment_is_ignored(self, worker, status):
        worker["tracked"] = False
        ww.process_one(_event(status=status), max_attempts=5)

        assert worker["processed"] == []
        assert worker["fetched"] == []
        assert len(worker["ignored"]) == 1

    @pytest.mark.parametrize("status", ["Tendered", "Cancelled", "Route Complete"])
    def test_the_same_status_on_a_tracked_shipment_is_processed(self, worker, status):
        worker["tracked"] = True
        ww.process_one(_event(status=status), max_attempts=5)

        assert worker["processed"] == [status]
        assert worker["ignored"] == []

    def test_a_tracked_shipment_with_no_status_at_all_is_still_refreshed(self, worker):
        worker["tracked"] = True
        event = _event(status="")
        event["status_value"] = None
        event["payload_json"]["eventPayload"]["status"] = {}

        ww.process_one(event, max_attempts=5)
        assert worker["fetched"] == [115792084]


class TestOutcomes:
    def test_a_missing_shipment_id_is_ignored(self, worker):
        event = _event()
        event["shipment_id"] = None
        event["payload_json"]["eventPayload"]["id"] = None

        ww.process_one(event, max_attempts=5)
        assert worker["ignored"] == ["missing shipment_id"]

    def test_a_processing_failure_is_scheduled_for_retry(self, worker, monkeypatch):
        def boom(shipment, status_value):
            raise RuntimeError("turvo exploded")

        monkeypatch.setattr(ww, "process_shipment_event", boom)
        ww.process_one(_event(), max_attempts=5)

        assert worker["done"] == []
        assert "turvo exploded" in worker["retried"][0]


class TestTurvoLoginBlocked:
    """A failed login latches; the worker must wait, not fail every event against it."""

    @pytest.fixture
    def blocked(self, worker, monkeypatch):
        state = {"blocked": False, "released": [], "claims": 0, "paused": []}
        monkeypatch.setattr(ww, "auth_blocked", lambda: state["blocked"])
        monkeypatch.setattr(
            ww, "release_events", lambda ids, delay, reason: state["released"].append((list(ids), delay))
        )
        monkeypatch.setattr(ww, "queue_counts", lambda: {})
        return state

    def test_an_event_that_hits_the_failed_login_is_released_untried(self, worker, blocked, monkeypatch):
        def login_fails(shipment_id):
            blocked["blocked"] = True
            raise RuntimeError("Authentication rejected with HTTP 401")

        monkeypatch.setattr(ww, "fetch_shipment_details", login_fails)
        ww.process_one(_event(), max_attempts=5)

        assert worker["retried"] == [], "a blocked login must not spend the event's attempts"
        assert blocked["released"] == [([1], ww.AUTH_PAUSE_SECONDS)]

    def test_an_ordinary_failure_still_retries(self, worker, blocked, monkeypatch):
        monkeypatch.setattr(ww, "fetch_shipment_details", lambda shipment_id: (_ for _ in ()).throw(ValueError("x")))
        ww.process_one(_event(), max_attempts=5)

        assert len(worker["retried"]) == 1
        assert blocked["released"] == []

    def test_the_rest_of_the_batch_goes_back_once_the_login_fails(self, worker, blocked, monkeypatch):
        events = [{**_event(), "id": n} for n in (1, 2, 3)]

        def claim(batch_size, worker_id):
            blocked["claims"] += 1
            if blocked["claims"] > 1:
                ww._shutdown_requested = True
                return []
            return events

        def login_fails(shipment_id):
            blocked["blocked"] = True
            raise RuntimeError("Authentication rejected with HTTP 401")

        monkeypatch.setattr(ww, "claim_events", claim)
        monkeypatch.setattr(ww, "fetch_shipment_details", login_fails)
        monkeypatch.setattr(ww, "_pause", lambda seconds: setattr(ww, "_shutdown_requested", True))
        monkeypatch.setattr(ww, "_shutdown_requested", False)

        ww.run_forever(batch_size=3, sleep_seconds=0, max_attempts=5)

        assert blocked["released"] == [([1], ww.AUTH_PAUSE_SECONDS), ([2, 3], ww.AUTH_PAUSE_SECONDS)]
        assert worker["fetched"] == [], "only the first event may reach Turvo"
        assert worker["retried"] == []

    def test_a_blocked_worker_claims_nothing_and_waits(self, worker, blocked, monkeypatch):
        blocked["blocked"] = True
        monkeypatch.setattr(ww, "claim_events", lambda *a: blocked.__setitem__("claims", blocked["claims"] + 1) or [])
        monkeypatch.setattr(
            ww, "_pause", lambda seconds: blocked["paused"].append(seconds) or setattr(ww, "_shutdown_requested", True)
        )
        monkeypatch.setattr(ww, "_shutdown_requested", False)

        ww.run_forever(batch_size=10, sleep_seconds=0, max_attempts=5)

        assert blocked["claims"] == 0
        assert blocked["paused"] == [ww.AUTH_PAUSE_SECONDS]


class TestLifecyclePhases:
    """One Turvo fetch per event; the status decides which phase(s) write."""

    def test_route_complete_on_an_unknown_shipment_runs_only_the_completion_phase(self, worker):
        worker["tracked"] = False
        ww.process_one(_event(status="Route Complete"), max_attempts=5)

        assert worker["fetched"] == [115792084]
        assert worker["processed"] == []
        assert worker["completed"] == [True]
        assert worker["done"] == [1]

    def test_route_complete_on_a_tracked_shipment_runs_both_phases_from_one_fetch(self, worker):
        worker["tracked"] = True
        ww.process_one(_event(status="Route Complete"), max_attempts=5)

        assert worker["fetched"] == [115792084]
        assert worker["processed"] == ["Route Complete"]
        assert worker["completed"] == [True]

    @pytest.mark.parametrize("status", ["Covered", "Processing", "Tendered"])
    def test_an_open_status_never_runs_the_completion_phase(self, worker, status):
        worker["tracked"] = True
        ww.process_one(_event(status=status), max_attempts=5)

        assert worker["processed"] == [status]
        assert worker["completed"] == []

    def test_completed_runs_both_phases(self, worker):
        worker["tracked"] = False
        ww.process_one(_event(status="Completed"), max_attempts=5)
        assert worker["processed"] == ["Completed"]
        assert worker["completed"] == [True]

    def test_a_carrier_event_never_runs_completion(self, worker):
        ww.process_one(_event(event_name="CARRIER_UPDATE", status="Completed"), max_attempts=5)
        assert worker["completed"] == []
        assert worker["fetched"] == []
        assert len(worker["ignored"]) == 1
