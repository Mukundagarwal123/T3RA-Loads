"""Tests for the rate-eligibility gates."""

from __future__ import annotations

import pytest

from t3ra_unified.covered.domain.rules import should_run_rates

ELIGIBLE = {
    "status_value": "Covered",
    "transport_mode": "TL",
    "ltl_flag": False,
    "equipment": "Van",
    "stops_count": 2,
    "pickup_date_gs": "2025-04-08",
}


def test_a_fully_eligible_shipment_passes():
    assert should_run_rates(**ELIGIBLE) == (True, "eligible")


@pytest.mark.parametrize("status", ["Processing", "Completed", "Tendered", None, ""])
def test_only_covered_shipments_are_priced(status):
    eligible, reason = should_run_rates(**{**ELIGIBLE, "status_value": status})
    assert not eligible
    assert "not eligible" in reason


@pytest.mark.parametrize("mode", ["LTL", "Drayage", None])
def test_only_truckload_is_priced(mode):
    eligible, _ = should_run_rates(**{**ELIGIBLE, "transport_mode": mode})
    assert not eligible


def test_ltl_flagged_shipments_are_skipped():
    eligible, reason = should_run_rates(**{**ELIGIBLE, "ltl_flag": True})
    assert not eligible
    assert reason == "ltlShipment == True"


@pytest.mark.parametrize("equipment", ["Power Only", "Step Deck", "Box Truck", None])
def test_unsupported_equipment_is_skipped(equipment):
    eligible, reason = should_run_rates(**{**ELIGIBLE, "equipment": equipment})
    assert not eligible
    assert "not allowed" in reason


@pytest.mark.parametrize("equipment", ["Flatbed", "Flat Bed", "Bed Pad"])
def test_flatbed_is_eligible(equipment):
    """Both engines price a flatbed, so every spelling of it has to pass.

    These spellings all used to fail the gate: the normalizer left them alone
    and neither engine's set contained them.
    """
    assert should_run_rates(**{**ELIGIBLE, "equipment": equipment})[0]


def test_status_and_equipment_matching_is_case_insensitive():
    assert should_run_rates(**{**ELIGIBLE, "status_value": "covered", "equipment": "VAN"})[0]


def test_a_single_stop_cannot_be_priced():
    eligible, reason = should_run_rates(**{**ELIGIBLE, "stops_count": 1})
    assert not eligible
    assert "stops" in reason


def test_greenscreens_requires_a_pickup_date():
    eligible, reason = should_run_rates(**{**ELIGIBLE, "pickup_date_gs": None})
    assert not eligible
    assert "pickup date missing" in reason
