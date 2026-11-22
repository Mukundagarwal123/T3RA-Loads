"""Columns computed from other columns, and what each one depends on.

Every derived column in ``covered_loads`` is declared in :data:`DEPENDENCIES`
with the columns it reads. The write paths in
:mod:`t3ra_unified.covered.repository` recompute the whole derived
set from the values a write is *actually about to store*, so a derived column
can never disagree with the inputs sitting beside it in the same row.

Computing from the merged row rather than from the payload is the whole point.
A write is a merge, not a replacement: most columns land as
``COALESCE(new, existing)``, so a payload that carries a freshly entered
carrier cost but no rate still has to produce a GS margin against the *stored*
rate. Margins computed from the payload alone go wrong in exactly the case
this exists for -- a cost that arrives after the load was first written.

The dashboard's own percentage columns (GS Diff %, LH %, Opp Cost, ...) are
not here: those are computed at read time in
:mod:`t3ra_unified.covered.dashboard_repo`, so they already follow their
inputs and cannot go stale. These four are the ones kept in the table.

Adding a derived column means adding it here and to
:func:`recompute_derived`; no write path needs to change.
"""

from __future__ import annotations

from typing import Any

from t3ra_unified.covered.domain.extractors import RateMarginBasis, compute_margins

#: Derived column -> the columns it is computed from. Read by the write paths
#: to decide whether a write can change a derived value, and by the tests that
#: keep this map honest against :func:`recompute_derived`.
DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "lh_margin": ("customer_freight_cost", "carrier_freight_cost"),
    "gs_margin": ("gs_rate", "carrier_freight_cost", "carrier_total_cost"),
    "dat_margin": ("dat_rate", "carrier_freight_cost", "carrier_total_cost"),
    "t_margin": ("customer_total_cost", "carrier_total_cost"),
}

#: Every column any derived column reads.
INPUT_COLUMNS: frozenset[str] = frozenset(
    column for inputs in DEPENDENCIES.values() for column in inputs
)

#: Record dicts spell a few columns differently from the table. Historical, and
#: not worth a rename while it is only these four.
_RECORD_KEYS: dict[str, str] = {
    "gs_rate": "GS_rate",
    "lh_margin": "LH_margin",
    "gs_margin": "GS_margin",
}

#: Rates are never carried by a shipment update -- a load is priced once, and
#: ``RATE_OWNED_COLUMNS`` keeps rate columns out of those statements entirely.
#: So a rate always falls back to what is stored, whatever the merge mode.
_RATE_COLUMNS: frozenset[str] = frozenset({"gs_rate", "dat_rate"})


def record_key(column: str) -> str:
    """The key a record dict uses for ``column``."""
    return _RECORD_KEYS.get(column, column)


def _effective(
    column: str,
    record: dict[str, Any],
    existing: dict[str, Any] | None,
    *,
    coalesce: bool,
) -> Any:
    """The value ``column`` will hold once this write lands.

    Mirrors what the SQL does, which is why the merge mode has to be passed in:
    the upsert and the refresh update both merge with ``COALESCE``, while
    ``overwrite_preserving_rates`` deliberately clears a field Turvo dropped.
    """
    key = record_key(column)
    incoming = record.get(key)
    stored = (existing or {}).get(column)

    if column in _RATE_COLUMNS:
        return incoming if incoming is not None else stored
    if coalesce:
        return incoming if incoming is not None else stored
    return incoming


def recompute_derived(
    record: dict[str, Any],
    existing: dict[str, Any] | None = None,
    *,
    coalesce: bool = True,
    rate_margin_basis: RateMarginBasis = "freight",
) -> dict[str, Any]:
    """Rewrite ``record``'s derived columns from the row this write produces.

    Mutates and returns ``record``. ``existing`` is the stored row, or None
    when there is not one yet -- a fresh insert merges against nothing.

    Every derived column is rewritten unconditionally, including back to None.
    A cost that goes away has to take its margins with it; leaving the old
    number behind is how a margin outlives the cost it was computed from.
    """
    values = {
        column: _effective(column, record, existing, coalesce=coalesce)
        for column in INPUT_COLUMNS
    }

    lh_margin, gs_margin, dat_margin, t_margin = compute_margins(
        values["customer_freight_cost"],
        values["carrier_freight_cost"],
        values["customer_total_cost"],
        values["carrier_total_cost"],
        values["gs_rate"],
        values["dat_rate"],
        rate_margin_basis=rate_margin_basis,
    )

    record[record_key("lh_margin")] = lh_margin
    record[record_key("gs_margin")] = gs_margin
    record[record_key("dat_margin")] = dat_margin
    record[record_key("t_margin")] = t_margin
    return record
