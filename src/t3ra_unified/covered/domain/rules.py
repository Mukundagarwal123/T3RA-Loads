"""Eligibility rules that decide whether a shipment is worth pricing.

Greenscreens and DAT calls cost money and latency, so a shipment must clear
every gate here before either engine runs. The master on/off switches live in
configuration (``GS_ENABLED`` / ``DAT_ENABLED``); the business gates below are
code because changing them changes what the numbers mean.
"""

from __future__ import annotations

from t3ra_unified.covered.domain.equipment import (
    DAT_ELIGIBLE_EQUIPMENT,
    GS_ELIGIBLE_EQUIPMENT,
    dat_allows_equipment,
    gs_allows_equipment,
)

__all__ = [
    "MIN_STOPS_FOR_DAT",
    "MIN_STOPS_FOR_GS",
    "RULES",
    "dat_allows_equipment",
    "gs_allows_equipment",
    "should_run_rates",
]

#: Each engine needs at least an origin and a destination.
MIN_STOPS_FOR_GS = 2
MIN_STOPS_FOR_DAT = 2

RULES: dict[str, object] = {
    # Only price shipments in these statuses.
    "run_rates_only_when_status_in": {"Covered"},
    # Only price these transport modes.
    "run_rates_only_when_mode_in": {"TL"},
    # LTL shipments are never priced by these engines.
    "skip_if_ltlShipment_true": True,
    # Equipment each engine will accept. The sets themselves live in
    # domain/equipment.py, next to the normalizer that produces the values --
    # keeping them apart is how "Flat Bed" used to fall through both gates.
    "gs_allowed_equipment": GS_ELIGIBLE_EQUIPMENT,
    "dat_allowed_equipment": DAT_ELIGIBLE_EQUIPMENT,
    # Data requirements.
    "require_pickup_date_for_gs": True,
    "require_stops_for_gs": True,
    "min_stops_for_gs": MIN_STOPS_FOR_GS,
    "min_stops_for_dat": MIN_STOPS_FOR_DAT,
}


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def _norm_set(values: set[str]) -> set[str]:
    return {v.strip().lower() for v in (values or set())}


def should_run_rates(
    status_value: str | None,
    transport_mode: str | None,
    ltl_flag: bool | None,
    equipment: str | None,
    stops_count: int,
    pickup_date_gs: str | None,
) -> tuple[bool, str]:
    """``(eligible, reason)`` -- the reason is logged either way."""
    if _norm(status_value) not in _norm_set(RULES["run_rates_only_when_status_in"]):  # type: ignore[arg-type]
        return False, f"status '{status_value}' not eligible"

    if _norm(transport_mode) not in _norm_set(RULES["run_rates_only_when_mode_in"]):  # type: ignore[arg-type]
        return False, f"mode '{transport_mode}' not eligible"

    if RULES["skip_if_ltlShipment_true"] and ltl_flag is True:
        return False, "ltlShipment == True"

    if not (gs_allows_equipment(equipment) or dat_allows_equipment(equipment)):
        return False, f"equipment '{equipment}' not allowed"

    min_stops = min(MIN_STOPS_FOR_GS, MIN_STOPS_FOR_DAT)
    if stops_count < min_stops:
        return False, f"need >= {min_stops} stops"

    if RULES["require_pickup_date_for_gs"] and not pickup_date_gs:
        return False, "pickup date missing (GS requirement)"

    return True, "eligible"
