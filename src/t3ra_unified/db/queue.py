"""Durable webhook queue backed by ``webhook_queue`` (one queue for every shipment event).

The receiver enqueues raw bodies and returns immediately; the worker claims
them with ``FOR UPDATE SKIP LOCKED`` so several workers can run side by side.
Duplicate deliveries are deduplicated by a SHA-256 of the raw body.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg2.extras import Json

from t3ra_unified.db import engine
from t3ra_unified.config import get_settings
from t3ra_unified.observability import traced_function

logger = logging.getLogger(__name__)

TABLE = "webhook_queue"

#: Backoff between retries: 30s, 60s, 120s ... capped at 30 minutes.
_RETRY_BASE_SECONDS = 30
_RETRY_CAP_SECONDS = 1800


def compute_event_key(raw_body: bytes) -> str:
    """Stable identity for a delivery; equal bodies enqueue once."""
    return hashlib.sha256(raw_body).hexdigest()


@traced_function()
def enqueue_webhook_event(
    raw_body: bytes,
    payload: dict[str, Any] | None,
    shipment_id: int | None,
    status_value: str | None,
) -> dict[str, Any]:
    """Persist a delivery. Re-deliveries of the same body are no-ops."""
    event_key = compute_event_key(raw_body)

    query = f"""
    INSERT INTO {TABLE} (event_key, shipment_id, status_value, payload_json, state)
    VALUES (%s, %s, %s, %s, 'pending')
    ON CONFLICT (event_key) DO NOTHING
    RETURNING id;
    """

    with engine.cursor(commit=True) as cur:
        cur.execute(
            query,
            (
                event_key,
                shipment_id,
                status_value,
                Json(payload) if payload is not None else None,
            ),
        )
        row = cur.fetchone()

    result = {
        "event_key": event_key,
        "inserted": row is not None,
        "event_id": row[0] if row else None,
    }
    logger.info(
        "Webhook event enqueue attempted",
        extra={**result, "shipment_id": shipment_id, "status_value": status_value},
    )
    return result


@traced_function()
def claim_events(batch_size: int, worker_id: str) -> list[dict[str, Any]]:
    """Atomically claim up to ``batch_size`` due events for this worker."""
    query = f"""
    WITH cte AS (
        SELECT id
        FROM {TABLE}
        WHERE state IN ('pending', 'retry')
          AND next_attempt_at <= NOW()
        ORDER BY received_at
        FOR UPDATE SKIP LOCKED
        LIMIT %s
    )
    UPDATE {TABLE} e
    SET state = 'processing',
        worker_id = %s,
        attempt_count = attempt_count + 1
    FROM cte
    WHERE e.id = cte.id
    RETURNING e.id, e.event_key, e.shipment_id, e.status_value, e.payload_json, e.attempt_count;
    """
    with engine.cursor(dict_rows=True, commit=True) as cur:
        cur.execute(query, (batch_size, worker_id))
        rows = cur.fetchall()

    if rows:
        logger.info("Claimed queue events", extra={"worker_id": worker_id, "batch_size": len(rows)})
    return [dict(row) for row in rows]


@traced_function()
def mark_done(event_id: int) -> None:
    with engine.cursor(commit=True) as cur:
        cur.execute(
            f"UPDATE {TABLE} SET state='done', processed_at=NOW(), last_error=NULL WHERE id=%s",
            (event_id,),
        )
    logger.info("Event marked done", extra={"event_id": event_id})


@traced_function()
def mark_ignored(event_id: int, reason: str) -> None:
    with engine.cursor(commit=True) as cur:
        cur.execute(
            f"UPDATE {TABLE} SET state='ignored', processed_at=NOW(), last_error=%s WHERE id=%s",
            (reason[:2000], event_id),
        )
    logger.info("Event marked ignored", extra={"event_id": event_id, "reason": reason[:2000]})


@traced_function()
def release_events(event_ids: list[int], delay_seconds: int, reason: str) -> None:
    """Hand claimed events back untried: the claim's attempt is not counted.

    For when the worker cannot try at all (Turvo will not let it log in), so a
    blocked hour does not burn every event's attempts and dead-letter the queue.
    """
    if not event_ids:
        return
    next_attempt = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    with engine.cursor(commit=True) as cur:
        cur.execute(
            f"""
            UPDATE {TABLE}
            SET state = 'retry', next_attempt_at = %s, last_error = %s,
                attempt_count = GREATEST(attempt_count - 1, 0)
            WHERE id = ANY(%s) AND state = 'processing'
            """,
            (next_attempt, reason[:4000], list(event_ids)),
        )
    logger.warning("Events released untried", extra={"count": len(event_ids), "reason": reason[:500]})


def retry_delay_seconds(attempt_count: int) -> int:
    """Backoff for the next attempt, capped so a stuck event still retries hourly-ish."""
    return min(_RETRY_CAP_SECONDS, _RETRY_BASE_SECONDS * (2 ** max(0, attempt_count - 1)))


@traced_function()
def mark_retry_or_dead(event_id: int, attempt_count: int, max_attempts: int, error_text: str) -> None:
    """Schedule another attempt, or dead-letter the event once attempts run out."""
    is_dead = attempt_count >= max_attempts

    if is_dead:
        query = f"UPDATE {TABLE} SET state='dead', processed_at=NOW(), last_error=%s WHERE id=%s"
        params: tuple[Any, ...] = (error_text[:4000], event_id)
    else:
        next_attempt = datetime.now(timezone.utc) + timedelta(seconds=retry_delay_seconds(attempt_count))
        query = f"UPDATE {TABLE} SET state='retry', next_attempt_at=%s, last_error=%s WHERE id=%s"
        params = (next_attempt, error_text[:4000], event_id)

    with engine.cursor(commit=True) as cur:
        cur.execute(query, params)

    logger.warning(
        "Event marked retry/dead",
        extra={
            "event_id": event_id,
            "attempt_count": attempt_count,
            "max_attempts": max_attempts,
            "dead": is_dead,
            "error_preview": (error_text or "")[:500],
        },
    )


@traced_function()
def queue_counts() -> dict[str, int]:
    """Row count per state -- the basis for queue-depth alerting."""
    counts: dict[str, int] = {}
    with engine.cursor() as cur:
        cur.execute(f"SELECT state, COUNT(*) FROM {TABLE} GROUP BY state")
        for state, count in cur.fetchall():
            counts[str(state)] = int(count)
    logger.info("Queue counts snapshot", extra={"queue_counts": json.dumps(counts)})
    return counts


@traced_function()
def reset_dead_events(event_ids: list[int] | None = None) -> int:
    """Move dead-lettered events back to pending so a worker retries them.

    With no ids, every dead event is requeued. Used by the runbook after the
    underlying cause of a failure has been fixed.
    """
    if event_ids:
        query = f"UPDATE {TABLE} SET state='pending', next_attempt_at=NOW(), attempt_count=0 WHERE state='dead' AND id = ANY(%s)"
        params: tuple[Any, ...] = (event_ids,)
    else:
        query = f"UPDATE {TABLE} SET state='pending', next_attempt_at=NOW(), attempt_count=0 WHERE state='dead'"
        params = ()

    with engine.cursor(commit=True) as cur:
        cur.execute(query, params)
        affected = cur.rowcount

    logger.info("Dead events requeued", extra={"affected_rows": affected})
    return affected


@traced_function()
def purge_old_events(payload_days: int = None, event_days: int = None,
                     dry_run: bool = False) -> dict:
    """Trim completed queue events so the table stops growing without bound.

    This table is the closest thing here to a log: every webhook is kept, with
    its full payload, and nothing ever removed one. At ~160 events a day it is
    already the second largest table in the database.

    Two stages, because the payload and the row are worth different amounts:

    * The payload is most of the bytes and is only useful while a failure is
      still being investigated, so it is cleared first.
    * The row itself carries event_key, which is what stops Turvo re-delivering
      a webhook and having us reprocess the shipment. Dropping it early would
      cost a wasted API call on a re-delivery, so rows go much later.

    Events in the 'dead' state are never touched. They failed every retry, they
    are few, and their payload is exactly what you need to work out why.
    """
    payload_days = get_settings().queue_payload_retention_days if payload_days is None else payload_days
    event_days = get_settings().queue_event_retention_days if event_days is None else event_days

    finished = ("done", "ignored")
    counts = {}

    with engine.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT count(*) FROM webhook_queue
                   WHERE state = ANY(%s) AND payload_json IS NOT NULL
                     AND received_at < NOW() - make_interval(days => %s);""",
                (list(finished), payload_days),
            )
            counts["payloads_to_clear"] = cur.fetchone()[0]

            cur.execute(
                """SELECT count(*) FROM webhook_queue
                   WHERE state = ANY(%s)
                     AND received_at < NOW() - make_interval(days => %s);""",
                (list(finished), event_days),
            )
            counts["events_to_delete"] = cur.fetchone()[0]

            if dry_run:
                return counts

            # Deleting first would make the payload update do redundant work.
            cur.execute(
                """UPDATE webhook_queue SET payload_json = NULL
                   WHERE state = ANY(%s) AND payload_json IS NOT NULL
                     AND received_at < NOW() - make_interval(days => %s);""",
                (list(finished), payload_days),
            )
            counts["payloads_cleared"] = cur.rowcount

            cur.execute(
                """DELETE FROM webhook_queue
                   WHERE state = ANY(%s)
                     AND received_at < NOW() - make_interval(days => %s);""",
                (list(finished), event_days),
            )
            counts["events_deleted"] = cur.rowcount
        conn.commit()

    logger.info(
        "Queue purge | payloads_cleared=%s events_deleted=%s payload_days=%s event_days=%s",
        counts.get("payloads_cleared"), counts.get("events_deleted"), payload_days, event_days,
    )
    return counts
