"""The blend definitions, executed.

The averaged columns live as SQL text in the dashboard repository. SQLite understands the same CASE/COALESCE these use, so
the expressions can be run here against literal rows -- no Postgres, no fixture
table -- which is the only way to pin down what a half-priced lane produces.
"""

from __future__ import annotations

import sqlite3

import pytest

from t3ra_unified.covered.dashboard_repo import (
    AVG_NETWORK_DIFF_SQL,
    AVG_NETWORK_RATE_SQL,
    OPPORTUNITY_COST_SQL,
)


def _evaluate(expression: str, **columns: float | None) -> float | None:
    """Run one aliased SELECT expression over a single literal row."""
    names = ", ".join(f"? AS {name}" for name in columns)
    sql = f"SELECT {expression} FROM (SELECT {names})"
    # Floats, always: SQLite divides two integers into an integer, which
    # Postgres NUMERIC columns never do.
    values = tuple(None if v is None else float(v) for v in columns.values())
    with sqlite3.connect(":memory:") as conn:
        return conn.execute(sql, values).fetchone()[0]


def _rate(gs_rate: float | None, dat_rate: float | None) -> float | None:
    return _evaluate(AVG_NETWORK_RATE_SQL, gs_rate=gs_rate, dat_rate=dat_rate)


def _diff(
    gs_rate: float | None, dat_rate: float | None, carrier_freight_cost: float | None
) -> float | None:
    return _evaluate(
        AVG_NETWORK_DIFF_SQL,
        gs_rate=gs_rate,
        dat_rate=dat_rate,
        carrier_freight_cost=carrier_freight_cost,
    )


def _opportunity(
    gs_rate: float | None, dat_rate: float | None, carrier_freight_cost: float | None
) -> float | None:
    return _evaluate(
        OPPORTUNITY_COST_SQL,
        gs_rate=gs_rate,
        dat_rate=dat_rate,
        carrier_freight_cost=carrier_freight_cost,
    )


# ----------------------------------------------------------------------
# Avg Network Rate
# ----------------------------------------------------------------------
def test_both_engines_priced_averages_them():
    assert _rate(1900, 2100) == 2000


@pytest.mark.parametrize(
    ("gs_rate", "dat_rate"),
    [(1900, None), (None, 1900), (1900, 0), (0, 1900)],
)
def test_a_missing_engine_uses_the_other_engine_rate(gs_rate, dat_rate):
    """A lone engine's rate is the network rate; a zero rate is a missing one."""
    assert _rate(gs_rate, dat_rate) == 1900


def test_neither_engine_priced_leaves_the_blend_blank():
    assert _rate(None, None) is None
    assert _rate(0, 0) is None


# ----------------------------------------------------------------------
# Avg Network Diff %
# ----------------------------------------------------------------------
def test_both_diffs_average_to_the_mean_of_the_percentages():
    # GS: (2000-1000)/2000 = 0.5, DAT: (4000-1000)/4000 = 0.75.
    assert _diff(2000, 4000, 1000) == pytest.approx(0.625)


@pytest.mark.parametrize(
    ("gs_rate", "dat_rate"),
    [(2000, None), (None, 2000)],
)
def test_a_missing_diff_uses_the_other_engine_diff(gs_rate, dat_rate):
    assert _diff(gs_rate, dat_rate, 1000) == pytest.approx(0.5)


def test_a_zero_rate_is_missing_too():
    """A zero rate is not a prediction -- it is the absence of one."""
    assert _diff(2000, 0, 1000) == pytest.approx(0.5)


def test_no_carrier_cost_leaves_both_diffs_blank():
    """Neither side can be measured, so there is nothing to average."""
    assert _diff(2000, 4000, None) is None


# ----------------------------------------------------------------------
# Opportunity Cost $
# ----------------------------------------------------------------------
def test_booked_over_the_network_rate_shows_the_gap():
    # Blend is 2000; the lane was booked at 2500, so 500 over the network.
    assert _opportunity(1900, 2100, 2500) == 500


def test_booked_under_the_network_rate_is_blank():
    """Only a positive gap is an opportunity cost -- the rest show a dash."""
    assert _opportunity(1900, 2100, 1500) is None


def test_booked_exactly_at_the_network_rate_is_blank():
    assert _opportunity(1900, 2100, 2000) is None


def test_no_network_rate_leaves_it_blank():
    assert _opportunity(None, None, 1500) is None


@pytest.mark.parametrize("carrier_freight_cost", [None, 0])
def test_a_missing_carrier_cost_leaves_it_blank(carrier_freight_cost):
    """A zero carrier cost is an absent cost, not a free load."""
    assert _opportunity(1900, 2100, carrier_freight_cost) is None


@pytest.mark.parametrize(("gs_rate", "dat_rate"), [(1900, None), (None, 1900)])
def test_a_single_engine_lane_uses_that_engine_rate(gs_rate, dat_rate):
    # Network rate is 1900; booked at 2000, so the gap is 100.
    assert _opportunity(gs_rate, dat_rate, 2000) == 100
