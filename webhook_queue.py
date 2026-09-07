import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from psycopg2.extras import Json, RealDictCursor

from db import get_conn

logger = logging.getLogger(__name__)

_SCHEMA_READY = False


def ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return

    q = """
    CREATE TABLE IF NOT EXISTS public.webhook_queue (
        id BIGSERIAL PRIMARY KEY,
        event_key TEXT UNIQUE NOT NULL,
        shipment_id BIGINT,
        status_value TEXT,
        payload_json JSONB,
        state TEXT NOT NULL DEFAULT 'pending', -- pending | processing | retry | done | ignored | dead
        attempt_count INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        processed_at TIMESTAMPTZ,
        worker_id TEXT,
        last_error TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_webhook_queue_state_next
      ON public.webhook_queue (state, next_attempt_at, received_at);
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q)
        conn.commit()
    _SCHEMA_READY = True


def compute_event_key(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()


def enqueue_event(
    raw_body: bytes,
    payload: Optional[Dict[str, Any]],
    shipment_id: Optional[int],
    status_value: Optional[str],
) -> Dict[str, Any]:
    ensure_schema()
    ev_key = compute_event_key(raw_body)

    q = """
    INSERT INTO public.webhook_queue (event_key, shipment_id, status_value, payload_json, state)
    VALUES (%s, %s, %s, %s, 'pending')
    ON CONFLICT (event_key) DO NOTHING
    RETURNING id;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, (ev_key, shipment_id, status_value, Json(payload) if payload is not None else None))
            row = cur.fetchone()
        conn.commit()

    return {"event_key": ev_key, "inserted": row is not None, "event_id": row[0] if row else None}


def claim_events(batch_size: int, worker_id: str) -> List[Dict[str, Any]]:
    ensure_schema()
    q = """
    WITH cte AS (
        SELECT id
        FROM public.webhook_queue
        WHERE state IN ('pending', 'retry')
          AND next_attempt_at <= NOW()
        ORDER BY received_at
        FOR UPDATE SKIP LOCKED
        LIMIT %s
    )
    UPDATE public.webhook_queue q
    SET state = 'processing', worker_id = %s, attempt_count = attempt_count + 1
    FROM cte
    WHERE q.id = cte.id
    RETURNING q.id, q.event_key, q.shipment_id, q.status_value, q.payload_json, q.attempt_count;
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(q, (batch_size, worker_id))
            rows = cur.fetchall()
        conn.commit()
    return rows


def mark_done(event_id: int) -> None:
    q = "UPDATE public.webhook_queue SET state='done', processed_at=NOW(), last_error=NULL WHERE id=%s;"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, (event_id,))
        conn.commit()


def mark_ignored(event_id: int, reason: str) -> None:
    q = "UPDATE public.webhook_queue SET state='ignored', processed_at=NOW(), last_error=%s WHERE id=%s;"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, (reason[:2000], event_id))
        conn.commit()


def defer_for_auth(event_id: int, cooldown_seconds: int, error_text: str) -> None:
    """Park an event that failed purely because Turvo rejected our credentials.

    The event keeps its data and is retried once the credentials are fixed, but
    the attempt is refunded so a credential outage doesn't burn through
    max_attempts and kill the whole backlog.
    """
    next_time = datetime.utcnow() + timedelta(seconds=cooldown_seconds)
    q = """
    UPDATE public.webhook_queue
    SET state='retry',
        next_attempt_at=%s,
        attempt_count=GREATEST(0, attempt_count - 1),
        last_error=%s
    WHERE id=%s;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, (next_time, error_text[:4000], event_id))
        conn.commit()


def mark_retry_or_dead(event_id: int, attempt_count: int, max_attempts: int, error_text: str) -> None:
    if attempt_count >= max_attempts:
        q = "UPDATE public.webhook_queue SET state='dead', processed_at=NOW(), last_error=%s WHERE id=%s;"
        params = (error_text[:4000], event_id)
    else:
        delay_seconds = min(1800, 30 * (2 ** max(0, attempt_count - 1)))
        next_time = datetime.utcnow() + timedelta(seconds=delay_seconds)
        q = "UPDATE public.webhook_queue SET state='retry', next_attempt_at=%s, last_error=%s WHERE id=%s;"
        params = (next_time, error_text[:4000], event_id)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, params)
        conn.commit()
