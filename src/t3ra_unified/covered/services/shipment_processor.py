"""Turn one Turvo shipment into a persisted covered-loads row.

This is the webhook path. It is the only place that calls the rate engines, and
therefore the only place allowed to write ``gs_rate``, ``dat_rate``,
``gs_confidence_score`` and ``covered_date``.

Two flows meet here. A shipment the table has never seen is classified by its
status and priced if it qualifies. A shipment already on the dashboard is
instead *mirrored*: whatever Turvo returns now is written over the stored row
whole, whatever the status and whether or not anything appears to have changed,
because Turvo is the source of truth for every fact the dashboard shows. Its
rates are the one exception -- they were paid for once and are carried forward.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from t3ra_unified.covered import repository as repo
from t3ra_unified.completion.extractors import (
    compute_carrier_freight_cost,
    compute_customer_freight_cost,
    extract_carrier_am,
    extract_carrier_name,
    extract_totals,
    parse_date,
)
from t3ra_unified.covered.domain.derived import recompute_derived
from t3ra_unified.covered.domain.extractors import (
    build_lane_raw,
    build_lane_string,
    compute_margins,
    extract_carrier_notes,
    extract_customer_name,
    extract_equipment,
    extract_stops,
    extract_transport_mode,
    filter_carrier_orders,
    mmddyyyy_to_iso_date,
    normalize_equipment,
    to_float,
)
from t3ra_unified.covered.domain.rate_engines import fill_missing_zips, run_dat, run_gs
from t3ra_unified.covered.domain.record_diff import diff_records
from t3ra_unified.covered.domain.rules import dat_allows_equipment, gs_allows_equipment, should_run_rates
from t3ra_unified.observability import traced_function

logger = logging.getLogger(__name__)

#: Statuses that trigger an update using rates already stored, rather than a
#: fresh pricing run.
LATE_STATUSES = frozenset({"route complete", "processing", "completed"})

#: Statuses that mean the load is done. Reaching one is the last refresh it
#: gets -- afterwards ``finalized_at`` keeps the refresh job away from it.
FINAL_STATUSES = frozenset({"route complete", "completed"})

#: Fields a covered load needs for the dashboard to be worth anything. A
#: missing one is logged by name rather than showing up later as a blank cell.
REQUIRED_FIELDS = ("shipment_id", "lane", "equipment", "carrier", "pickup_date")


def _log_missing_fields(record: dict[str, Any], log_context: dict[str, Any]) -> list[str]:
    """Name every required field this record is missing, and return them.

    Nothing is dropped on account of a missing field -- a covered load belongs
    on the dashboard even when Turvo has not filled the carrier in yet, and a
    later event will populate it. The log is how we notice it never did.
    """
    missing = [field for field in REQUIRED_FIELDS if not record.get(field)]
    if missing:
        logger.warning(
            "Shipment is missing required fields",
            extra={**log_context, "missing_fields": missing, "outcome": "kept"},
        )
    return missing


def _today_mmddyyyy() -> str:
    return datetime.now().strftime("%m/%d/%Y")


@traced_function()
def build_final_record(
    shipment: dict[str, Any],
    status_value: str | None,
    *,
    force_skip_rates: bool = False,
    covered_date: str | None = None,
) -> dict[str, Any]:
    """Flatten a shipment into the row shape, optionally pricing it.

    ``covered_date`` (mm/dd/yyyy) doubles as the Greenscreens pickup date when
    present, so a shipment is priced as of the day it was covered rather than
    the day the webhook happened to arrive.
    """
    details = shipment.get("details") or {}
    shipment_id = details.get("id") or shipment.get("id")
    shipment_num = details.get("customId") or "UNKNOWN"

    transport_mode = extract_transport_mode(details)
    ltl_flag = details.get("ltlShipment")

    start_date = details.get("startDate") or {}
    end_date = details.get("endDate") or {}
    pickup_date_gs = mmddyyyy_to_iso_date(covered_date) or start_date.get("date")
    pickup_date_db = parse_date(start_date.get("date"))
    delivery_date_db = parse_date(end_date.get("date"))

    stops = extract_stops(shipment)
    lane = build_lane_string(stops)
    lane_raw = build_lane_raw(stops)
    equipment_raw = extract_equipment(details)
    equipment = normalize_equipment(equipment_raw)

    carrier_orders = filter_carrier_orders(details.get("carrierOrder") or [])
    customer_orders = details.get("customerOrder") or []

    customer_name = extract_customer_name(customer_orders)
    # Shared columns follow the T3RA rule, so both lifecycle phases store the same value.
    carrier_name = extract_carrier_name(details)
    carrier_am = extract_carrier_am(details)


    # The two costs the dashboard shows per side. "Cost" is the line haul --
    # the freight-flat charge alone, no layover or other accessorial -- because
    # every GS/DAT diff and the LH margin are measured on it. "Total cost" is
    # Turvo's own order total (``costs.totalAmount``), so it carries every
    # accessorial and matches the shipment in Turvo.
    #
    # A side Turvo priced at nothing stays None rather than becoming 0: the
    # write path must not overwrite a stored cost with a zero, and a margin
    # against a cost nobody entered is not a 100% margin.
    carrier_cost = compute_carrier_freight_cost(details)
    customer_cost = compute_customer_freight_cost(details)
    total_customer_cost, total_carrier_cost = extract_totals(details)

    log_context = {
        "shipment_num": shipment_num,
        "shipment_id": shipment_id,
        "status_value": status_value,
        "transport_mode": transport_mode,
        "equipment": equipment,
        "equipment_raw": equipment_raw,
        "stops_count": len(stops),
        "ltl_flag": ltl_flag,
    }
    logger.info("Shipment facts extracted", extra=log_context)

    if force_skip_rates:
        can_rate, reason = False, "forced skip"
    else:
        can_rate, reason = should_run_rates(
            status_value=status_value,
            transport_mode=transport_mode,
            ltl_flag=ltl_flag,
            equipment=equipment,
            stops_count=len(stops),
            pickup_date_gs=pickup_date_gs,
        )

    logger.info(
        "Rates rule matched: RUN rates" if can_rate else "Rates skipped by rule",
        extra={**log_context, "reason": reason},
    )

    gs_rate: float | None = None
    gs_confidence: float | None = None
    dat_rate: float | None = None

    if can_rate:
        stops = fill_missing_zips(stops)
        if gs_allows_equipment(equipment):
            gs_rate, gs_confidence = run_gs(equipment, pickup_date_gs, stops)
            logger.info(
                "GS rate fetched" if gs_rate is not None else "GS rate unavailable",
                extra={**log_context, "gs_rate": gs_rate, "gs_confidence_score": gs_confidence},
            )

        if dat_allows_equipment(equipment):
            if len(stops) != 2:
                logger.info("DAT skipped due to stops count", extra=log_context)
            else:
                dat_rate = run_dat(stops, equipment)
                logger.info(
                    "DAT rate fetched" if dat_rate is not None else "DAT rate unavailable",
                    extra={**log_context, "dat_rate": dat_rate},
                )

    lh_margin, gs_margin, dat_margin, t_margin = compute_margins(
        customer_cost,
        carrier_cost,
        total_customer_cost,
        total_carrier_cost,
        gs_rate,
        dat_rate,
        rate_margin_basis="freight",
    )

    return {
        "shipment_num": shipment_num,
        "shipment_id": shipment_id,
        "lane": lane,
        "lane_raw": lane_raw,
        "pickup_date": pickup_date_db,
        "delivery_date": delivery_date_db,
        "equipment": equipment,
        "equipment_raw": equipment_raw,
        "transport_mode": transport_mode,
        "customer": customer_name,
        "carrier": carrier_name,
        "carrier_am": carrier_am,
        "carrier_notes": extract_carrier_notes(carrier_orders),
        "status": status_value,
        "customer_freight_cost": customer_cost,
        "customer_total_cost": total_customer_cost,
        "carrier_freight_cost": carrier_cost,
        "carrier_total_cost": total_carrier_cost,
        "GS_rate": gs_rate,
        "GS_confidence_score": gs_confidence,
        "dat_rate": dat_rate,
        "LH_margin": lh_margin,
        "GS_margin": gs_margin,
        "dat_margin": dat_margin,
        "t_margin": t_margin,
        "covered_date": covered_date,
    }


def _recompute_margins_from_stored_rates(record: dict[str, Any], gs_rate: Any, dat_rate: Any) -> None:
    """Overlay stored rates onto a record and recompute only the rate margins.

    Mutates ``record`` in place. Used when a shipment reaches a late status:
    the facts are refreshed, but we must not pay for pricing a second time.
    """
    gs = to_float(gs_rate)
    dat = to_float(dat_rate)

    record["GS_rate"] = gs
    record["dat_rate"] = dat
    record["GS_confidence_score"] = None
    record["covered_date"] = None

    # Recomputed through the one shared definition rather than restated here.
    # The restatement this replaces guarded only on ``is not None``, so a
    # carrier cost of 0 produced a 100% GS margin instead of no margin at all.
    recompute_derived(record, coalesce=False, rate_margin_basis="freight")


@traced_function()
def process_shipment_event(shipment: dict[str, Any], status_value: str | None) -> dict[str, Any]:
    """Persist a shipment according to its status. Empty dict means skipped."""
    status = (status_value or "").strip().lower()

    base_record = build_final_record(shipment, status_value, force_skip_rates=True, covered_date=None)
    shipment_num = base_record["shipment_num"]
    existing = repo.fetch_row(shipment_num)

    log_context = {
        "shipment_num": shipment_num,
        "shipment_id": base_record.get("shipment_id"),
        "status_value": status_value,
        "exists_in_db": existing is not None,
    }
    logger.info("Shipment identity resolved", extra=log_context)
    _log_missing_fields(base_record, log_context)

    if existing is not None:
        return _mirror_existing_shipment(
            shipment, status_value, status, base_record, existing, log_context
        )

    return _store_new_shipment(shipment, status_value, status, log_context)


def _mirror_existing_shipment(
    shipment: dict[str, Any],
    status_value: str | None,
    status: str,
    base_record: dict[str, Any],
    existing: dict[str, Any],
    log_context: dict[str, Any],
) -> dict[str, Any]:
    """Overwrite a known shipment with Turvo's current state, rates aside.

    Unconditional by design: no status filter and no change detection. Every
    shipment update Turvo sends for a load we already track is a statement of
    what that load looks like now, and the dashboard is expected to agree with
    it -- including when a field has been cleared on Turvo's side.

    The one thing carried forward rather than rewritten is the pricing. A load
    is priced once, on the day it was covered, and no later update re-runs the
    engines. The exception is a covered load that was never priced at all --
    ``covered_date`` is the record of the attempt, so its absence means the
    engines have not run yet and this update is their chance to.
    """
    shipment_num = base_record["shipment_num"]
    changes = diff_records(existing, base_record)

    never_priced = status == "covered" and not existing.get("covered_date")

    if never_priced:
        record = build_final_record(
            shipment,
            status_value,
            force_skip_rates=False,
            covered_date=_today_mmddyyyy(),
        )
    else:
        record = base_record
        _recompute_margins_from_stored_rates(record, existing.get("gs_rate"), existing.get("dat_rate"))

    repo.overwrite_preserving_rates(record, write_rates=never_priced)

    if not never_priced:
        # Reported back to the caller; the stored values were never touched.
        record["GS_rate"] = to_float(existing.get("gs_rate"))
        record["dat_rate"] = to_float(existing.get("dat_rate"))
        record["GS_confidence_score"] = existing.get("gs_confidence_score")
        record["covered_date"] = existing.get("covered_date")

    logger.info(
        "Shipment overwritten from Turvo",
        extra={
            **log_context,
            "db_action": "overwrite_preserving_rates",
            "changed_fields": sorted(changes),
            "priced": never_priced,
        },
    )

    if status in FINAL_STATUSES:
        # The route is done: this was the final refresh.
        repo.mark_finalized(shipment_num)

    return record


def _store_new_shipment(
    shipment: dict[str, Any],
    status_value: str | None,
    status: str,
    log_context: dict[str, Any],
) -> dict[str, Any]:
    """First sighting of a shipment: classify it by status, and price it."""
    if status == "covered":
        record = build_final_record(
            shipment,
            status_value,
            force_skip_rates=False,
            covered_date=_today_mmddyyyy(),
        )
        repo.upsert_final_record(record)
        logger.info(
            "Shipment persisted for covered status",
            extra={**log_context, "db_action": "upsert", "repriced": True},
        )
        return record

    if status in LATE_STATUSES:
        record = build_final_record(shipment, status_value, force_skip_rates=False, covered_date=None)
        repo.upsert_final_record(record)
        if status in FINAL_STATUSES:
            repo.mark_finalized(record["shipment_num"])
        logger.info(
            "Shipment persisted from late-status new shipment flow",
            extra={**log_context, "db_action": "upsert"},
        )
        return record

    logger.warning("Shipment skipped due to unhandled status", extra={**log_context, "outcome": "skipped"})
    return {}
