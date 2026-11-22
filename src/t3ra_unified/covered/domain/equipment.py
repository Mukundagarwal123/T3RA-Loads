"""Canonical equipment types, and which rate engine accepts each one.

Turvo's equipment field is free text, so the same trailer arrives as
``"Flat Bed"``, ``"Flatbed"``, ``"FLATBED 48"`` and ``"Bed Pad"`` on different
shipments. Everything downstream -- the rate-engine gates in
:mod:`t3ra_unified.covered.domain.rules`, the ``equipment`` column, and the dashboard
-- reads the canonical value produced here, so a new Turvo spelling is a
one-line change in :data:`_SUBSTRING_RULES` rather than a bug in four places.

Eligibility mirrors what each vendor's API actually accepts:

* Greenscreens takes VAN, REEFER and FLATBED (``clients/greenscreens.py``).
  Verified against the live v3 endpoint: FLATBED returns a priced response.
* DAT takes VAN, REEFER and FLATBED (``clients/dat.py::map_equipment``).

Anything else is stored and displayed but never priced.
"""

from __future__ import annotations

import re

VAN = "Van"
REEFER = "Reefer"
FLATBED = "Flatbed"
STEP_DECK = "Step Deck"
POWER_ONLY = "Power Only"
BOX_TRUCK = "Box Truck"
SPRINTER = "Sprinter"

#: Every canonical value this module can produce.
CANONICAL_EQUIPMENT: frozenset[str] = frozenset(
    {VAN, REEFER, FLATBED, STEP_DECK, POWER_ONLY, BOX_TRUCK, SPRINTER}
)

#: Equipment Greenscreens will price.
GS_ELIGIBLE_EQUIPMENT: frozenset[str] = frozenset({VAN, REEFER, FLATBED})

#: Equipment DAT will price.
DAT_ELIGIBLE_EQUIPMENT: frozenset[str] = frozenset({VAN, REEFER, FLATBED})

#: Short codes that must match the whole string. They are too short to match as
#: substrings -- ``"dv"`` would otherwise fire on ``"Advance"``.
_EXACT_ALIASES: dict[str, str] = {
    "v": VAN,
    "dv": VAN,
    "53v": VAN,
    "r": REEFER,
    "rf": REEFER,
    "f": FLATBED,
    "fd": FLATBED,
    "fb": FLATBED,
    "sd": STEP_DECK,
    "po": POWER_ONLY,
}

#: Word rules, tried in order -- the first hit wins, so the most specific
#: spelling must come first. ``"Cargo Van"`` has to reach the Sprinter rule
#: before the Van rule sees the word "van", and ``"Reefer Van"`` has to reach
#: the Reefer rule first. Each needle matches on word boundaries, not as a bare
#: substring, or ``"Advance Trailer"`` would normalize to Van.
_WORD_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (REEFER, ("reefer", "refrigerated", "temp control", "temperature", "temp-control")),
    (STEP_DECK, ("step deck", "stepdeck", "step-deck", "double drop", "lowboy", "rgn")),
    # "Bed Pad" is Turvo free text for a padded flatbed; confirm with ops
    # before adding more spellings here.
    (FLATBED, ("flatbed", "flat bed", "flat-bed", "bed pad", "bedpad", "conestoga", "curtain")),
    (POWER_ONLY, ("power only", "power-only", "poweronly")),
    (BOX_TRUCK, ("box truck", "boxtruck", "straight truck")),
    (SPRINTER, ("sprinter", "cargo van")),
    # "dry" on its own covers Turvo's "Dry", "Dry Box" and "48 Dry"; "dryvan"
    # is the unspaced spelling, which no word-boundary needle would catch.
    (VAN, ("dry van", "dryvan", "dry", "van")),
)

#: The rules above, compiled once, as ``(canonical, pattern)`` pairs.
_COMPILED_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (canonical, re.compile("|".join(rf"\b{re.escape(needle)}\b" for needle in needles)))
    for canonical, needles in _WORD_RULES
)


def normalize_equipment(equipment_value: str | None) -> str | None:
    """Fold Turvo's free-text equipment onto a canonical value.

    Returns None for empty input. An unrecognised value comes back trimmed but
    otherwise untouched -- it is still stored and displayed, it simply will not
    match an eligibility set, so no rate engine runs for it.
    """
    if equipment_value is None:
        return None

    raw = str(equipment_value).strip()
    if not raw:
        return None

    folded = " ".join(raw.lower().split())

    exact = _EXACT_ALIASES.get(folded)
    if exact:
        return exact

    for canonical, pattern in _COMPILED_RULES:
        if pattern.search(folded):
            return canonical

    return raw


def is_canonical(equipment: str | None) -> bool:
    """True when ``equipment`` is one of the values this module recognises."""
    return equipment in CANONICAL_EQUIPMENT


def gs_allows_equipment(equipment: str | None) -> bool:
    """True when Greenscreens will price this (already-normalized) equipment."""
    return normalize_equipment(equipment) in GS_ELIGIBLE_EQUIPMENT


def dat_allows_equipment(equipment: str | None) -> bool:
    """True when DAT will price this (already-normalized) equipment."""
    return normalize_equipment(equipment) in DAT_ELIGIBLE_EQUIPMENT
