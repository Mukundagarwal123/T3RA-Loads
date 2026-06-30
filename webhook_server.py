import logging
from typing import Any, Dict, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request

import config
from webhook_queue import enqueue_event

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Turvo Route-Complete Webhook Receiver")


def _safe_get(d: Dict[str, Any], path: str):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


@app.get("/health")
async def health():
    return {"ok": True}


def _extract_token(request: Request) -> Optional[str]:
    qp_token = request.query_params.get("token")
    if qp_token:
        return qp_token

    auth_header = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth_header:
        return auth_header[7:] if auth_header.lower().startswith("bearer ") else auth_header

    return request.headers.get("token") or request.headers.get("x-turvo-token")


@app.post(config.WEBHOOK_PATH)
async def turvo_webhook(request: Request):
    token = _extract_token(request)
    if token != config.WEBHOOK_SHARED_TOKEN:
        logger.warning(
            "AUTH FAILED | headers=%s | query_params=%s | extracted_token=%r",
            dict(request.headers),
            dict(request.query_params),
            token,
        )
        raise HTTPException(status_code=401, detail="Bad token")

    raw = await request.body()
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        payload = {}

    logger.info("Webhook received | raw_payload=%s", payload)

    shipment_id_raw = _safe_get(payload, "eventPayload.id")
    status_value = _safe_get(payload, "eventPayload.status.code.value") or _safe_get(
        payload, "eventPayload.status.description"
    )
    try:
        shipment_id: Optional[int] = int(shipment_id_raw) if shipment_id_raw is not None else None
    except Exception:
        shipment_id = None

    status_norm = (status_value or "").strip().lower()
    logger.info(
        "Parsed webhook | shipment_id=%s status_value=%r status_norm=%r trigger_statuses=%s",
        shipment_id, status_value, status_norm, config.TRIGGER_STATUSES,
    )

    if status_norm not in config.TRIGGER_STATUSES:
        logger.info(
            "Webhook IGNORED (status not eligible) | shipment_id=%s status_value=%r",
            shipment_id, status_value,
        )
        return {"status": "ignored", "reason": "status not eligible"}

    if shipment_id is None:
        logger.warning("Webhook IGNORED (missing shipment id) | status_value=%r", status_value)
        return {"status": "ignored", "reason": "missing shipment id"}

    try:
        enq = enqueue_event(
            raw_body=raw,
            payload=payload,
            shipment_id=shipment_id,
            status_value=status_value,
        )
    except Exception:
        logger.exception("Queue persist FAILED | shipment_id=%s", shipment_id)
        raise HTTPException(status_code=503, detail="queue persist failed")

    logger.info(
        "Webhook ENQUEUED | shipment_id=%s event_id=%s queued=%s",
        shipment_id, enq.get("event_id"), bool(enq.get("inserted")),
    )
    return {
        "status": "accepted",
        "queued": bool(enq.get("inserted")),
        "duplicate": not bool(enq.get("inserted")),
        "event_id": enq.get("event_id"),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
