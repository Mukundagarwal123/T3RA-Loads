"""What actually changed between the stored row and a freshly built one.

Turvo's shipment-update webhook is generic: every edit to a shipment arrives as
the same event, and most of them touch nothing this dashboard shows. Comparing
the raw payloads is useless -- they carry per-line ``lastUpdatedOn`` stamps that
differ on every delivery -- so the comparison happens on the flat record the
extractors produce, which is exactly the set of facts the row holds.

The worker and the refresh job both use this to skip writes that would change
nothing, which is what keeps a covered load from being re-pulled and rewritten
on every unrelated edit.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

#: Fields worth reacting to. A change to anything outside this set is noise as
#: far as the dashboard is concerned. Rate columns are absent on purpose: they
#: are owned by the pricing path, never by a diff.
MATERIAL_FIELDS: tuple[str, ...] = (
    "shipment_id",
    "lane",
    "lane_raw",
    "pickup_date",
    "delivery_date",
    "equipment",
    "transport_mode",
    "customer",
    "carrier",
    "carrier_am",
    "carrier_id",
    "carrier_notes",
    "status",
    "customer_freight_cost",
    "customer_total_cost",
    "carrier_freight_cost",
    "carrier_total_cost",
)

#: Money columns compared to the cent; float round-trips through Postgres are
#: not bit-exact and must not read as a change.
_NUMERIC_TOLERANCE = 0.005


def _comparable(value: Any) -> Any:
    """Normalize a value so equal facts compare equal across DB round-trips."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip()
    return text or None


def values_differ(old: Any, new: Any) -> bool:
    """True when two field values are meaningfully different."""
    left = _comparable(old)
    right = _comparable(new)

    if isinstance(left, float) and isinstance(right, float):
        return abs(left - right) >= _NUMERIC_TOLERANCE

    return bool(left != right)


def diff_records(
    old_record: dict[str, Any] | None,
    new_record: dict[str, Any],
    fields: Iterable[str] = MATERIAL_FIELDS,
) -> dict[str, tuple[Any, Any]]:
    """``{field: (old, new)}`` for every material field that moved.

    A missing ``old_record`` means the shipment is new, so every field the new
    record actually carries counts as a change. A new value of None never
    overwrites a stored one -- the SQL COALESCEs it away -- so it is not
    reported as a difference either.
    """
    if old_record is None:
        return {
            field: (None, new_record[field])
            for field in fields
            if field in new_record and _comparable(new_record.get(field)) is not None
        }

    changes: dict[str, tuple[Any, Any]] = {}
    for field in fields:
        if field not in new_record:
            continue
        new_value = new_record.get(field)
        if _comparable(new_value) is None:
            continue
        old_value = old_record.get(field)
        if values_differ(old_value, new_value):
            changes[field] = (old_value, new_value)
    return changes


def has_material_change(
    old_record: dict[str, Any] | None,
    new_record: dict[str, Any],
    fields: Iterable[str] = MATERIAL_FIELDS,
) -> bool:
    """True when writing ``new_record`` would change something worth storing."""
    return bool(diff_records(old_record, new_record, fields))
