"""Tests for equipment normalization and rate-engine eligibility."""

from __future__ import annotations

import pytest

from t3ra_unified.covered.domain.equipment import (
    CANONICAL_EQUIPMENT,
    dat_allows_equipment,
    gs_allows_equipment,
    is_canonical,
    normalize_equipment,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Van", "Van"),
        ("VAN", "Van"),
        ("Dry Van", "Van"),
        ("53' Dry Van", "Van"),
        ("dv", "Van"),
        ("Reefer", "Reefer"),
        ("REEFER 53", "Reefer"),
        ("Refrigerated", "Reefer"),
        ("Temp Control", "Reefer"),
        ("Flatbed", "Flatbed"),
        ("Flat Bed", "Flatbed"),
        ("FLAT-BED 48", "Flatbed"),
        ("Bed Pad", "Flatbed"),
        ("Conestoga", "Flatbed"),
        ("Step Deck", "Step Deck"),
        ("stepdeck", "Step Deck"),
        ("RGN", "Step Deck"),
        ("Power Only", "Power Only"),
        ("Box Truck", "Box Truck"),
        ("Straight Truck", "Box Truck"),
        ("Sprinter", "Sprinter"),
        ("Cargo Van", "Sprinter"),
    ],
)
def test_turvo_spellings_fold_onto_canonical_values(raw, expected):
    assert normalize_equipment(raw) == expected


def test_whitespace_and_case_do_not_matter():
    assert normalize_equipment("   flat   bed  ") == "Flatbed"


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_an_absent_value_stays_absent(raw):
    assert normalize_equipment(raw) is None


def test_a_specific_spelling_wins_over_a_generic_one():
    """'Reefer Van' is a reefer, and 'Cargo Van' is a sprinter -- neither is a Van."""
    assert normalize_equipment("Reefer Van") == "Reefer"
    assert normalize_equipment("Cargo Van") == "Sprinter"


def test_a_short_alias_only_matches_the_whole_string():
    """'dv' is a Van, but the letters inside another word are not."""
    assert normalize_equipment("dv") == "Van"
    assert normalize_equipment("Advance Trailer") == "Advance Trailer"


def test_an_unknown_value_is_kept_rather_than_dropped():
    """An unrecognised trailer still reaches the row, it simply is not priced."""
    assert normalize_equipment("  Hovercraft ") == "Hovercraft"
    assert not is_canonical("Hovercraft")
    assert not gs_allows_equipment("Hovercraft")
    assert not dat_allows_equipment("Hovercraft")


class TestEligibility:
    @pytest.mark.parametrize(
        "equipment",
        ["Van", "Dry Van", "Dryvan", "Dry", "Reefer", "refrigerated", "Flatbed", "Flat Bed"],
    )
    def test_greenscreens_prices_vans_reefers_and_flatbeds(self, equipment):
        """FLATBED was verified against the live v3 endpoint -- it returns a rate."""
        assert gs_allows_equipment(equipment)

    @pytest.mark.parametrize("equipment", ["Step Deck", "Power Only", "Box Truck", None])
    def test_greenscreens_prices_nothing_else(self, equipment):
        assert not gs_allows_equipment(equipment)

    @pytest.mark.parametrize("equipment", ["Van", "Reefer", "Flatbed", "Flat Bed", "Bed Pad"])
    def test_dat_also_prices_flatbeds(self, equipment):
        assert dat_allows_equipment(equipment)

    @pytest.mark.parametrize("equipment", ["Step Deck", "Power Only", "Box Truck", None])
    def test_dat_prices_nothing_else(self, equipment):
        assert not dat_allows_equipment(equipment)

    def test_every_eligible_value_is_one_the_normalizer_can_produce(self):
        """A typo in an eligibility set would silently disable an engine."""
        from t3ra_unified.covered.domain.equipment import DAT_ELIGIBLE_EQUIPMENT, GS_ELIGIBLE_EQUIPMENT

        assert GS_ELIGIBLE_EQUIPMENT <= CANONICAL_EQUIPMENT
        assert DAT_ELIGIBLE_EQUIPMENT <= CANONICAL_EQUIPMENT
