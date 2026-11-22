"""Queue worker: one Turvo fetch per event, then the lifecycle phase(s) that apply.

Lifecycle of a shipment, in one place:

* **Covered phase** (the former Covered Loads worker): a shipment already
  tracked, or arriving as covered / processing / completed, is stored and priced
  (GS / DAT) according to its status.
* **Completion phase** (the former T3RA worker): once the status is route
  complete / completed, the market areas, the carrier and the final costs are
  stored.

Both write the same ``shipments`` row. A completed shipment runs both, in that
order, from the same payload. Claims use ``FOR UPDATE SKIP LOCKED`` so several
workers can run side by side; a failure schedules a retry with backoff and the
event is dead-lettered after ``WORKER_MAX_ATTEMPTS``.

A failed Turvo login is not an event failure: the events go back untried and the
worker waits, so one bad password cannot dead-letter the queue or lock the
account out.
"""

from __future__ import annotations

import argparse
import logging
import signal
import socket
import time
from types import FrameType
from typing import Any

from t3ra_unified.clients.turvo import TurvoAuthError, auth_blocked, fetch_shipment_details
from t3ra_unified.completion.service import process_completed_shipment
from t3ra_unified.config import get_settings
from t3ra_unified.covered.repository import shipment_id_exists
from t3ra_unified.covered.services.shipment_processor import process_shipment_event
from t3ra_unified.db.queue import (
    claim_events,
    mark_done,
    mark_ignored,
    mark_retry_or_dead,
    queue_counts,
    release_events,
)
from t3ra_unified.observability import (
    bind_event_key,
    bind_request_id,
    bind_shipment_id,
    configure_logfire,
    configure_logging,
    traced_function,
)

logger = logging.getLogger(__name__)

#: Statuses that start tracking a shipment the Covered side does not hold yet.
COVERED_NEW_SHIPMENT_STATUSES = frozenset({"covered", "processing", "completed"})

#: Turvo event names the Covered phase handles (both are shipment updates).
COVERED_EVENT_NAMES = frozenset({"SHIPMENT_STATUS_UPDATE", "SHIPMENT_UPDATE"})

#: Seconds the worker waits, and events are pushed back, while Turvo login is blocked.
AUTH_PAUSE_SECONDS = 300
_AUTH_BLOCKED = "Turvo login failed; not tried until the credentials are fixed and the worker restarts"

_shutdown_requested = False


def _request_shutdown(signum: int, _frame: FrameType | None) -> None:
    global _shutdown_requested
    _shutdown_requested = True
    logger.info("Shutdown signal received; finishing current batch", extra={"signal": signum})


def _pause(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not _shutdown_requested and time.monotonic() < deadline:
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def _safe_get(data: dict[str, Any], path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def decide_phases(event_name: str, status_norm: str, is_tracked: bool) -> tuple[bool, bool]:
    """``(run_covered, run_completion)`` for one event.

    Covered: a shipment-update event for a shipment already on the dashboard
    (any status -- a cancellation is as much a fact about it as a cover), or for
    an unknown shipment whose status starts tracking.
    Completion: the status is one of the completion statuses, whatever the
    event name -- except carrier events, whose id is a carrier's, not a shipment's.
    """
    completion_statuses = {
        s.strip().lower() for s in get_settings().completion_statuses.split(",") if s.strip()
    }
    run_covered = event_name in COVERED_EVENT_NAMES and (
        is_tracked or status_norm in COVERED_NEW_SHIPMENT_STATUSES
    )
    run_completion = status_norm in completion_statuses and not event_name.startswith("CARRIER")
    return run_covered, run_completion


@traced_function()
def process_one(event: dict[str, Any], max_attempts: int) -> None:
    """Handle one claimed event. Never raises -- outcomes are recorded in the queue."""
    event_id = int(event["id"])
    event_key = event.get("event_key")
    attempt_count = int(event.get("attempt_count") or 1)
    payload = event.get("payload_json") or {}

    shipment_id = event.get("shipment_id")
    if shipment_id is None:
        raw_id = _safe_get(payload, "eventPayload.id")
        try:
            shipment_id = int(raw_id) if raw_id is not None else None
        except (TypeError, ValueError):
            shipment_id = None

    status_value = event.get("status_value") or _safe_get(payload, "eventPayload.status.code.value") or _safe_get(
        payload, "eventPayload.status.description"
    )
    status_norm = (status_value or "").strip().lower()
    event_name = str(_safe_get(payload, "eventName") or "").strip().upper()

    request_id = f"webhook-{str(event_key)[:12]}" if event_key else f"queue-event-{event_id}"
    log_context = {
        "event_id": event_id,
        "event_key": event_key,
        "attempt_count": attempt_count,
        "max_attempts": max_attempts,
        "shipment_id": shipment_id,
        "status_value": status_value,
        "event_name": event_name,
    }

    with bind_request_id(request_id), bind_event_key(event_key), bind_shipment_id(
        str(shipment_id) if shipment_id else "-"
    ):
        logger.info("Event processing started", extra=log_context)

        if not shipment_id:
            mark_ignored(event_id, "missing shipment_id")
            logger.warning("Event ignored: missing shipment_id", extra={**log_context, "outcome": "ignored"})
            return

        is_tracked = shipment_id_exists(int(shipment_id)) if event_name in COVERED_EVENT_NAMES else False
        run_covered, run_completion = decide_phases(event_name, status_norm, is_tracked)
        if not run_covered and not run_completion:
            mark_ignored(event_id, f"event '{event_name or '-'}' status '{status_value}' needs no processing")
            logger.info("Event ignored by phase filter", extra={**log_context, "outcome": "ignored"})
            return

        try:
            shipment = fetch_shipment_details(int(shipment_id))
            if run_covered:
                process_shipment_event(shipment, status_value=status_value)
            if run_completion:
                process_completed_shipment(shipment)
            mark_done(event_id)
            logger.info(
                "Event processed successfully",
                extra={**log_context, "covered": run_covered, "completion": run_completion, "outcome": "done"},
            )
        except Exception as exc:
            error = repr(exc)
            if isinstance(exc, TurvoAuthError) or auth_blocked():
                # Not this event's fault: no attempt spent, no traceback.
                release_events([event_id], AUTH_PAUSE_SECONDS, f"{_AUTH_BLOCKED}: {error}")
                logger.warning(
                    "Event released: Turvo login is blocked",
                    extra={**log_context, "error_preview": error[:500], "outcome": "released"},
                )
                return
            mark_retry_or_dead(event_id, attempt_count, max_attempts, error)
            logger.exception(
                "Event processing failed",
                extra={**log_context, "error_preview": error[:500], "outcome": "retry_or_dead"},
            )


def run_forever(batch_size: int, sleep_seconds: float, max_attempts: int) -> None:
    """Claim-and-process loop. Returns once a shutdown signal is handled."""
    worker_id = f"{socket.gethostname()}-{int(time.time())}"
    logger.info(
        "Worker started",
        extra={"worker_id": worker_id, "batch_size": batch_size, "sleep_seconds": sleep_seconds,
               "max_attempts": max_attempts},
    )

    while not _shutdown_requested:
        if auth_blocked():
            logger.warning(_AUTH_BLOCKED, extra={"worker_id": worker_id, "pause_seconds": AUTH_PAUSE_SECONDS})
            _pause(AUTH_PAUSE_SECONDS)
            continue

        try:
            events = claim_events(max(1, batch_size), worker_id)
        except Exception:
            logger.exception("Failed to claim events; backing off")
            time.sleep(max(1.0, sleep_seconds))
            continue

        if not events:
            time.sleep(max(0.2, sleep_seconds))
            continue

        for i, event in enumerate(events):
            if _shutdown_requested:
                break
            if auth_blocked():
                release_events([int(e["id"]) for e in events[i:]], AUTH_PAUSE_SECONDS, _AUTH_BLOCKED)
                break
            process_one(event, max_attempts)

        logger.info("Queue counts", extra={"queue_counts": queue_counts()})

    logger.info("Worker stopped", extra={"worker_id": worker_id})


def main() -> None:
    """Console entry point: ``t3ra-worker``."""
    settings = get_settings()

    parser = argparse.ArgumentParser(description="Unified shipment queue worker")
    parser.add_argument("--batch-size", type=int, default=settings.worker_batch_size)
    parser.add_argument("--sleep-seconds", type=float, default=settings.worker_sleep_seconds)
    parser.add_argument("--max-attempts", type=int, default=settings.worker_max_attempts)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    configure_logging(service_name="t3ra-worker", level_override="DEBUG" if args.verbose else None)
    configure_logfire(service_name="t3ra-worker")
    logger.info("Worker configuration", extra=settings.safe_summary())

    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)

    run_forever(args.batch_size, args.sleep_seconds, args.max_attempts)


if __name__ == "__main__":
    main()
