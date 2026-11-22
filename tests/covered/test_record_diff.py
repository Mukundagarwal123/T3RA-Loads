"""Tests for deciding whether a rebuilt record is worth writing.

Turvo's shipment-update webhook fires for every edit, so this is the gate that
keeps an unrelated change from re-pricing and rewriting a covered load.
"""

from __future__ import annotations

import pytest

from t3ra_unified.covered.domain.record_diff import diff_records, has_material_change, values_differ

STORED = {
    "shipment_num": "T3-100045",
    "shipment_id": 115792084,
    "lane": "MCCLELLAN CA - LIVERMORE CA",
    "equipment": "Van",
    "carrier": "SWIFT TRANSPORT LLC",
    "status": "Covered",
    "carrier_freight_cost": 1700.0,
    "customer_freight_cost": 2350.0,
    "gs_rate": 2000.0,
}


def test_an_identical_record_is_no_change():
    assert diff_records(STORED, dict(STORED)) == {}
    assert not has_material_change(STORED, dict(STORED))


def test_a_changed_cost_is_reported():
    changes = diff_records(STORED, {**STORED, "carrier_freight_cost": 1750.0})
    assert changes == {"carrier_freight_cost": (1700.0, 1750.0)}


def test_a_changed_status_is_reported():
    assert "status" in diff_records(STORED, {**STORED, "status": "Completed"})


def test_a_new_shipment_counts_every_field_it_carries():
    changes = diff_records(None, {"lane": "A - B", "carrier": None, "status": "Covered"})
    assert set(changes) == {"lane", "status"}


def test_a_carrier_arriving_late_is_a_change():
    """The covered row was written without a carrier; Turvo fills it in later."""
    stored = {**STORED, "carrier": None}
    assert diff_records(stored, STORED) == {"carrier": (None, "SWIFT TRANSPORT LLC")}


def test_a_new_carrier_note_is_a_change():
    changes = diff_records(STORED, {**STORED, "carrier_notes": "abcdefghijk"})
    assert changes == {"carrier_notes": (None, "abcdefghijk")}


def test_user_notes_are_never_a_material_change():
    """The dashboard owns ``notes``; a Turvo-driven write must not react to it."""
    assert diff_records(STORED, {**STORED, "notes": "typed by a user"}) == {}


def test_a_field_going_missing_is_not_a_change():
    """A None never overwrites a stored value -- the SQL COALESCEs it away."""
    assert diff_records(STORED, {**STORED, "carrier": None}) == {}


def test_a_rate_column_is_never_a_material_change():
    """Rates belong to the pricing path; a diff must not react to them."""
    assert diff_records(STORED, {**STORED, "gs_rate": 2500.0}) == {}


def test_a_float_round_trip_is_not_a_change():
    assert not has_material_change(STORED, {**STORED, "carrier_freight_cost": 1700.000001})


def test_a_cent_is_a_change():
    assert has_material_change(STORED, {**STORED, "carrier_freight_cost": 1700.01})


@pytest.mark.parametrize(
    ("old", "new"),
    [("Van", " Van "), (1700, 1700.0), ("", None), (None, "   ")],
)
def test_values_that_only_look_different(old, new):
    assert not values_differ(old, new)


def test_only_the_named_fields_are_compared():
    assert diff_records(STORED, {**STORED, "lane": "X - Y"}, fields=("carrier",)) == {}
