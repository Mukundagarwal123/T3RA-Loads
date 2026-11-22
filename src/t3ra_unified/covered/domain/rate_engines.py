"""Adapters that turn raw rate-engine responses into a single number.

Both entry points swallow their exceptions on purpose: a pricing outage must
degrade the record (no rate) rather than fail the shipment.
"""

from __future__ import annotations

import functools
import json
import logging
from typing import Any

from t3ra_unified.clients import dat as dat_client
from t3ra_unified.clients import greenscreens as gs_client
from t3ra_unified.clients import turvo as turvo_client
from t3ra_unified.config import get_settings
from t3ra_unified.covered.domain.extractors import clean_place_text, to_float
from t3ra_unified.observability import traced_function

logger = logging.getLogger(__name__)

#: Keys Greenscreens has used for the confidence score across API versions.
_CONFIDENCE_KEYS = (
    "confidenceLevel",
    "confidenceScore",
    "confidence",
    "predictionConfidence",
    "modelConfidence",
    "score",
)


def extract_confidence(obj: Any) -> float | None:
    if not isinstance(obj, dict):
        return None
    for key in _CONFIDENCE_KEYS:
        value = to_float(obj.get(key))
        if value is not None:
            return value
    return None


def _rate_from_target_buy(obj: dict[str, Any]) -> float | None:
    """``targetBuyRate * distance``, rounded -- the total for the lane."""
    target_buy = obj.get("targetBuyRate") or obj.get("target_buy_rate") or obj.get("buyRate")
    distance = obj.get("distance") or obj.get("miles")
    target_buy_f = to_float(target_buy)
    distance_f = to_float(distance)
    if target_buy_f is None or distance_f is None:
        return None
    return round(target_buy_f * distance_f, 2)


def parse_gs_response(body: Any) -> tuple[float | None, float | None]:
    """``(rate, confidence)`` from a Greenscreens body.

    The endpoint has returned the rate both at the top level and nested under
    several list keys, so every known shape is tried before giving up.
    """
    if not isinstance(body, dict):
        logger.warning("GS non-dict response", extra={"response_preview": str(body)[:1200]})
        return None, None

    confidence = extract_confidence(body)

    top_level = _rate_from_target_buy(body)
    if top_level is not None:
        logger.info("GS parsed top-level", extra={"netw_rate": top_level, "confidence": confidence})
        return top_level, confidence

    candidates = [
        ("networkRates", body.get("networkRates")),
        ("rates", body.get("rates")),
        ("data.networkRates", (body.get("data") or {}).get("networkRates")),
        ("data.rates", (body.get("data") or {}).get("rates")),
        ("result.networkRates", (body.get("result") or {}).get("networkRates")),
    ]

    for source, value in candidates:
        if isinstance(value, list) and value:
            first = value[0] or {}
            confidence = extract_confidence(first) or confidence
            rate = _rate_from_target_buy(first)
            if rate is None:
                logger.warning("GS missing targetBuyRate/distance", extra={"source": source})
                return None, confidence
            logger.info("GS parsed list shape", extra={"source": source, "netw_rate": rate, "confidence": confidence})
            return rate, confidence

    logger.warning("GS no rates found", extra={"response_preview": str(body)[:2000]})
    return None, confidence


@functools.lru_cache(maxsize=4096)
def _location_zip(location_id: int) -> str | None:
    """The zip on a Turvo location record, preferring its main address."""
    body = turvo_client.fetch_location(location_id)
    addresses = (body.get("details") or body).get("address") or []
    live = [a for a in addresses if isinstance(a, dict) and not a.get("deleted")]
    live.sort(key=lambda a: (a.get("type") or {}).get("value") != "Main")
    for address in live:
        zip_code = clean_place_text(address.get("zip"))
        if zip_code:
            return zip_code
    return None


@traced_function()
def fill_missing_zips(stops: list[dict]) -> list[dict]:
    """Stops with each missing zip taken from the stop's Turvo location.

    Greenscreens finds a stop by its zip when there is one. Without it, it looks
    the city up by name and rejects Turvo's abbreviations ("RCH CUCAMONGA",
    "SANTA FE SPGS") and places it has no zip for ("Spanish Springs") with
    "Zip not found". A failed lookup leaves the stop as it was.
    """
    filled: list[dict] = []
    for stop in stops:
        location_id = stop.get("location_id")
        if stop.get("zip") or not location_id:
            filled.append(stop)
            continue
        try:
            zip_code = _location_zip(int(location_id))
        except Exception:
            logger.exception("Turvo location zip lookup failed", extra={"location_id": location_id})
            zip_code = None
        filled.append({**stop, "zip": zip_code} if zip_code else stop)
    return filled


@traced_function()
def run_gs(
    equipment: str | None,
    pickup_date: str | None,
    stops: list[dict],
) -> tuple[float | None, float | None]:
    """``(rate, confidence)``; ``(None, None)`` when disabled, unusable, or on failure."""
    if not get_settings().gs_enabled or not equipment or not pickup_date:
        return None, None

    try:
        payload, body = gs_client.get_rates_for_lane(equipment, pickup_date, stops)
        logger.debug("GS payload", extra={"payload": json.dumps(payload, default=str)})
        return parse_gs_response(body)
    except Exception:
        logger.exception("GS failed (continuing)")
        return None, None


@traced_function()
def run_dat(stops: list[dict], equipment: str | None) -> float | None:
    """Total trip rate; None when disabled or on any failure."""
    if not get_settings().dat_enabled:
        return None

    try:
        return dat_client.get_total_rate_two_stop(stops, equipment)
    except Exception:
        logger.exception("DAT failed (continuing)")
        return None
