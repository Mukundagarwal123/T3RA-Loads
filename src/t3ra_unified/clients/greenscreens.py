"""Greenscreens network-rate client."""

from __future__ import annotations

import logging
from typing import Any

from t3ra_unified.clients.http import TokenCache, request_with_retry
from t3ra_unified.config import get_settings
from t3ra_unified.observability import traced_function

logger = logging.getLogger(__name__)

_token_cache = TokenCache()

#: Equipment values the prediction endpoint accepts. The enum is exact --
#: "FLAT_BED" comes back as a 400 parse_error, only "FLATBED" is priced.
SUPPORTED_TRANSPORT_TYPES = frozenset({"VAN", "REEFER", "FLATBED"})


def _fetch_token() -> tuple[str, float]:
    settings = get_settings()
    response = request_with_retry(
        "POST",
        settings.gs_auth_url,
        timeout=settings.gs_timeout_seconds,
        # One attempt only: a retried credential exchange burns whatever
        # failed-login budget the upstream enforces. Same rule as Turvo.
        max_attempts=1,
        headers={
            "accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "client_credentials",
            "client_id": settings.gs_client_id,
            "client_secret": settings.gs_client_secret.get_secret_value(),
        },
    )
    data = response.json()
    logger.info("Greenscreens token refreshed")
    return data["access_token"], float(data.get("expires_in", 3600))


@traced_function()
def get_access_token() -> str:
    return _token_cache.get(_fetch_token)


def _normalize_pickup_datetime(pickup_date: str) -> str:
    return pickup_date if "T" in pickup_date else f"{pickup_date}T00:00:00Z"


def build_payload(equipment: str, pickup_date: str, stops: list[dict]) -> dict[str, Any] | None:
    """Request body for a lane, or None when the equipment is unsupported.

    Stop ``order`` is renumbered 1..N because the endpoint rejects duplicates.
    """
    transport_type = str(equipment).strip().upper()
    if transport_type not in SUPPORTED_TRANSPORT_TYPES:
        return None

    return {
        "pickupDateTime": _normalize_pickup_datetime(pickup_date),
        "transportType": transport_type,
        "stops": [
            {
                "order": index,
                "city": stop.get("city"),
                "state": stop.get("state"),
                "zip": stop.get("zip"),
            }
            for index, stop in enumerate(stops, start=1)
        ],
        "currency": "USD",
    }


@traced_function()
def get_rates_for_lane(
    equipment: str,
    pickup_date: str,
    stops: list[dict],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """``(payload_sent, response_json)``, or ``(None, None)`` for unsupported equipment.

    The payload is returned alongside the response so failures can be traced
    back to exactly what was asked for.
    """
    settings = get_settings()
    payload = build_payload(equipment, pickup_date, stops)
    if payload is None:
        logger.info("Greenscreens skipped: unsupported equipment", extra={"equipment": equipment})
        return None, None

    response = request_with_retry(
        "POST",
        settings.gs_prediction_url,
        timeout=settings.gs_timeout_seconds,
        headers={
            "Authorization": f"Bearer {get_access_token()}",
            "Content-Type": "application/json",
            "accept": "application/json",
        },
        json=payload,
    )

    text = response.text or ""
    try:
        body = response.json() if text.strip() else {"raw": text}
    except ValueError:
        body = {"raw": text}

    return payload, body
