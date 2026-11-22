"""The one Turvo webhook receiver.

Authenticates a delivery, writes it to ``webhook_queue`` and returns. Nothing
else happens on the request: all Turvo API work is the worker's, so a slow
upstream can never cause a webhook timeout and a lost event.

Every shipment event is queued; the worker decides which lifecycle phase (if
any) applies. Auth accepts the token the way the T3RA receiver always did
(``?token=``, ``Authorization: Bearer``, ``token`` / ``x-turvo-token`` headers),
compared in constant time.
"""

from __future__ import annotations

import hmac
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from t3ra_unified.config import get_settings
from t3ra_unified.db import engine
from t3ra_unified.db.queue import compute_event_key, enqueue_webhook_event, queue_counts
from t3ra_unified.observability import bind_event_key, bind_shipment_id

logger = logging.getLogger(__name__)


def _safe_get(data: dict[str, Any], path: str) -> Any:
    """Walk a dotted path, returning None rather than raising on any gap."""
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def extract_token(request: Request) -> str | None:
    token = request.query_params.get("token")
    if token:
        return token
    auth = request.headers.get("authorization")
    if auth:
        return auth[7:] if auth.lower().startswith("bearer ") else auth
    return request.headers.get("token") or request.headers.get("x-turvo-token")


def verify_token(token: str | None) -> None:
    expected = get_settings().webhook_shared_token.get_secret_value()
    if not token or not hmac.compare_digest(token.strip(), expected):
        raise HTTPException(status_code=401, detail="Bad token")


def enqueue_delivery(raw: bytes, payload: dict[str, Any]) -> dict[str, Any]:
    """Queue one Turvo delivery for the worker. Re-deliveries are no-ops."""
    shipment_id_raw = _safe_get(payload, "eventPayload.id")
    try:
        shipment_id = int(shipment_id_raw) if shipment_id_raw is not None else None
    except (TypeError, ValueError):
        shipment_id = None

    status_value = _safe_get(payload, "eventPayload.status.code.value") or _safe_get(
        payload, "eventPayload.status.description"
    )
    event_key = compute_event_key(raw)
    with bind_event_key(event_key), bind_shipment_id(str(shipment_id) if shipment_id else "-"):
        logger.info(
            "Turvo webhook received",
            extra={"shipment_id": shipment_id, "status_value": status_value, "event_key": event_key},
        )
        return enqueue_webhook_event(
            raw_body=raw, payload=payload, shipment_id=shipment_id, status_value=status_value
        )


def readiness() -> tuple[bool, dict[str, Any]]:
    """The database answers and the queue depth could be read."""
    db_ok = engine.healthcheck()
    counts: dict[str, int] = {}
    if db_ok:
        try:
            counts = queue_counts()
        except Exception:
            logger.exception("Queue depth check failed")
            db_ok = False
    return db_ok, {
        "ok": db_ok,
        "database": "up" if db_ok else "down",
        "queue": counts,
        "time": datetime.now(timezone.utc).isoformat(),
    }


def build_ready_router(path: str) -> APIRouter:
    router = APIRouter(tags=["shipments"])

    @router.get(path)
    async def ready(response: Response) -> dict[str, Any]:
        ok, body = readiness()
        if not ok:
            response.status_code = 503
        return body

    return router


def build_webhook_router() -> APIRouter:
    """The Turvo webhook at ``settings.webhook_path``."""
    settings = get_settings()
    router = APIRouter(tags=["shipments"])

    @router.post(settings.webhook_path)
    async def turvo_webhook(request: Request) -> dict[str, Any]:
        verify_token(extract_token(request))

        raw = await request.body()
        try:
            payload: dict[str, Any] = await request.json()
        except Exception:
            payload = {}

        try:
            result = enqueue_delivery(raw, payload)
        except Exception as exc:
            # Non-2xx so Turvo redelivers; a lost webhook is a lost load.
            logger.exception("Queue persist failed")
            raise HTTPException(status_code=503, detail="queue persist failed") from exc

        return {
            "status": "accepted",
            "queued": bool(result["inserted"]),
            "duplicate": not bool(result["inserted"]),
            "event_id": result["event_id"],
            "event_key": result["event_key"],
        }

    return router
