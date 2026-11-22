"""Every read the covered-loads dashboard makes.

The FreightX portal reads through here via the JSON API in
:mod:`t3ra_unified.api.dashboard`, and the margin definitions below
live in one place.

Rows come back as plain dicts. Presentation (percent scaling, formatting,
KPI rollups) belongs to the caller.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from t3ra_unified.config import get_settings
from t3ra_unified.db import engine
from t3ra_unified.observability import traced_function

#: The diff expressions, unaliased, so they can be reused inside the averaged
#: column below without being restated. Each is a fraction, not a percentage.
_GS_DIFF_EXPR = """CASE
    WHEN gs_rate IS NULL OR gs_rate = 0
      OR carrier_freight_cost IS NULL OR carrier_freight_cost = 0 THEN NULL
    ELSE ((gs_rate - carrier_freight_cost) / gs_rate)
END"""

_DAT_DIFF_EXPR = """CASE
    WHEN dat_rate IS NULL OR dat_rate = 0
      OR carrier_freight_cost IS NULL OR carrier_freight_cost = 0 THEN NULL
    ELSE ((dat_rate - carrier_freight_cost) / dat_rate)
END"""

#: Linehaul margin as a fraction, computed in SQL so the dashboard and the
#: pipeline cannot drift on the definition.
LH_MARGIN_SQL = """CASE
    WHEN customer_freight_cost IS NULL OR customer_freight_cost = 0
      OR carrier_freight_cost IS NULL OR carrier_freight_cost = 0 THEN NULL
    ELSE ((customer_freight_cost - carrier_freight_cost) / customer_freight_cost)
END AS lh_margin"""

#: How far the booked carrier cost sits below the Greenscreens prediction.
GS_DIFF_SQL = f"{_GS_DIFF_EXPR} AS gs_diff"

#: How far the booked carrier cost sits below the DAT prediction.
DAT_DIFF_SQL = f"{_DAT_DIFF_EXPR} AS dat_diff"

#: The two engines blended. When both engines priced the lane this is their
#: average; when only one did, it is that engine's rate on its own; blank only
#: when neither priced it. A zero rate is not a prediction, so it counts as
#: missing. COALESCE-ing each side with the other makes a lone rate average
#: with itself.
_GS_RATE_EXPR = "NULLIF(gs_rate, 0)"
_DAT_RATE_EXPR = "NULLIF(dat_rate, 0)"

_AVG_NETWORK_RATE_EXPR = f"""CASE
    WHEN {_GS_RATE_EXPR} IS NULL AND {_DAT_RATE_EXPR} IS NULL THEN NULL
    ELSE ((COALESCE({_GS_RATE_EXPR}, {_DAT_RATE_EXPR})
         + COALESCE({_DAT_RATE_EXPR}, {_GS_RATE_EXPR})) / 2)
END"""

AVG_NETWORK_RATE_SQL = f"{_AVG_NETWORK_RATE_EXPR} AS avg_network_rate"

#: The mean of the two diffs -- deliberately NOT the diff of the averaged rate.
#: The two are not equal, and this column answers "how did we do against each
#: engine, on average", which is the mean of the percentages. Follows the same
#: rule as AVG_NETWORK_RATE_SQL: a lone diff is used as-is, blank only when both
#: are missing.
AVG_NETWORK_DIFF_SQL = f"""CASE
    WHEN ({_GS_DIFF_EXPR}) IS NULL AND ({_DAT_DIFF_EXPR}) IS NULL THEN NULL
    ELSE ((COALESCE({_GS_DIFF_EXPR}, {_DAT_DIFF_EXPR})
         + COALESCE({_DAT_DIFF_EXPR}, {_GS_DIFF_EXPR})) / 2)
END AS avg_network_diff"""

#: What the lane cost us above what the network said it was worth, in dollars.
#: Only a load booked *over* the blended rate carries one -- carrier cost minus
#: network rate has to be positive -- so the column reads as a list of the loads
#: that overpaid, and every other row shows a dash. A zero carrier cost is a
#: missing cost, not a free load, and is excluded with it.
OPPORTUNITY_COST_SQL = f"""CASE
    WHEN ({_AVG_NETWORK_RATE_EXPR}) IS NULL
      OR carrier_freight_cost IS NULL OR carrier_freight_cost = 0
      OR (carrier_freight_cost - ({_AVG_NETWORK_RATE_EXPR})) <= 0 THEN NULL
    ELSE (carrier_freight_cost - ({_AVG_NETWORK_RATE_EXPR}))
END AS opportunity_cost"""

#: Truckload only. There is deliberately no equipment whitelist here: a covered
#: load belongs on the dashboard whatever trailer it moved on, and filtering by
#: equipment is what used to hide every load whose Turvo equipment string did
#: not normalize to one of three exact spellings. Equipment still decides which
#: rate engines run -- that gate lives in domain/rules.py, where it belongs.
BASE_FILTER = """
  AND transport_mode = 'TL'
"""

_SHARED_COLUMNS = """shipment_num, lane, covered_date, pickup_date, customer, carrier, carrier_am,
           equipment, equipment_raw, carrier_notes,
           -- Whichever note changed last: a user's save, or Turvo's carrier note.
           CASE
               WHEN notes IS NULL THEN carrier_notes
               WHEN carrier_notes IS NULL THEN notes
               WHEN carrier_notes_updated_at > notes_updated_at THEN carrier_notes
               ELSE notes
           END AS notes,
           customer_freight_cost, carrier_freight_cost,
           customer_total_cost, carrier_total_cost,
           gs_rate, gs_confidence_score,
           dat_rate"""


def _table() -> str:
    return get_settings().covered_view


def _fetch_rows(sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    with engine.cursor(dict_rows=True) as cur:
        cur.execute(sql, tuple(params or ()))
        return [dict(row) for row in cur.fetchall()]


@traced_function()
def fetch_priced_loads(start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Covered loads in the window that have a Greenscreens rate.

    ``lh_margin``, ``gs_diff`` and ``dat_diff`` come back as fractions, not
    percentages.
    """
    sql = f"""
    SELECT {_SHARED_COLUMNS},
           {GS_DIFF_SQL}, {DAT_DIFF_SQL}, {LH_MARGIN_SQL},
           {AVG_NETWORK_RATE_SQL}, {AVG_NETWORK_DIFF_SQL},
           {OPPORTUNITY_COST_SQL}
    FROM {_table()}
    WHERE covered_date >= %s AND covered_date <= %s
    {BASE_FILTER}
      AND gs_rate IS NOT NULL
    ORDER BY covered_date DESC NULLS LAST
    LIMIT {get_settings().dashboard_row_limit}
    """
    return _fetch_rows(sql, [start, end])


@traced_function()
def fetch_unpriced_loads(start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Covered loads in the window with no Greenscreens rate.

    No ``gs_diff`` column: without a rate there is nothing to compare against.
    ``dat_rate`` still comes back -- DAT prices a lane independently of GS.
    """
    sql = f"""
    SELECT {_SHARED_COLUMNS}
    FROM {_table()}
    WHERE covered_date >= %s AND covered_date <= %s
    {BASE_FILTER}
      AND gs_rate IS NULL
    ORDER BY pickup_date DESC NULLS LAST
    LIMIT {get_settings().dashboard_row_limit}
    """
    return _fetch_rows(sql, [start, end])


@traced_function()
def fetch_data_version(job_name: str) -> dict[str, datetime | None]:
    """When the loads last changed underneath the dashboard.

    The portal polls this instead of re-reading the whole window, and refetches
    the loads only when a value moves. Two writers change the table:

    * the webhook worker, once per ``SHIPMENT_UPDATE`` or
      ``SHIPMENT_STATUS_UPDATE`` it applies -- it marks the event ``done`` only
      after the row is written, so ``processed_at`` never runs ahead of the data;
    * the refresh job, whose heartbeat stamps ``finished_at`` per run.

    Both lookups are index-backed (see migration 0005), so this stays cheap
    however often it is polled.
    """
    sql = """
    SELECT
        (SELECT MAX(processed_at) FROM webhook_queue WHERE state = 'done') AS last_webhook_at,
        (SELECT finished_at FROM job_run_status WHERE job_name = %s) AS last_refresh_at
    """
    rows = _fetch_rows(sql, [job_name])
    row = rows[0] if rows else {}
    return {
        "last_webhook_at": row.get("last_webhook_at"),
        "last_refresh_at": row.get("last_refresh_at"),
    }
