"""Reads and writes for the covered-loads table.

The table name comes from configuration (``SHIPMENTS_TABLE``) and is
validated as a bare identifier at load time, which is what makes it safe to
interpolate here -- Postgres has no bind parameter for identifiers. Every
*value* is still passed as a parameter.
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg2.extras import execute_batch

from t3ra_unified.config import get_settings
from t3ra_unified.db import engine
from t3ra_unified.covered.domain.derived import recompute_derived
from t3ra_unified.observability import traced_function

logger = logging.getLogger(__name__)

#: Columns the refresh job is forbidden from touching -- they are owned by the
#: webhook path, which is the only place that calls the rate engines.
RATE_OWNED_COLUMNS = ("gs_rate", "dat_rate", "gs_confidence_score", "covered_date")

#: Columns only the dashboard's notes endpoint writes. No Turvo-driven write
#: path names them, so a webhook or refresh can never overwrite a user's note.
USER_OWNED_COLUMNS = ("notes", "notes_updated_at")


def _carrier_notes_stamp(new: str, old: str, old_stamp: str) -> str:
    """SQL that stamps ``carrier_notes_updated_at`` only when Turvo's note changes.

    The dashboard shows whichever of the two notes changed last, so an
    unchanged or missing carrier note must keep its old stamp.
    """
    return f"CASE WHEN {new} IS NOT NULL AND {new} IS DISTINCT FROM {old} THEN NOW() ELSE {old_stamp} END"


def _table() -> str:
    """The write target: the unified ``shipments`` table."""
    return get_settings().shipments_table


def _view() -> str:
    """Reads go through the Covered compat view, so every column keeps the
    name and shape this module's logic (diffing, derived columns) was written for."""
    return get_settings().covered_view


def is_covered_status(status_value: Any) -> bool:
    return str(status_value or "").strip().lower() == "covered"


def _normalize_covered_date(record: dict[str, Any]) -> None:
    """Only covered shipments carry a covered_date; blank strings become NULL."""
    if not is_covered_status(record.get("status")):
        record["covered_date"] = None
        return

    covered_date = record.get("covered_date")
    if isinstance(covered_date, str) and not covered_date.strip():
        record["covered_date"] = None


#: Columns a caller may legitimately omit. Every write statement names them,
#: so a missing key would be a psycopg KeyError rather than a NULL.
_OPTIONAL_COLUMNS = ("equipment_raw", "carrier_notes")


def _apply_optional_defaults(record: dict[str, Any]) -> None:
    for column in _OPTIONAL_COLUMNS:
        record.setdefault(column, None)


def _sync_derived(
    record: dict[str, Any],
    *,
    coalesce: bool,
    rate_margin_basis: str = "freight",
    existing: dict[str, Any] | None = None,
) -> None:
    """Bring a record's derived columns in line with the row it will produce.

    Called by every write path immediately before its statement runs, so a
    derived column is always computed from the values actually being stored --
    including the stored ones this write leaves untouched. A carrier cost
    entered weeks after the load was covered therefore brings its margins with
    it, and a cost that disappears takes them away again.

    The lookup is best-effort: if it fails, the record keeps the margins its
    caller computed, which is the behaviour that predates this.
    """
    if existing is None:
        shipment_num = record.get("shipment_num")
        if shipment_num is not None:
            existing = fetch_row(str(shipment_num))
    recompute_derived(
        record,
        existing,
        coalesce=coalesce,
        rate_margin_basis=rate_margin_basis,  # type: ignore[arg-type]
    )


@traced_function()
def upsert_final_record(record: dict[str, Any]) -> None:
    """Insert or update a fully-priced shipment row.

    Every column uses ``COALESCE(EXCLUDED.x, existing)`` so a partial payload
    never blanks out a value that a previous run established.
    """
    table = _table()
    _normalize_covered_date(record)
    record.setdefault("GS_confidence_score", None)
    _apply_optional_defaults(record)
    _sync_derived(record, coalesce=True)

    notes_stamp = _carrier_notes_stamp(
        "EXCLUDED.carrier_notes", f"{table}.carrier_notes", f"{table}.carrier_notes_updated_at"
    )
    query = f"""
    INSERT INTO {table} (
        shipment_num, shipment_id, lane, lane_raw, pickup_date, delivery_date,
        covered_equipment, equipment_raw,
        transport_mode, customer_name, carrier_name, carrier_am, carrier_notes, status,
        customer_freight_cost, customer_total_cost,
        carrier_freight_cost, carrier_total_cost,
        gs_rate, gs_confidence_score, dat_rate,
        lh_margin, gs_margin, dat_margin, t_margin,
        covered_date, carrier_notes_updated_at, is_covered
    )
    VALUES (
        %(shipment_num)s, %(shipment_id)s, %(lane)s, %(lane_raw)s, %(pickup_date)s, %(delivery_date)s,
        %(equipment)s, %(equipment_raw)s,
        %(transport_mode)s, %(customer)s, %(carrier)s, %(carrier_am)s, %(carrier_notes)s, %(status)s,
        %(customer_freight_cost)s, %(customer_total_cost)s,
        %(carrier_freight_cost)s, %(carrier_total_cost)s,
        %(GS_rate)s, %(GS_confidence_score)s, %(dat_rate)s,
        %(LH_margin)s, %(GS_margin)s, %(dat_margin)s, %(t_margin)s,
        %(covered_date)s,
        CASE WHEN %(carrier_notes)s IS NOT NULL THEN NOW() END,
        TRUE
    )
    ON CONFLICT (shipment_num)
    DO UPDATE SET
        lane = COALESCE(NULLIF(EXCLUDED.lane, ''), {table}.lane),
        lane_raw = COALESCE(NULLIF(EXCLUDED.lane_raw, ''), {table}.lane_raw),
        pickup_date = COALESCE(EXCLUDED.pickup_date, {table}.pickup_date),
        delivery_date = COALESCE(EXCLUDED.delivery_date, {table}.delivery_date),
        covered_equipment = COALESCE(EXCLUDED.covered_equipment, {table}.covered_equipment),
        equipment_raw = COALESCE(EXCLUDED.equipment_raw, {table}.equipment_raw),
        transport_mode = COALESCE(EXCLUDED.transport_mode, {table}.transport_mode),
        customer_name = COALESCE(EXCLUDED.customer_name, {table}.customer_name),
        carrier_name = COALESCE(EXCLUDED.carrier_name, {table}.carrier_name),
        carrier_am = COALESCE(EXCLUDED.carrier_am, {table}.carrier_am),
        carrier_notes = COALESCE(EXCLUDED.carrier_notes, {table}.carrier_notes),
        carrier_notes_updated_at = {notes_stamp},
        status = COALESCE(EXCLUDED.status, {table}.status),
        customer_freight_cost = COALESCE(EXCLUDED.customer_freight_cost, {table}.customer_freight_cost),
        customer_total_cost = COALESCE(EXCLUDED.customer_total_cost, {table}.customer_total_cost),
        carrier_freight_cost = COALESCE(EXCLUDED.carrier_freight_cost, {table}.carrier_freight_cost),
        carrier_total_cost = COALESCE(EXCLUDED.carrier_total_cost, {table}.carrier_total_cost),
        gs_rate = COALESCE(EXCLUDED.gs_rate, {table}.gs_rate),
        gs_confidence_score = COALESCE(EXCLUDED.gs_confidence_score, {table}.gs_confidence_score),
        dat_rate = COALESCE(EXCLUDED.dat_rate, {table}.dat_rate),
        lh_margin = COALESCE(EXCLUDED.lh_margin, {table}.lh_margin),
        gs_margin = COALESCE(EXCLUDED.gs_margin, {table}.gs_margin),
        dat_margin = COALESCE(EXCLUDED.dat_margin, {table}.dat_margin),
        t_margin = COALESCE(EXCLUDED.t_margin, {table}.t_margin),
        covered_date = COALESCE(EXCLUDED.covered_date, {table}.covered_date),
        is_covered = TRUE,
        updated_at = NOW();
    """

    log_context = {
        "table_name": table,
        "shipment_num": record.get("shipment_num"),
        "shipment_id": record.get("shipment_id"),
        "status_value": record.get("status"),
    }

    try:
        with engine.cursor(commit=True) as cur:
            cur.execute(query, record)
        logger.info("DB upsert succeeded", extra=log_context)
    except Exception:
        logger.exception("DB upsert failed", extra=log_context)
        raise


#: Columns a shipment-update overwrite rewrites verbatim. Every column the
#: dashboard shows except the rate-owned ones, which survive untouched.
_OVERWRITE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("shipment_id", "shipment_id"),
    ("lane", "lane"),
    ("lane_raw", "lane_raw"),
    ("pickup_date", "pickup_date"),
    ("delivery_date", "delivery_date"),
    ("covered_equipment", "equipment"),
    ("equipment_raw", "equipment_raw"),
    ("transport_mode", "transport_mode"),
    ("customer_name", "customer"),
    ("carrier_name", "carrier"),
    ("carrier_am", "carrier_am"),
    ("carrier_notes", "carrier_notes"),
    ("status", "status"),
    ("customer_freight_cost", "customer_freight_cost"),
    ("customer_total_cost", "customer_total_cost"),
    ("carrier_freight_cost", "carrier_freight_cost"),
    ("carrier_total_cost", "carrier_total_cost"),
    ("lh_margin", "LH_margin"),
    ("gs_margin", "GS_margin"),
    ("dat_margin", "dat_margin"),
    ("t_margin", "t_margin"),
)


#: The rate columns, appended only when a caller has actually priced a load
#: that had never been priced before.
_RATE_ASSIGNMENTS: tuple[tuple[str, str], ...] = (
    ("gs_rate", "GS_rate"),
    ("gs_confidence_score", "GS_confidence_score"),
    ("dat_rate", "dat_rate"),
    ("covered_date", "covered_date"),
)


@traced_function()
def overwrite_preserving_rates(record: dict[str, Any], *, write_rates: bool = False) -> int:
    """Make a known shipment an exact mirror of what Turvo returns right now.

    Deliberately *not* COALESCE: a field Turvo has cleared is cleared here too,
    so the dashboard never shows a value Turvo no longer holds. The columns in
    :data:`RATE_OWNED_COLUMNS` are excluded from the statement entirely -- a
    load is priced once, and no shipment update repeats or disturbs that.

    ``write_rates`` is the single exception, and means the caller has just run
    the rate engines against a load that had never been priced. It is never set
    for a load that already carries rates.

    Returns the number of rows affected; zero means the shipment_num is gone.
    """
    table = _table()
    _apply_optional_defaults(record)
    if write_rates:
        record.setdefault("GS_confidence_score", None)
        _normalize_covered_date(record)

    _sync_derived(record, coalesce=False)

    columns = _OVERWRITE_COLUMNS + (_RATE_ASSIGNMENTS if write_rates else ())
    assignments = ",\n        ".join(
        [f"{column} = %({key})s" for column, key in columns]
        + [
            "carrier_notes_updated_at = "
            + _carrier_notes_stamp("%(carrier_notes)s", "carrier_notes", "carrier_notes_updated_at")
        ]
    )
    query = f"""
    UPDATE {table}
    SET
        {assignments}
    WHERE shipment_num = %(shipment_num)s;
    """

    log_context = {
        "table_name": table,
        "shipment_num": record.get("shipment_num"),
        "shipment_id": record.get("shipment_id"),
        "status_value": record.get("status"),
    }

    try:
        with engine.cursor(commit=True) as cur:
            cur.execute(query, record)
            affected = cur.rowcount
    except Exception:
        logger.exception("DB overwrite_preserving_rates failed", extra=log_context)
        raise

    if affected == 0:
        logger.warning("DB overwrite skipped: no matching shipment_num", extra=log_context)
    else:
        logger.info(
            "DB overwrite_preserving_rates succeeded",
            extra={**log_context, "affected_rows": affected, "rates_written": write_rates},
        )
    return affected


@traced_function()
def shipment_id_exists(shipment_id: int) -> bool:
    """Whether the table already holds a row for this Turvo shipment id.

    Lets the worker decide whether an event is an update to a load we track --
    which is processed whatever its status -- before paying for a Turvo fetch.
    A read failure answers False, so the status filter still applies and the
    event takes the pre-existing path rather than being dropped.
    """
    try:
        with engine.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {_view()} WHERE shipment_id = %s LIMIT 1", (shipment_id,))
            return cur.fetchone() is not None
    except Exception:
        logger.exception("DB shipment_id_exists failed", extra={"shipment_id": shipment_id})
        return False


@traced_function()
def fetch_refresh_inputs(limit: int | None = None) -> list[tuple[str, int, float | None, float | None]]:
    """Covered rows the refresh job should re-pull from Turvo.

    Returns ``(shipment_num, shipment_id, gs_rate, dat_rate)``. The stored rates
    come along only so margins can be recomputed -- they are never rewritten.

    The scope is deliberately narrow. A finalized load (its route completed, so
    the completion webhook already wrote its final state) and a load older than
    ``REFRESH_MAX_AGE_DAYS`` are both excluded: re-pulling them every half hour
    costs Turvo calls and changes nothing.
    """
    settings = get_settings()

    query = f"""
    SELECT shipment_num, shipment_id, gs_rate, dat_rate
    FROM {_view()}
    WHERE shipment_id IS NOT NULL
      AND lower(coalesce(status, '')) = 'covered'
      AND covered_date IS NOT NULL
    """
    params: tuple[Any, ...] = ()

    if settings.refresh_skip_finalized:
        # A finalized load is normally done changing. The exception is one that
        # finalized without a carrier cost: accounting often enters carrier pay
        # after delivery, and that load's margins stay blank forever if the
        # refresh job stops looking. Keep re-pulling those until a cost lands.
        query += " AND (finalized_at IS NULL OR carrier_freight_cost IS NULL)"

    if settings.refresh_max_age_days > 0:
        query += " AND covered_date >= (CURRENT_DATE - %s::integer)"
        params += (settings.refresh_max_age_days,)

    query += " ORDER BY shipment_num"

    if limit is not None:
        query += " LIMIT %s"
        params += (limit,)

    with engine.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    out: list[tuple[str, int, float | None, float | None]] = []
    for row in rows:
        shipment_num, shipment_id, gs_rate, dat_rate = row[0], row[1], row[2], row[3]
        if shipment_num and shipment_id is not None:
            out.append(
                (
                    str(shipment_num),
                    int(shipment_id),
                    float(gs_rate) if gs_rate is not None else None,
                    float(dat_rate) if dat_rate is not None else None,
                )
            )
    return out


@traced_function()
def batch_update_preserving_rates(records: list[dict[str, Any]], page_size: int | None = None) -> int:
    """Apply every refresh record in one transaction.

    Deliberately omits the columns in :data:`RATE_OWNED_COLUMNS` -- the refresh
    job never calls a rate engine, so it must not overwrite what one produced.
    """
    if not records:
        return 0

    table = _table()
    settings = get_settings()

    # One lookup for the batch: the refresh job merges with COALESCE, so each
    # record's margins have to be recomputed against the costs and rates that
    # will actually sit in the row -- not against the payload alone, which
    # carries no rates at all.
    stored = fetch_rows_by_num([str(r["shipment_num"]) for r in records if r.get("shipment_num")])
    for record in records:
        _apply_optional_defaults(record)
        _sync_derived(
            record,
            coalesce=True,
            rate_margin_basis="freight",
            existing=stored.get(str(record.get("shipment_num"))) or {},
        )

    notes_stamp = _carrier_notes_stamp("%(carrier_notes)s", "carrier_notes", "carrier_notes_updated_at")
    query = f"""
    UPDATE {table}
    SET
        shipment_id = COALESCE(%(shipment_id)s, shipment_id),
        lane = COALESCE(NULLIF(%(lane)s, ''), lane),
        lane_raw = COALESCE(NULLIF(%(lane_raw)s, ''), lane_raw),
        pickup_date = COALESCE(%(pickup_date)s, pickup_date),
        delivery_date = COALESCE(%(delivery_date)s, delivery_date),
        covered_equipment = COALESCE(%(equipment)s, covered_equipment),
        equipment_raw = COALESCE(%(equipment_raw)s, equipment_raw),
        transport_mode = COALESCE(%(transport_mode)s, transport_mode),
        customer_name = COALESCE(%(customer)s, customer_name),
        carrier_name = COALESCE(%(carrier)s, carrier_name),
        carrier_am = COALESCE(%(carrier_am)s, carrier_am),
        carrier_notes = COALESCE(%(carrier_notes)s, carrier_notes),
        carrier_notes_updated_at = {notes_stamp},
        status = COALESCE(%(status)s, status),
        customer_freight_cost = COALESCE(%(customer_freight_cost)s, customer_freight_cost),
        customer_total_cost = COALESCE(%(customer_total_cost)s, customer_total_cost),
        carrier_freight_cost = COALESCE(%(carrier_freight_cost)s, carrier_freight_cost),
        carrier_total_cost = COALESCE(%(carrier_total_cost)s, carrier_total_cost),
        lh_margin = COALESCE(%(LH_margin)s, lh_margin),
        gs_margin = COALESCE(%(GS_margin)s, gs_margin),
        dat_margin = COALESCE(%(dat_margin)s, dat_margin),
        t_margin = COALESCE(%(t_margin)s, t_margin)
    WHERE shipment_num = %(shipment_num)s;
    """

    with engine.cursor(commit=True) as cur:
        execute_batch(cur, query, records, page_size=page_size or settings.refresh_batch_page_size)

    logger.info("Refresh batch written", extra={"table_name": table, "record_count": len(records)})
    return len(records)


@traced_function()
def update_notes(shipment_num: str, notes: str) -> str | None:
    """Save a dashboard user's note on a shipment.

    Stored in this database only -- it is never sent to Turvo. An empty string
    is kept as a deliberate blank, so clearing a note also hides the Turvo
    carrier note it replaced. Returns the stored note, or None when the
    shipment is not in the table.
    """
    query = f"""
    UPDATE {_table()}
    SET notes = %s, notes_updated_at = NOW()
    WHERE shipment_num = %s
    RETURNING notes;
    """
    with engine.cursor(commit=True) as cur:
        cur.execute(query, (notes, shipment_num))
        row = cur.fetchone()

    logger.info(
        "Shipment notes saved" if row else "Shipment notes not saved: no matching shipment_num",
        extra={"shipment_num": shipment_num, "notes_length": len(notes)},
    )
    return row[0] if row else None


@traced_function()
def fetch_rows_by_num(shipment_nums: list[str]) -> dict[str, dict[str, Any]]:
    """Stored rows for a batch, keyed by shipment_num.

    A read failure answers with what it has -- an empty mapping at worst --
    because a write must not be lost over a lookup that was only there to keep
    derived columns honest.
    """
    if not shipment_nums:
        return {}
    try:
        with engine.cursor(dict_rows=True) as cur:
            cur.execute(
                f"SELECT * FROM {_view()} WHERE shipment_num = ANY(%s)",
                (list(shipment_nums),),
            )
            return {str(row["shipment_num"]): dict(row) for row in cur.fetchall()}
    except Exception:
        logger.exception("DB fetch_rows_by_num failed", extra={"record_count": len(shipment_nums)})
        return {}


@traced_function()
def fetch_row(shipment_num: str) -> dict[str, Any] | None:
    """The stored row for a shipment, or None when it is not in the table.

    Used to diff a freshly built record against what is already there, so an
    unrelated Turvo edit does not trigger a pointless rewrite.
    """
    try:
        with engine.cursor(dict_rows=True) as cur:
            cur.execute(f"SELECT * FROM {_view()} WHERE shipment_num = %s LIMIT 1", (shipment_num,))
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception:
        # A read failure must not stop the write path; the caller falls back to
        # writing unconditionally, which is the old behaviour.
        logger.exception("DB fetch_row failed", extra={"shipment_num": shipment_num})
        return None


@traced_function()
def mark_finalized(shipment_num: str) -> int:
    """Stamp a load as finished so the refresh job stops re-pulling it.

    Called once the route has completed. Idempotent -- the first stamp stands,
    so a redelivered completion webhook does not move the timestamp.
    """
    query = f"""
    UPDATE {_table()}
    SET finalized_at = COALESCE(finalized_at, NOW())
    WHERE shipment_num = %s
    """
    try:
        with engine.cursor(commit=True) as cur:
            cur.execute(query, (shipment_num,))
            affected = cur.rowcount
    except Exception:
        logger.exception("DB mark_finalized failed", extra={"shipment_num": shipment_num})
        raise

    logger.info("Shipment marked finalized", extra={"shipment_num": shipment_num, "affected_rows": affected})
    return affected
