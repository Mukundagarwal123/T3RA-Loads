"""Tests for Greenscreens response parsing and the DAT adapters."""

from __future__ import annotations

import pytest

from t3ra_unified.clients import dat as dat_client
from t3ra_unified.clients import greenscreens as gs_client
from t3ra_unified.covered.domain import rate_engines
from t3ra_unified.covered.domain.rate_engines import extract_confidence, fill_missing_zips, parse_gs_response


class TestParseGsResponse:
    def test_top_level_rate_is_target_buy_times_distance(self):
        rate, confidence = parse_gs_response({"targetBuyRate": 2.5, "distance": 800, "confidenceScore": 0.91})
        assert rate == 2000.0
        assert confidence == 0.91

    def test_nested_network_rates_list_is_understood(self):
        body = {"networkRates": [{"targetBuyRate": 2.0, "distance": 500, "confidence": 0.8}]}
        assert parse_gs_response(body) == (1000.0, 0.8)

    def test_alternative_key_names_are_understood(self):
        body = {"data": {"rates": [{"buyRate": 1.5, "miles": 200}]}}
        rate, _ = parse_gs_response(body)
        assert rate == 300.0

    def test_a_body_with_no_rate_yields_none(self):
        assert parse_gs_response({"message": "no coverage"}) == (None, None)

    def test_a_non_dict_body_yields_none(self):
        assert parse_gs_response("<html>gateway error</html>") == (None, None)

    def test_confidence_survives_a_missing_rate(self):
        rate, confidence = parse_gs_response({"networkRates": [{"confidence": 0.42}]})
        assert rate is None
        assert confidence == 0.42

    @pytest.mark.parametrize("key", ["confidenceScore", "confidence", "predictionConfidence", "score"])
    def test_every_known_confidence_key_is_read(self, key):
        assert extract_confidence({key: 0.75}) == 0.75


class TestGreenscreensPayload:
    def test_stops_are_renumbered_from_one(self):
        payload = gs_client.build_payload(
            "Van",
            "2025-04-08",
            [{"city": "A", "state": "CA", "zip": "1"}, {"city": "B", "state": "NV", "zip": "2"}],
        )
        assert [s["order"] for s in payload["stops"]] == [1, 2]

    def test_a_bare_date_is_expanded_to_a_timestamp(self):
        payload = gs_client.build_payload("Reefer", "2025-04-08", [])
        assert payload["pickupDateTime"] == "2025-04-08T00:00:00Z"

    def test_an_existing_timestamp_is_left_alone(self):
        payload = gs_client.build_payload("Van", "2025-04-08T06:30:00Z", [])
        assert payload["pickupDateTime"] == "2025-04-08T06:30:00Z"

    @pytest.mark.parametrize("equipment", ["Power Only", "Step Deck", ""])
    def test_unsupported_equipment_produces_no_payload(self, equipment):
        assert gs_client.build_payload(equipment, "2025-04-08", []) is None

    def test_flatbed_is_sent_as_the_exact_enum_value(self):
        """The endpoint 400s on "FLAT_BED"; only "FLATBED" is priced."""
        payload = gs_client.build_payload("Flatbed", "2025-04-08", [])
        assert payload["transportType"] == "FLATBED"


class TestDatHelpers:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("Dry Van", "VAN"), ("Reefer", "REEFER"), ("53 Flatbed", "FLATBED"), (None, "VAN"), ("Sprinter", "VAN")],
    )
    def test_equipment_mapping(self, raw, expected):
        assert dat_client.map_equipment(raw) == expected

    def test_city_names_are_title_cased_with_particles_left_lowercase(self):
        assert dat_client.normalize_city("MARINA DEL REY", "CA") == "Marina del Rey"
        assert dat_client.normalize_city("LOS ANGELES", "CA") == "los Angeles"

    def test_a_known_misspelling_is_corrected(self):
        assert dat_client.normalize_city("MARINA DEL RAY", "CA") == "Marina Del Rey"

    def test_total_is_linehaul_plus_fuel(self):
        body = {
            "rateResponses": [
                {"response": {"rate": {"perTrip": {"rateUsd": 1500.0}, "averageFuelSurchargePerTripUsd": 250.0}}}
            ]
        }
        assert dat_client.extract_total_trip_usd(body) == 1750.0

    def test_a_missing_fuel_component_yields_none(self):
        body = {"rateResponses": [{"response": {"rate": {"perTrip": {"rateUsd": 1500.0}}}}]}
        assert dat_client.extract_total_trip_usd(body) is None

    def test_an_empty_response_yields_none(self):
        assert dat_client.extract_total_trip_usd({"rateResponses": []}) is None


class TestFillMissingZips:
    @pytest.fixture(autouse=True)
    def fake_locations(self, monkeypatch):
        """Turvo location records by id; counts the lookups made."""
        rate_engines._location_zip.cache_clear()
        calls: list[int] = []
        records = {
            7: {
                "details": {
                    "address": [
                        {"zip": "00000", "deleted": True},
                        {"zip": "11111", "type": {"value": "Billing"}},
                        {"zip": "91730", "type": {"value": "Main"}},
                    ]
                }
            },
            8: {"details": {"address": []}},
        }

        def fetch_location(location_id):
            calls.append(location_id)
            if location_id not in records:
                raise RuntimeError("404")
            return records[location_id]

        monkeypatch.setattr(rate_engines.turvo_client, "fetch_location", fetch_location)
        yield calls
        rate_engines._location_zip.cache_clear()

    def test_a_missing_zip_comes_from_the_main_location_address(self):
        stops = fill_missing_zips([{"city": "RCH CUCAMONGA", "state": "CA", "zip": None, "location_id": 7}])
        assert stops[0]["zip"] == "91730"

    def test_a_stop_with_a_zip_is_not_looked_up(self, fake_locations):
        stops = [{"city": "RENO", "state": "NV", "zip": "89502", "location_id": 7}]
        assert fill_missing_zips(stops) == stops
        assert fake_locations == []

    def test_each_location_is_fetched_once(self, fake_locations):
        stop = {"city": "RCH CUCAMONGA", "state": "CA", "zip": None, "location_id": 7}
        fill_missing_zips([stop, stop])
        fill_missing_zips([stop])
        assert fake_locations == [7]

    @pytest.mark.parametrize("location_id", [None, 8, 9])
    def test_no_zip_found_leaves_the_stop_alone(self, location_id):
        stop = {"city": "Spanish Springs", "state": "NV", "zip": None, "location_id": location_id}
        assert fill_missing_zips([stop]) == [stop]
