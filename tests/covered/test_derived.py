"""Derived columns follow their inputs, including inputs that arrive late."""

from __future__ import annotations

import pytest

from t3ra_unified.covered.domain.derived import DEPENDENCIES, INPUT_COLUMNS, recompute_derived


class TestRecomputeDerived:
    def test_no_carrier_cost_yields_no_margins(self):
        """The bug this exists for: 0 carrier pay once read as a 100% margin."""
        record = {"carrier_freight_cost": None, "customer_freight_cost": 600.0, "GS_rate": 9291.53}
        recompute_derived(record, {}, coalesce=True)
        assert record["GS_margin"] is None
        assert record["LH_margin"] is None

    def test_late_carrier_cost_revives_margins(self):
        """A cost entered after the row was written brings its margins with it."""
        existing = {
            "customer_freight_cost": 600.0,
            "customer_total_cost": 689.0,
            "carrier_freight_cost": None,
            "carrier_total_cost": None,
            "gs_rate": 1000.0,
            "dat_rate": None,
        }
        record = {"carrier_freight_cost": 800.0, "carrier_total_cost": 800.0}
        recompute_derived(record, existing, coalesce=True)

        # Computed against the *stored* rate, which the payload never carries.
        assert record["GS_margin"] == pytest.approx(20.0)
        assert record["dat_margin"] is None

    def test_disappearing_cost_clears_its_margins(self):
        """A margin must not outlive the cost it was computed from."""
        existing = {
            "customer_freight_cost": 600.0,
            "customer_total_cost": 689.0,
            "carrier_freight_cost": 800.0,
            "carrier_total_cost": 800.0,
            "gs_rate": 1000.0,
            "dat_rate": None,
        }
        record = dict.fromkeys(INPUT_COLUMNS)
        recompute_derived(record, existing, coalesce=False)
        assert record["GS_margin"] is None
        assert record["LH_margin"] is None

    def test_coalesce_keeps_stored_inputs(self):
        """A partial payload computes against what the row already holds."""
        existing = {
            "customer_freight_cost": 1000.0,
            "customer_total_cost": 1000.0,
            "carrier_freight_cost": 750.0,
            "carrier_total_cost": 750.0,
            "gs_rate": None,
            "dat_rate": None,
        }
        recompute_derived(record := {"carrier": "Someone"}, existing, coalesce=True)
        assert record["LH_margin"] == pytest.approx(25.0)

    def test_rates_always_come_from_storage(self):
        """Shipment updates never carry rates; a load is priced once."""
        existing = {
            "customer_freight_cost": 600.0,
            "customer_total_cost": 600.0,
            "carrier_freight_cost": 500.0,
            "carrier_total_cost": 500.0,
            "gs_rate": 1000.0,
            "dat_rate": None,
        }
        record = dict.fromkeys(INPUT_COLUMNS)
        record["carrier_freight_cost"] = 500.0
        recompute_derived(record, existing, coalesce=False)
        assert record["GS_margin"] == pytest.approx(50.0)


def test_dependency_map_covers_every_derived_column():
    """The map is documentation only if nothing checks it."""
    record: dict = {}
    recompute_derived(record, {}, coalesce=True)
    from t3ra_unified.covered.domain.derived import record_key

    assert {record_key(column) for column in DEPENDENCIES} == set(record)
