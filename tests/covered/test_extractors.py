"""Tests for the shipment field extraction and margin maths."""

from __future__ import annotations

import pytest

from t3ra_unified.covered.domain import extractors as ex


class TestLaneStrings:
    def test_two_stop_lane_uses_a_simple_dash(self):
        stops = [{"city": "MCCLELLAN", "state": "CA"}, {"city": "LIVERMORE", "state": "CA"}]
        assert ex.build_lane_string(stops) == "MCCLELLAN CA - LIVERMORE CA"

    def test_intermediate_stops_go_in_parentheses(self):
        stops = [
            {"city": "MCCLELLAN", "state": "CA"},
            {"city": "STOCKTON", "state": "CA"},
            {"city": "LIVERMORE", "state": "CA"},
        ]
        assert ex.build_lane_string(stops) == "MCCLELLAN CA - LIVERMORE CA (STOCKTON CA)"

    def test_lane_raw_drops_the_middle(self):
        stops = [
            {"city": "MCCLELLAN", "state": "CA"},
            {"city": "STOCKTON", "state": "CA"},
            {"city": "LIVERMORE", "state": "CA"},
        ]
        assert ex.build_lane_raw(stops) == "MCCLELLAN CA - LIVERMORE CA"

    def test_round_trip_collapses_to_one_label(self):
        stops = [{"city": "RENO", "state": "NV"}, {"city": "RENO", "state": "NV"}]
        assert ex.build_lane_raw(stops) == "RENO NV"

    def test_missing_city_does_not_leave_a_dangling_dash(self):
        stops = [{"city": None, "state": None}, {"city": "LIVERMORE", "state": "CA"}]
        assert ex.build_lane_string(stops) == "LIVERMORE CA"
        assert ex.build_lane_raw(stops) == "LIVERMORE CA"

    def test_no_stops_is_empty(self):
        assert ex.build_lane_string([]) == ""
        assert ex.build_lane_raw([]) == ""


class TestExtractStops:
    def test_default_keeps_stops_without_a_deleted_flag(self):
        shipment = {"details": {"globalRoute": [{"sequence": 1, "address": {"city": "A", "state": "CA"}}]}}
        assert len(ex.extract_stops(shipment)) == 1

    def test_strict_mode_drops_stops_without_an_explicit_false(self):
        shipment = {"details": {"globalRoute": [{"sequence": 1, "address": {"city": "A", "state": "CA"}}]}}
        assert ex.extract_stops(shipment, strict_active=True) == []

    def test_deleted_stops_are_always_dropped(self, shipment_payload):
        stops = ex.extract_stops(shipment_payload)
        assert [s["city"] for s in stops] == ["MCCLELLAN", "LIVERMORE"]

    def test_stops_are_returned_in_sequence_order(self):
        shipment = {
            "details": {
                "globalRoute": [
                    {"sequence": 2, "deleted": False, "address": {"city": "B", "state": "CA"}},
                    {"sequence": 1, "deleted": False, "address": {"city": "A", "state": "CA"}},
                ]
            }
        }
        assert [s["city"] for s in ex.extract_stops(shipment)] == ["A", "B"]

    def test_zip_and_location_id_are_kept(self):
        shipment = {
            "details": {
                "globalRoute": [
                    {"sequence": 1, "location": {"id": 22661681}, "address": {"city": "A", "state": "CA"}},
                    {"sequence": 2, "address": {"city": "B", "state": "CA", "zipCode": "94550"}},
                ]
            }
        }
        stops = ex.extract_stops(shipment)
        assert [(s["zip"], s["location_id"]) for s in stops] == [(None, 22661681), ("94550", None)]

    def test_stray_spaces_and_misdecoded_characters_are_cleaned(self):
        shipment = {
            "details": {
                "globalRoute": [
                    {"sequence": 1, "address": {"city": "Edison", "state": "NJ"}},
                    {"sequence": 2, "address": {"city": "Atlanta ", "state": "GA\u00c2\u00a0 "}},
                ]
            }
        }
        stops = ex.extract_stops(shipment)
        assert stops[1]["city"] == "Atlanta"
        assert stops[1]["state"] == "GA"
        assert ex.build_lane_string(stops) == "Edison NJ - Atlanta GA"


class TestCostCodes:
    @pytest.mark.parametrize(
        "raw",
        ["Freight - Flat", "freight-flat", "Freight  –  Flat", "FREIGHT — FLAT", "Freight -Flat"],
    )
    def test_dash_and_spacing_variants_fold_together(self, raw):
        assert ex.normalize_cost_code(raw) == "freight-flat"

    def test_empty_input_is_empty(self):
        assert ex.normalize_cost_code(None) == ""


class TestExtractLatestFreight:
    def test_latest_freight_wins(self, shipment_payload):
        carrier_orders = shipment_payload["details"]["carrierOrder"]
        assert ex.extract_latest_freight(carrier_orders, "carrier") == {"side": "carrier", "amount": 1600.0}

    def test_layover_and_detention_are_not_freight(self):
        orders = [
            {
                "costs": {
                    "lineItem": [
                        {"deleted": False, "code": {"value": "Freight - Flat"}, "amount": 500.0},
                        {"deleted": False, "code": {"value": "Accessorial - Layover"}, "amount": 50.0},
                        {"deleted": False, "code": {"value": "Accessorial - Detention"}, "amount": 60.0},
                    ]
                }
            }
        ]
        assert ex.extract_latest_freight(orders, "carrier") == {"side": "carrier", "amount": 500.0}

    def test_deleted_line_items_are_ignored(self):
        orders = [
            {
                "costs": {
                    "lineItem": [
                        {"deleted": True, "code": {"value": "Freight - Flat"}, "amount": 999.0},
                        {"deleted": False, "code": {"value": "Freight - Flat"}, "amount": 100.0},
                    ]
                }
            }
        ]
        assert ex.extract_latest_freight(orders, "carrier")["amount"] == 100.0

    def test_no_orders_returns_no_freight(self):
        assert ex.extract_latest_freight([], "carrier") is None


class TestExtractOrderTotal:
    def test_reads_turvos_total_as_is(self):
        orders = [{"deleted": False, "costs": {"subTotal": 3226.9, "totalTax": 0, "totalAmount": 3226.9}}]
        assert ex.extract_order_total(orders) == 3226.9

    def test_live_orders_sum_and_deleted_ones_are_skipped(self):
        orders = [
            {"deleted": False, "costs": {"totalAmount": 1100.0}},
            {"deleted": True, "costs": {"totalAmount": 999.0}},
            {"deleted": False, "costs": {"totalAmount": 1941.9}},
        ]
        assert ex.extract_order_total(orders) == pytest.approx(3041.9)

    @pytest.mark.parametrize(
        "orders",
        [
            [],
            [{"deleted": False, "costs": {}}],
            [{"deleted": False, "costs": {"totalAmount": 0}}],
            [{"deleted": True, "costs": {"totalAmount": 500.0}}],
        ],
    )
    def test_no_positive_total_is_none(self, orders):
        assert ex.extract_order_total(orders) is None


class TestEquipment:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("Dry Van", "Van"), ("53' Reefer", "Reefer"), ("VAN", "Van"), ("Flatbed", "Flatbed"), (None, None)],
    )
    def test_normalization(self, raw, expected):
        assert ex.normalize_equipment(raw) == expected

    def test_extract_prefers_a_live_equipment_entry(self):
        details = {
            "equipment": [
                {"deleted": True, "type": {"value": "Reefer"}},
                {"deleted": False, "type": {"value": "Dry Van"}},
            ]
        }
        assert ex.extract_equipment(details) == "Dry Van"

    def test_extract_falls_back_to_equipment_type(self):
        assert ex.extract_equipment({"equipmentType": {"value": "Flatbed"}}) == "Flatbed"


class TestNormalizeStatus:
    @pytest.mark.parametrize(
        "raw",
        ["Processing", {"value": "Processing"}, {"name": "Processing"}, {"code": {"value": "Processing"}}],
    )
    def test_every_shape_yields_the_plain_string(self, raw):
        assert ex.normalize_status(raw) == "Processing"

    def test_none_uses_the_fallback(self):
        assert ex.normalize_status(None) == "Covered"


class TestComputeMargins:
    def test_freight_basis_measures_rates_against_carrier_freight(self):
        _, gs_margin, dat_margin, _ = ex.compute_margins(
            2000, 1500, 2400, 1800, 1800, 1700, rate_margin_basis="freight"
        )
        assert gs_margin == pytest.approx(16.67, abs=0.01)  # (1800-1500)/1800
        assert dat_margin == pytest.approx(11.76, abs=0.01)  # (1700-1500)/1700

    def test_total_basis_measures_rates_against_carrier_total(self):
        _, gs_margin, _, _ = ex.compute_margins(2000, 1500, 2400, 1800, 1800, None, rate_margin_basis="total")
        assert gs_margin == 0.0  # (1800-1800)/1800

    def test_linehaul_and_total_margins(self):
        lh, _, _, total = ex.compute_margins(2000, 1500, 2400, 1800, None, None)
        assert lh == 25.0
        assert total == 25.0

    def test_missing_or_zero_inputs_give_none_not_a_wrong_number(self):
        assert ex.compute_margins(None, 1500, 0, 1800, 0, None) == (None, None, None, None)

    def test_negative_margin_is_reported(self):
        lh, _, _, _ = ex.compute_margins(1000, 1200, None, None, None, None)
        assert lh == -20.0


class TestDateHelpers:
    def test_iso_to_display(self):
        assert ex.format_mmddyyyy("2025-04-08T00:00:00Z") == "04/08/2025"

    def test_display_to_iso(self):
        assert ex.mmddyyyy_to_iso_date("04/08/2025") == "2025-04-08"

    @pytest.mark.parametrize("bad", [None, "", "not-a-date"])
    def test_unparseable_values_return_none(self, bad):
        assert ex.format_mmddyyyy(bad) is None

    @pytest.mark.parametrize("ts", ["2025-04-08T00:00:00.000Z", "2025-04-08T10:11:12.345Z", "2025-04-08"])
    def test_milliseconds_and_bare_dates_now_parse(self, ts):
        # Previously None: format_mmddyyyy demanded exactly %Y-%m-%dT%H:%M:%SZ.
        assert ex.format_mmddyyyy(ts) == "04/08/2025"


class TestCarrierIdentity:
    def test_lumper_advance_is_filtered_out(self, shipment_payload):
        orders = ex.filter_carrier_orders(shipment_payload["details"]["carrierOrder"])
        assert all((o.get("carrier") or {}).get("name") != "Lumper Advance" for o in orders)

    def test_lumper_survives_when_it_is_the_only_carrier(self):
        only_lumper = [{"carrier": {"name": "Lumper Advance"}}]
        assert ex.filter_carrier_orders(only_lumper) == only_lumper

    def test_name_and_account_manager_come_from_the_live_order(self, shipment_payload):
        orders = ex.filter_carrier_orders(shipment_payload["details"]["carrierOrder"])
        assert ex.extract_carrier_identity(orders) == ("SWIFT TRANSPORT LLC", "Jordan Lee")

    def test_no_orders_yields_no_identity(self):
        assert ex.extract_carrier_identity([]) == (None, None)


class TestCarrierNotes:
    def test_note_comes_from_the_carrier_costs(self):
        orders = [{"deleted": False, "costs": {"notes": " abcdefghijk ", "lineItem": []}}]
        assert ex.extract_carrier_notes(orders) == "abcdefghijk"

    def test_deleted_orders_and_blank_notes_are_ignored(self):
        orders = [
            {"deleted": True, "costs": {"notes": "old"}},
            {"deleted": False, "costs": {"notes": ""}},
            {"deleted": False, "costs": {"notes": "keep"}},
            {"deleted": False, "costs": {"notes": "keep"}},
            {"deleted": False, "costs": {"notes": "second"}},
        ]
        assert ex.extract_carrier_notes(orders) == "keep | second"

    def test_no_note_yields_none(self, shipment_payload):
        assert ex.extract_carrier_notes(shipment_payload["details"]["carrierOrder"]) is None
        assert ex.extract_carrier_notes([]) is None
