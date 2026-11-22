"""DAT linehaul-rate client.

DAT is only consulted for straight two-stop lanes; the total returned is
``perTrip.rateUsd + averageFuelSurchargePerTripUsd``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from t3ra_unified.clients.http import request_with_retry
from t3ra_unified.config import get_settings
from t3ra_unified.observability import traced_function

logger = logging.getLogger(__name__)

#: City names DAT spells differently from Turvo. Keyed by "CITY|STATE".
CITY_ALIASES = {
    "MARINA DEL RAY|CA": "Marina Del Rey",
    "SPANISH SPRINGS": "Sparks",
}

#: Particles that stay lowercase when title-casing a city name.
_LOWERCASE_PARTICLES = frozenset({"de", "del", "la", "las", "los", "da", "do", "du", "von", "van", "of", "the"})


def _norm(value: str | None) -> str:
    return "" if value is None else str(value).strip()


def _title_city(value: str) -> str:
    words = " ".join(value.split()).lower().split(" ")
    return " ".join(w if w in _LOWERCASE_PARTICLES else w[:1].upper() + w[1:] for w in words)


def normalize_city(city: str | None, state: str | None) -> str:
    cleaned = re.sub(r"\s+", " ", _norm(city)).strip()
    alias_key = f"{cleaned.upper()}|{_norm(state).upper()}"
    if alias_key in CITY_ALIASES:
        return CITY_ALIASES[alias_key]
    return _title_city(cleaned)


def build_stop(stop: dict[str, Any]) -> dict[str, str]:
    return {
        "city": normalize_city(stop.get("city"), stop.get("state")),
        "state": _norm(stop.get("state")).upper(),
        "zip": _norm(stop.get("zip")),
    }


def map_equipment(equipment_value: str | None) -> str:
    """Fold free-text equipment onto DAT's enum. Unknown values become VAN."""
    if not equipment_value:
        return "VAN"
    text = equipment_value.strip().upper()
    if "REEFER" in text or "REF" in text:
        return "REEFER"
    if "FLAT" in text:
        return "FLATBED"
    return "VAN"


def _post(url: str, headers: dict[str, str], payload: Any) -> dict[str, Any]:
    settings = get_settings()
    response = request_with_retry(
        "POST",
        url,
        timeout=settings.dat_timeout_seconds,
        max_attempts=settings.dat_retry_count + 1,
        base_delay_seconds=settings.dat_retry_sleep_seconds,
        headers=headers,
        json=payload,
    )
    body: dict[str, Any] = response.json()
    return body


@traced_function()
def fetch_org_token() -> str:
    settings = get_settings()
    body = _post(
        settings.dat_org_token_url,
        {"Content-Type": "application/json"},
        {
            "username": settings.dat_org_username,
            "password": settings.dat_org_password.get_secret_value(),
        },
    )
    token = body.get("accessToken") or body.get("access_token")
    if not token:
        raise RuntimeError("DAT organization token missing from response")
    return str(token)


@traced_function()
def fetch_user_token(org_token: str) -> str:
    settings = get_settings()
    body = _post(
        settings.dat_user_token_url,
        {"Authorization": f"Bearer {org_token}", "Content-Type": "application/json"},
        {
            "username": settings.dat_user_username,
            "password": settings.dat_user_password.get_secret_value(),
        },
    )
    token = body.get("accessToken") or body.get("access_token")
    if not token:
        raise RuntimeError("DAT user token missing from response")
    return str(token)


def _lookup(
    user_token: str,
    origin: dict[str, str],
    destination: dict[str, str],
    equipment: str,
    *,
    include_zip: bool,
) -> dict[str, Any]:
    settings = get_settings()
    item: dict[str, Any] = {
        "origin": {"city": origin["city"], "stateOrProvince": origin["state"]},
        "destination": {"city": destination["city"], "stateOrProvince": destination["state"]},
        "rateType": "SPOT",
        "equipment": equipment,
        "targetEscalation": {"escalationType": "BEST_FIT"},
        "includeMyRate": False,
        "rateTimePeriod": {"rateTense": "CURRENT"},
    }

    if include_zip:
        if origin.get("zip"):
            item["origin"]["postalCode"] = origin["zip"]
        if destination.get("zip"):
            item["destination"]["postalCode"] = destination["zip"]

    return _post(
        settings.dat_rate_lookup_url,
        {
            "Authorization": f"Bearer {user_token}",
            "Content-Type": "application/json",
            "x-cache-response": "true",
        },
        [item],
    )


def extract_total_trip_usd(response_json: dict[str, Any]) -> float | None:
    """Linehaul plus fuel for the trip, or None when either is absent."""
    rate_responses = response_json.get("rateResponses") or []
    if not rate_responses:
        return None

    rate = ((rate_responses[0].get("response") or {}).get("rate") or {})
    per_trip = (rate.get("perTrip") or {}).get("rateUsd")
    fuel = rate.get("averageFuelSurchargePerTripUsd")

    if per_trip is None or fuel is None:
        return None

    try:
        return float(per_trip) + float(fuel)
    except (TypeError, ValueError):
        return None


@traced_function()
def get_total_rate_two_stop(
    stops: list[dict[str, Any]],
    equipment_type_raw: str | None,
) -> float | None:
    """Total trip rate for a two-stop lane, or None if unavailable.

    Never raises: a DAT outage must not stop a shipment from being persisted.
    The lookup is retried once without postal codes, because DAT rejects some
    ZIPs that Turvo happily supplies.
    """
    if not stops or len(stops) != 2:
        logger.info("DAT skipped: not a two-stop lane", extra={"stops_count": len(stops or [])})
        return None

    settings = get_settings()
    equipment = map_equipment(equipment_type_raw)
    origin = build_stop(stops[0])
    destination = build_stop(stops[-1])

    try:
        logger.info("DAT token flow start", extra={"dat_env": settings.dat_env})
        user_token = fetch_user_token(fetch_org_token())
    except Exception:
        logger.exception("DAT token fetch failed")
        return None

    for include_zip in (True, False):
        try:
            body = _lookup(user_token, origin, destination, equipment, include_zip=include_zip)
        except Exception:
            # A rejected ZIP raises here; the ZIP-less attempt must still run,
            # so this catch stays inside the loop.
            logger.exception("DAT lookup failed", extra={"include_zip": include_zip})
            continue

        total = extract_total_trip_usd(body)
        if total is not None:
            return total

    logger.info("DAT returned no total rate")
    return None
