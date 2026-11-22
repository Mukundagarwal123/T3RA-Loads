"""Tests for the webhook processing flow.

The repository and both rate engines are stubbed, so these exercise the
decision logic without a database or an outbound call.
"""

from __future__ import annotations

import pytest

from t3ra_unified.covered.services import shipment_processor as sp


@pytest.fixture
def fake_repo(monkeypatch):
    """Capture what the processor would have written."""

    calls = {"upsert": [], "overwrite": [], "rates_written": [], "finalized": [], "row": None}

    def overwrite(record, *, write_rates=False):
        calls["overwrite"].append(record)
        calls["rates_written"].append(write_rates)
        return 1

    monkeypatch.setattr(sp.repo, "upsert_final_record", lambda r: calls["upsert"].append(r))
    monkeypatch.setattr(sp.repo, "overwrite_preserving_rates", overwrite)
    monkeypatch.setattr(sp.repo, "fetch_row", lambda _: calls["row"])
    monkeypatch.setattr(sp.repo, "mark_finalized", lambda n: calls["finalized"].append(n) or 1)
    return calls


@pytest.fixture
def fake_rates(monkeypatch):
    """Record rate-engine invocations and return fixed prices."""
    calls = {"gs": 0, "dat": 0}

    def run_gs(equipment, pickup_date, stops):
        calls["gs"] += 1
        return 2000.0, 0.9

    def run_dat(stops, equipment):
        calls["dat"] += 1
        return 1900.0

    monkeypatch.setattr(sp, "run_gs", run_gs)
    monkeypatch.setattr(sp, "run_dat", run_dat)
    return calls


class TestBuildFinalRecord:
    def test_flattens_a_shipment_into_the_row_shape(self, shipment_payload, fake_rates):
        record = sp.build_final_record(shipment_payload, "Covered", force_skip_rates=True)

        assert record["shipment_num"] == "T3-100045"
        assert record["shipment_id"] == 115792084
        assert record["lane"] == "MCCLELLAN CA - LIVERMORE CA"
        assert record["equipment"] == "Van"
        assert record["transport_mode"] == "TL"
        assert record["customer"] == "DIAMOND PET FOODS"
        assert record["carrier"] == "SWIFT TRANSPORT LLC"
        assert record["carrier_am"] == "Jordan Lee"

    def test_line_haul_leaves_layover_out(self, shipment_payload, fake_rates):
        record = sp.build_final_record(shipment_payload, "Covered", force_skip_rates=True)
        assert record["carrier_freight_cost"] == 1600.0  # freight-flat only, no 100 layover
        assert record["customer_freight_cost"] == 2200.0  # freight-flat only, no 150 layover

    def test_total_cost_is_turvos_margin_total(self, shipment_payload, fake_rates):
        record = sp.build_final_record(shipment_payload, "covered")
        # Totals are Turvo's own margin block (shared with the completion phase),
        # not a sum of order totals.
        assert record["customer_total_cost"] == 2400.0  # margin.totalReceivableAmount
        assert record["carrier_total_cost"] == 1800.0  # margin.totalPayableAmount

    def test_force_skip_rates_calls_neither_engine(self, shipment_payload, fake_rates):
        record = sp.build_final_record(shipment_payload, "Covered", force_skip_rates=True)
        assert fake_rates == {"gs": 0, "dat": 0}
        assert record["GS_rate"] is None
        assert record["dat_rate"] is None

    def test_an_eligible_shipment_is_priced_by_both_engines(self, shipment_payload, fake_rates):
        record = sp.build_final_record(shipment_payload, "Covered", covered_date="04/08/2025")
        assert fake_rates == {"gs": 1, "dat": 1}
        assert record["GS_rate"] == 2000.0
        assert record["GS_confidence_score"] == 0.9
        assert record["dat_rate"] == 1900.0

    def test_an_ineligible_status_is_not_priced(self, shipment_payload, fake_rates):
        sp.build_final_record(shipment_payload, "Tendered")
        assert fake_rates == {"gs": 0, "dat": 0}

    def test_the_covered_date_becomes_the_pricing_date(self, shipment_payload, monkeypatch):
        """A load is priced as of the day it was covered, not the day it is processed."""
        seen = {}

        monkeypatch.setattr(sp, "run_gs", lambda eq, date, stops: (seen.update(date=date), (None, None))[1])
        monkeypatch.setattr(sp, "run_dat", lambda stops, eq: None)

        sp.build_final_record(shipment_payload, "Covered", covered_date="04/08/2025")
        assert seen["date"] == "2025-04-08"


class TestProcessShipmentEvent:
    """A shipment the table has never seen: classified by status, and priced."""

    def test_a_covered_shipment_is_upserted_with_rates(self, shipment_payload, fake_repo, fake_rates):
        record = sp.process_shipment_event(shipment_payload, "Covered")
        assert len(fake_repo["upsert"]) == 1
        assert record["GS_rate"] == 2000.0
        assert record["covered_date"] is not None

    def test_a_late_status_on_an_unknown_shipment_is_inserted_unpriced(
        self, shipment_payload, fake_repo, fake_rates
    ):
        """Documents existing behaviour: this path inserts but never prices.

        The processor asks for rates, but should_run_rates only admits
        status "Covered", so a late-status shipment the database has never
        seen is stored with no GS or DAT rate.
        """
        fake_repo["row"] = None
        sp.process_shipment_event(shipment_payload, "Completed")

        assert len(fake_repo["upsert"]) == 1
        assert fake_rates == {"gs": 0, "dat": 0}
        assert fake_repo["upsert"][0]["GS_rate"] is None

    def test_an_unhandled_status_on_an_unknown_shipment_writes_nothing(
        self, shipment_payload, fake_repo, fake_rates
    ):
        assert sp.process_shipment_event(shipment_payload, "Tendered") == {}
        assert fake_repo["upsert"] == []
        assert fake_repo["overwrite"] == []


class TestExistingShipmentIsMirrored:
    """A shipment already on the dashboard is overwritten with Turvo's state."""

    def _stored_row(self, shipment_payload, fake_rates):
        """The row a first Covered event would have written.

        Building it runs the stub engines, so the counters are reset -- a test
        asserting "not re-priced" means calls made after this point.
        """
        record = sp.build_final_record(shipment_payload, "Covered", covered_date="04/08/2025")
        fake_rates.update(gs=0, dat=0)
        return {
            **record,
            "gs_rate": record["GS_rate"],
            "dat_rate": record["dat_rate"],
            "gs_confidence_score": record["GS_confidence_score"],
            "covered_date": "04/08/2025",
        }

    def test_a_late_status_on_a_known_shipment_reuses_stored_rates(
        self, shipment_payload, fake_repo, fake_rates
    ):
        fake_repo["row"] = {
            "shipment_num": "T3-100045",
            "gs_rate": 2000.0,
            "dat_rate": 1900.0,
            "covered_date": "04/08/2025",
        }

        record = sp.process_shipment_event(shipment_payload, "Completed")

        assert fake_rates == {"gs": 0, "dat": 0}, "must not pay to price the same load twice"
        assert len(fake_repo["overwrite"]) == 1
        assert fake_repo["rates_written"] == [False], "the rate columns stay out of the statement"
        assert record["GS_rate"] == 2000.0
        # (2000 - 1600) / 2000: the line haul is freight-flat alone
        assert record["GS_margin"] == 20.0

    def test_stored_rates_of_zero_do_not_divide_by_zero(self, shipment_payload, fake_repo, fake_rates):
        fake_repo["row"] = {
            "shipment_num": "T3-100045",
            "gs_rate": 0.0,
            "dat_rate": None,
            "covered_date": "04/08/2025",
        }

        record = sp.process_shipment_event(shipment_payload, "Processing")
        assert record["GS_margin"] is None
        assert record["dat_margin"] is None

    def test_an_unchanged_shipment_is_still_overwritten(self, shipment_payload, fake_repo, fake_rates):
        """Turvo is the source of truth, so every update restates the row."""
        fake_repo["row"] = self._stored_row(shipment_payload, fake_rates)

        record = sp.process_shipment_event(shipment_payload, "Covered")

        assert len(fake_repo["overwrite"]) == 1
        assert record != {}
        assert fake_rates == {"gs": 0, "dat": 0}, "an already priced load must not be re-priced"

    def test_a_changed_shipment_is_overwritten_without_re_pricing(
        self, shipment_payload, fake_repo, fake_rates
    ):
        fake_repo["row"] = {**self._stored_row(shipment_payload, fake_rates), "carrier": "OLD CARRIER INC"}

        record = sp.process_shipment_event(shipment_payload, "Covered")

        assert len(fake_repo["overwrite"]) == 1
        assert fake_repo["overwrite"][0]["carrier"] == "SWIFT TRANSPORT LLC"
        assert fake_rates == {"gs": 0, "dat": 0}, "the load was already priced"
        assert record["GS_rate"] == 2000.0, "the stored rate is carried forward"
        assert record["covered_date"] == "04/08/2025", "the original cover date stands"

    def test_a_field_turvo_has_cleared_is_written_as_null(self, shipment_payload, fake_repo, fake_rates):
        """The dashboard must not keep showing a value Turvo no longer holds."""
        fake_repo["row"] = self._stored_row(shipment_payload, fake_rates)
        shipment_payload["details"]["carrierOrder"] = []

        sp.process_shipment_event(shipment_payload, "Covered")

        assert fake_repo["overwrite"][0]["carrier"] is None

    def test_a_status_outside_the_processable_set_still_overwrites(
        self, shipment_payload, fake_repo, fake_rates
    ):
        """A known load's cancellation is as much a fact as its cover was."""
        fake_repo["row"] = self._stored_row(shipment_payload, fake_rates)

        record = sp.process_shipment_event(shipment_payload, "Cancelled")

        assert len(fake_repo["overwrite"]) == 1
        assert fake_repo["overwrite"][0]["status"] == "Cancelled"
        assert record["GS_rate"] == 2000.0, "a cancelled load keeps the rate it was priced at"
        assert fake_rates == {"gs": 0, "dat": 0}

    def test_a_known_covered_shipment_that_was_never_priced_is_priced(
        self, shipment_payload, fake_repo, fake_rates
    ):
        """No covered_date means the engines never ran against this load."""
        fake_repo["row"] = {
            "shipment_num": "T3-100045",
            "gs_rate": None,
            "dat_rate": None,
            "covered_date": None,
        }

        record = sp.process_shipment_event(shipment_payload, "Covered")

        assert fake_rates == {"gs": 1, "dat": 1}
        assert fake_repo["rates_written"] == [True]
        assert record["GS_rate"] == 2000.0
        assert record["covered_date"] is not None

    def test_a_pricing_attempt_that_returned_nothing_is_not_retried(
        self, shipment_payload, fake_repo, fake_rates
    ):
        """covered_date records the attempt, so an empty result is not re-bought."""
        fake_repo["row"] = {
            "shipment_num": "T3-100045",
            "gs_rate": None,
            "dat_rate": None,
            "covered_date": "04/08/2025",
        }

        sp.process_shipment_event(shipment_payload, "Covered")

        assert fake_rates == {"gs": 0, "dat": 0}
        assert fake_repo["rates_written"] == [False]


class TestFinalization:
    def test_route_completion_stamps_the_load_as_finished(
        self, shipment_payload, fake_repo, fake_rates
    ):
        """Once finalized the refresh job stops re-pulling it from Turvo."""
        fake_repo["row"] = {
            "shipment_num": "T3-100045",
            "gs_rate": 2000.0,
            "dat_rate": 1900.0,
            "covered_date": "04/08/2025",
        }

        sp.process_shipment_event(shipment_payload, "Completed")
        assert fake_repo["finalized"] == ["T3-100045"]

    def test_a_completion_event_writes_even_when_nothing_changed(
        self, shipment_payload, fake_repo, fake_rates
    ):
        """Completion is the load's final refresh, so it always writes."""
        record = sp.build_final_record(shipment_payload, "Completed", force_skip_rates=True)
        fake_repo["row"] = {**record, "gs_rate": 2000.0, "dat_rate": 1900.0}

        sp.process_shipment_event(shipment_payload, "Completed")
        assert len(fake_repo["overwrite"]) == 1
        assert fake_repo["finalized"] == ["T3-100045"]

    def test_an_in_flight_status_does_not_finalize(self, shipment_payload, fake_repo, fake_rates):
        fake_repo["row"] = {"shipment_num": "T3-100045", "gs_rate": 2000.0, "dat_rate": 1900.0}

        sp.process_shipment_event(shipment_payload, "Processing")
        assert fake_repo["finalized"] == []

    def test_route_complete_on_a_known_shipment_finalizes(self, shipment_payload, fake_repo, fake_rates):
        """Reachable now that a known load is processed whatever its status."""
        fake_repo["row"] = {
            "shipment_num": "T3-100045",
            "gs_rate": 2000.0,
            "dat_rate": 1900.0,
            "covered_date": "04/08/2025",
        }

        sp.process_shipment_event(shipment_payload, "Route Complete")
        assert fake_repo["finalized"] == ["T3-100045"]


def _missing_fields(caplog) -> list[str]:
    """The ``missing_fields`` list off the warning, which caplog.text omits."""
    for record in caplog.records:
        fields = getattr(record, "missing_fields", None)
        if fields is not None:
            return list(fields)
    return []


class TestMissingFields:
    def test_a_covered_load_with_no_carrier_is_still_stored(
        self, shipment_payload, fake_repo, fake_rates, caplog
    ):
        """Turvo sometimes has no carrier at cover time; it populates later.

        The load must reach the dashboard anyway -- dropping it is how covered
        shipments went missing.
        """
        shipment_payload["details"]["carrierOrder"] = []

        with caplog.at_level("WARNING"):
            record = sp.process_shipment_event(shipment_payload, "Covered")

        assert len(fake_repo["upsert"]) == 1
        assert record["carrier"] is None
        assert "carrier" in _missing_fields(caplog)

    def test_the_missing_fields_are_named_in_the_log(self, shipment_payload, fake_repo, fake_rates, caplog):
        shipment_payload["details"]["equipment"] = []
        shipment_payload["details"].pop("equipmentType", None)

        with caplog.at_level("WARNING"):
            sp.process_shipment_event(shipment_payload, "Covered")

        assert "missing required fields" in caplog.text.lower()
        assert _missing_fields(caplog) == ["equipment"]
