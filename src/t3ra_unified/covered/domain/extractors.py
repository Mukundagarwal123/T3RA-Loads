"""Pure functions that turn a Turvo shipment payload into flat fields.

This is the single source of truth for lane strings, equipment normalization,
cost extraction and margin math. It replaces the two divergent copies that
previously lived in ``shipment_extractors.py`` and ``update_covered_loads.py``.

Where the two copies genuinely disagreed, the difference is preserved as an
explicit argument rather than silently resolved -- see ``strict_active`` on
:func:`extract_stops` and ``rate_margin_basis`` on :func:`compute_margins`.

Everything here is I/O-free and therefore directly unit-testable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from t3ra_unified.covered.domain.equipment import normalize_equipment as normalize_equipment

LUMPER_CARRIER_NAME = "Lumper Advance"

RateMarginBasis = Literal["freight", "total"]

#: A UTF-8 non-breaking space read back as Latin-1.
_MISDECODED_NBSP = "\u00c2\u00a0"


def _is_positive_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value > 0


def to_float(value: Any) -> float | None:
    """Coerce Decimal/str/None to float, returning None when it cannot be done."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_timestamp(line_item: dict) -> datetime:
    """Timestamp of a cost line item, as an aware UTC datetime.

    Unparseable or missing timestamps sort first (``datetime.min``) so a line
    item with a real timestamp always wins the "latest" comparison.
    """
    raw = line_item.get("lastUpdatedOn") or line_item.get("date")
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def normalize_cost_code(value: Any) -> str:
    """Fold the many dash/spacing variants of a cost code into one form.

    ``"Freight - Flat"``, ``"Freight  – Flat"`` and ``"freight-flat"`` all
    become ``"freight-flat"``.
    """
    if not value:
        return ""
    text = str(value).strip().lower()
    text = text.replace("–", "-").replace("—", "-")
    text = " ".join(text.split())
    return text.replace(" - ", "-").replace("- ", "-").replace(" -", "-")


def format_mmddyyyy(iso_z: str | None) -> str | None:
    """``2025-04-08T00:00:00Z`` -> ``04/08/2025``; None when unparseable.

    Accepts fractional seconds and offsets (``2025-04-08T00:00:00.000Z``): this
    used to demand the exact ``%Y-%m-%dT%H:%M:%SZ`` shape and returned None for
    any timestamp with milliseconds. Parsing is shared with the completion phase.
    """
    from t3ra_unified.completion.extractors import parse_date

    parsed = parse_date(iso_z)
    return parsed.strftime("%m/%d/%Y") if parsed else None


def mmddyyyy_to_iso_date(date_str: str | None) -> str | None:
    """``04/08/2025`` -> ``2025-04-08``; None when unparseable."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%m/%d/%Y").strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def filter_carrier_orders(orders: list[dict]) -> list[dict]:
    """Drop the lumper-advance pseudo-carrier, unless it is all we have."""
    non_lumper = [o for o in orders if (o.get("carrier") or {}).get("name") != LUMPER_CARRIER_NAME]
    return non_lumper or orders


def extract_transport_mode(details: dict[str, Any]) -> str | None:
    mode = ((details.get("transportation") or {}).get("mode") or {}).get("value")
    if mode:
        return str(mode).strip()

    mode_info = details.get("modeInfo") or []
    if mode_info:
        nested = ((mode_info[0] or {}).get("mode") or {}).get("value")
        if nested:
            return str(nested).strip()

    return None


def clean_place_text(value: Any) -> str | None:
    """A city, state or zip with stray characters and runs of spaces removed.

    Turvo sometimes stores a non-breaking space that was decoded with the wrong
    encoding, so ``GA`` arrives as ``"GA\u00c2\u00a0 "`` and Greenscreens rejects
    it as an invalid state. Empty values come back as None.
    """
    if value is None:
        return None
    # str.split() already treats a bare non-breaking space as whitespace.
    text = " ".join(str(value).replace(_MISDECODED_NBSP, " ").split())
    return text or None


def extract_stops(shipment: dict[str, Any], *, strict_active: bool = False) -> list[dict]:
    """Ordered stop list from a shipment payload.

    ``strict_active`` selects which stops count as active, and the two callers
    genuinely differ:

    * ``False`` (webhook path) keeps every stop except those explicitly
      ``deleted: true`` -- a stop with no ``deleted`` key is kept.
    * ``True`` (refresh job) keeps only stops explicitly marked
      ``deleted: false``.

    Changing either caller's choice changes the lane strings written to the
    database, so it is a deliberate argument rather than a default.
    """
    details = shipment.get("details") or shipment
    raw_stops = details.get("globalRoute") or details.get("route") or []

    stops: list[dict] = []
    for index, stop in enumerate(raw_stops, start=1):
        if strict_active:
            if stop.get("deleted") is not False:
                continue
        elif stop.get("deleted") is True:
            continue

        address = stop.get("address") or stop.get("location") or {}
        stops.append(
            {
                "sequence": stop.get("sequence", index),
                "city": clean_place_text(address.get("city")),
                "state": clean_place_text(address.get("state")),
                "zip": clean_place_text(address.get("zip") or address.get("zipCode") or address.get("postalCode")),
                # Turvo often leaves the zip off a stop's address but keeps it on
                # the location record; the id lets the pricing path look it up.
                "location_id": (stop.get("location") or {}).get("id"),
            }
        )

    stops.sort(key=lambda s: s.get("sequence") or 0)
    return stops


def _stop_label(stop: dict) -> str:
    city = (stop.get("city") or "").strip()
    state = (stop.get("state") or "").strip()
    return f"{city} {state}".strip()


def build_lane_string(stops: list[dict]) -> str:
    """Full lane, with intermediate stops in parentheses.

    ``MCCLELLAN CA - LIVERMORE CA (STOCKTON CA)``
    """
    labels = [label for label in (_stop_label(s) for s in stops or []) if label]
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} - {labels[1]}"
    middle = ", ".join(labels[1:-1])
    return f"{labels[0]} - {labels[-1]} ({middle})"


def build_lane_raw(stops: list[dict]) -> str:
    """First and last stop only: ``MCCLELLAN CA - LIVERMORE CA``."""
    if not stops:
        return ""
    origin = _stop_label(stops[0])
    destination = _stop_label(stops[-1])
    if not origin:
        return destination
    if not destination or origin == destination:
        return origin
    return f"{origin} - {destination}"


def extract_equipment(details: dict[str, Any]) -> str | None:
    for equipment in details.get("equipment") or []:
        if equipment.get("deleted") is False:
            return (equipment.get("type") or {}).get("value")
    return (details.get("equipmentType") or {}).get("value")


# ``normalize_equipment`` is imported at the top of this module and re-exported
# here so its existing callers keep working. The alias table itself lives in
# domain/equipment.py, beside the GS/DAT eligibility sets it has to agree with.


def normalize_status(raw_status: Any, fallback: str = "Covered") -> str:
    """Always yield a plain string, whatever shape Turvo sends.

    Historic rows contain dict-like strings because this was not applied
    consistently; the refresh job depends on it.
    """
    if raw_status is None:
        return fallback

    if isinstance(raw_status, str):
        return raw_status.strip() or fallback

    if isinstance(raw_status, dict):
        for key in ("value", "name", "displayValue", "label"):
            value = raw_status.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        code = raw_status.get("code")
        if isinstance(code, dict):
            value = code.get("value") or code.get("name")
            if isinstance(value, str) and value.strip():
                return value.strip()

    return str(raw_status).strip() or fallback


def extract_latest_freight(orders: list[dict], label: str) -> dict | None:
    """Latest ``freight-flat`` charge, as ``{"side": label, "amount": ...}``.

    This is the line haul alone. Accessorials such as layover and detention are
    not added in: they reach the dashboard only through Turvo's order total.
    """
    latest_freight: dict | None = None
    latest_freight_ts = datetime.min.replace(tzinfo=timezone.utc)

    for order in orders or []:
        costs = order.get("costs") or {}
        for line_item in costs.get("lineItem") or costs.get("lineItems") or []:
            if line_item.get("deleted") is not False:
                continue

            code = normalize_cost_code((line_item.get("code") or {}).get("value", ""))
            if code != "freight-flat":
                continue

            timestamp = parse_timestamp(line_item)
            if timestamp >= latest_freight_ts:
                latest_freight_ts = timestamp
                latest_freight = {"side": label, "amount": line_item.get("amount")}

    return latest_freight


def extract_customer_name(customer_orders: list[dict]) -> str | None:
    if not customer_orders:
        return None
    return (customer_orders[0].get("customer") or {}).get("name")


def extract_order_total(orders: list[dict]) -> float | None:
    """One side's total cost: Turvo's own ``costs.totalAmount``, summed over the
    side's live orders.

    Taken as Turvo computes it -- freight, every accessorial (lumper, fuel,
    detention, ...) and tax -- rather than rebuilt from line items, so the
    dashboard's total matches the shipment in Turvo. A deleted order, or one
    with no cost block yet, contributes nothing. None when no live order
    carries a positive total, so an absent side stays None: a stored total must
    not be overwritten with a zero.
    """
    total = 0.0
    found = False
    for order in orders or []:
        if order.get("deleted") is True:
            continue
        costs = order.get("costs") or {}
        if costs.get("deleted") is True:
            continue
        amount = to_float(costs.get("totalAmount"))
        if amount is None:
            continue
        total += amount
        found = True
    return total if found and total > 0 else None


def extract_carrier_identity(carrier_orders: list[dict]) -> tuple[str | None, str | None]:
    """``(carrier_name, carrier_account_manager)`` from the first live order.

    Falls back to the first order of any state when none is explicitly live.
    """
    for order in carrier_orders or []:
        if order.get("deleted") is False:
            carrier = order.get("carrier") or {}
            name = carrier.get("name")
            if name:
                return name, (carrier.get("owner") or {}).get("name")

    if carrier_orders:
        carrier = carrier_orders[0].get("carrier") or {}
        return carrier.get("name"), (carrier.get("owner") or {}).get("name")

    return None, None


def extract_carrier_notes(carrier_orders: list[dict]) -> str | None:
    """The internal note on the carrier costs (Turvo's "Notes (internal)").

    Turvo keeps it at ``carrierOrder[].costs.notes``. Every non-deleted order
    contributes; distinct notes are joined with `` | `` in order. None when no
    order carries one.
    """
    notes: list[str] = []
    for order in carrier_orders or []:
        if order.get("deleted") is True:
            continue
        note = str((order.get("costs") or {}).get("notes") or "").strip()
        if note and note not in notes:
            notes.append(note)
    return " | ".join(notes) or None


def compute_margins(
    customer_freight_cost: Any,
    carrier_freight_cost: Any,
    total_customer_cost: Any,
    total_carrier_cost: Any,
    gs_rate: Any,
    dat_rate: Any,
    *,
    rate_margin_basis: RateMarginBasis = "freight",
) -> tuple[float | None, float | None, float | None, float | None]:
    """Return ``(lh_margin, gs_margin, dat_margin, t_margin)`` as percentages.

    ``rate_margin_basis`` picks what the GS and DAT margins are measured
    against, and the two historic callers disagreed:

    * ``"freight"`` (webhook path) compares the predicted rate to the carrier
      *freight* cost.
    * ``"total"`` (refresh job) compares it to the carrier *total* cost.

    The same shipment yields different gs_margin/dat_margin under each, so the
    choice stays explicit until the business picks one.
    """
    lh_margin = None
    if _is_positive_number(customer_freight_cost) and _is_positive_number(carrier_freight_cost):
        lh_margin = round(((customer_freight_cost - carrier_freight_cost) / customer_freight_cost) * 100, 2)

    t_margin = None
    if _is_positive_number(total_customer_cost) and _is_positive_number(total_carrier_cost):
        t_margin = round(((total_customer_cost - total_carrier_cost) / total_customer_cost) * 100, 2)

    carrier_basis = carrier_freight_cost if rate_margin_basis == "freight" else total_carrier_cost

    gs_margin = None
    if _is_positive_number(gs_rate) and _is_positive_number(carrier_basis):
        gs_margin = round(((gs_rate - carrier_basis) / gs_rate) * 100, 2)

    dat_margin = None
    if _is_positive_number(dat_rate) and _is_positive_number(carrier_basis):
        dat_margin = round(((dat_rate - carrier_basis) / dat_rate) * 100, 2)

    return lh_margin, gs_margin, dat_margin, t_margin
