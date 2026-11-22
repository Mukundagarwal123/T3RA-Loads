"""Periodic refresh of covered loads from Turvo.

Re-reads every covered shipment and updates its facts and margins. It never
calls a rate engine, so ``gs_rate``, ``dat_rate``, ``gs_confidence_score`` and
``covered_date`` are left exactly as the webhook path wrote them.

Runs once per invocation and exits. Scheduling belongs to systemd/ECS/cron, not
to a loop inside the process -- see docs/runbook.md.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from t3ra_unified.clients import turvo
from t3ra_unified.config import get_settings
from t3ra_unified.db.engine import DatabaseUnavailable
from t3ra_unified.covered import repository as repo
from t3ra_unified.db import job_status
from t3ra_unified.completion.extractors import (
    compute_carrier_freight_cost,
    compute_customer_freight_cost,
    extract_carrier_am,
    extract_carrier_name,
    extract_totals,
    parse_date,
)
from t3ra_unified.covered.domain.extractors import (
    build_lane_raw,
    build_lane_string,
    compute_margins,
    extract_carrier_notes,
    extract_customer_name,
    extract_equipment,
    extract_stops,
    extract_transport_mode,
    normalize_equipment,
    normalize_status,
)
from t3ra_unified.covered.domain.record_diff import diff_records
from t3ra_unified.observability import configure_logfire, configure_logging, traced_function

logger = logging.getLogger(__name__)


@traced_function()
def build_record_without_rates(
    shipment: dict[str, Any],
    *,
    shipment_num: str,
    shipment_id: int,
    gs_rate: float | None,
    dat_rate: float | None,
) -> dict[str, Any]:
    """Rebuild a row from a shipment payload using rates already in the database.

    Two deliberate differences from the webhook path, both preserving this
    job's existing behaviour:

    * stops are extracted with ``strict_active=True``
    * (GS/DAT margins now use freight cost on every path -- see repository/derived)
    """
    details = shipment.get("details") or shipment

    start_date = details.get("startDate") or {}
    end_date = details.get("endDate") or {}

    stops = extract_stops(shipment, strict_active=True)
    carrier_orders = details.get("carrierOrder") or []
    customer_orders = details.get("customerOrder") or []


    # Same cost rule as the webhook path and the completion phase.
    carrier_cost = compute_carrier_freight_cost(details)
    customer_cost = compute_customer_freight_cost(details)
    total_customer_cost, total_carrier_cost = extract_totals(details)

    carrier_name = extract_carrier_name(details)
    carrier_am = extract_carrier_am(details)

    lh_margin, gs_margin, dat_margin, t_margin = compute_margins(
        customer_cost,
        carrier_cost,
        total_customer_cost,
        total_carrier_cost,
        gs_rate,
        dat_rate,
        rate_margin_basis="freight",
    )

    equipment_raw = extract_equipment(details)

    return {
        "shipment_num": shipment_num,
        "shipment_id": shipment_id,
        "lane": build_lane_string(stops),
        "lane_raw": build_lane_raw(stops),
        "pickup_date": parse_date(start_date.get("date")),
        "delivery_date": parse_date(end_date.get("date")),
        "equipment": normalize_equipment(equipment_raw),
        "equipment_raw": equipment_raw,
        "transport_mode": extract_transport_mode(details),
        "customer": extract_customer_name(customer_orders),
        "carrier": carrier_name,
        "carrier_am": carrier_am,
        "carrier_notes": extract_carrier_notes(carrier_orders),
        "status": normalize_status(details.get("status")),
        "customer_freight_cost": customer_cost,
        "customer_total_cost": total_customer_cost,
        "carrier_freight_cost": carrier_cost,
        "carrier_total_cost": total_carrier_cost,
        "LH_margin": lh_margin,
        "GS_margin": gs_margin,
        "dat_margin": dat_margin,
        "t_margin": t_margin,
    }


@traced_function()
def run_refresh(limit: int | None = None) -> dict[str, int]:
    """Execute one refresh pass. Returns ``{updated, succeeded, failed}``.

    A Turvo failure on one shipment is counted and skipped; the batch still
    writes every shipment that did resolve.
    """
    settings = get_settings()
    started_at = datetime.now(timezone.utc)
    next_run_at = started_at + timedelta(minutes=settings.refresh_interval_minutes)

    job_status.upsert_job_status(
        job_name=settings.refresh_job_name,
        status="running",
        started_at=started_at,
        next_run_at=next_run_at,
        message="Refresh in progress",
        updated_rows=0,
        success_count=0,
        failure_count=0,
    )

    try:
        inputs = repo.fetch_refresh_inputs(limit=limit)
        logger.info("Refresh inputs loaded", extra={"row_count": len(inputs)})

        records: list[dict[str, Any]] = []
        succeeded = 0
        failed = 0
        unchanged = 0

        for shipment_num, shipment_id, gs_rate, dat_rate in inputs:
            try:
                shipment = turvo.fetch_shipment_details(shipment_id)
                record = build_record_without_rates(
                    shipment,
                    shipment_num=shipment_num,
                    shipment_id=shipment_id,
                    gs_rate=gs_rate,
                    dat_rate=dat_rate,
                )
                # Most refreshed loads come back identical. Writing them anyway
                # is a wasted UPDATE per load per half hour, so only changed
                # records reach the batch.
                if not diff_records(repo.fetch_row(shipment_num), record):
                    unchanged += 1
                else:
                    records.append(record)
                succeeded += 1
            except Exception as exc:
                failed += 1
                logger.exception(
                    "Refresh failed for shipment",
                    extra={
                        "shipment_num": shipment_num,
                        "shipment_id": shipment_id,
                        "error_preview": repr(exc)[:500],
                    },
                )

        updated = repo.batch_update_preserving_rates(records)
        finished_at = datetime.now(timezone.utc)

        job_status.upsert_job_status(
            job_name=settings.refresh_job_name,
            status="completed",
            started_at=started_at,
            finished_at=finished_at,
            last_success_at=finished_at,
            next_run_at=finished_at + timedelta(minutes=settings.refresh_interval_minutes),
            message=f"Refresh completed successfully ({unchanged} unchanged)",
            updated_rows=updated,
            success_count=succeeded,
            failure_count=failed,
        )

        logger.info(
            "Refresh completed",
            extra={
                "updated_rows": updated,
                "success_count": succeeded,
                "failure_count": failed,
                "unchanged_count": unchanged,
            },
        )
        return {"updated": updated, "succeeded": succeeded, "failed": failed, "unchanged": unchanged}

    except Exception as exc:
        finished_at = datetime.now(timezone.utc)
        job_status.upsert_job_status(
            job_name=settings.refresh_job_name,
            status="failed",
            started_at=started_at,
            finished_at=finished_at,
            next_run_at=finished_at + timedelta(minutes=settings.refresh_interval_minutes),
            message=repr(exc)[:2000],
            updated_rows=0,
            success_count=0,
            failure_count=1,
        )
        logger.exception("Refresh job failed")
        raise


def main() -> None:
    """Console entry point: ``covered-loads-refresh``."""
    configure_logging(service_name="covered-loads-refresh")
    configure_logfire(service_name="covered-loads-refresh")
    logger.info("Refresh job starting", extra=get_settings().safe_summary())

    try:
        run_refresh()
    except DatabaseUnavailable as exc:
        # Connectivity is an operator problem, not a bug; say so plainly and
        # exit non-zero so a scheduler records the failure.
        raise SystemExit(f"\nDatabase unavailable.\n{exc}\n") from exc


if __name__ == "__main__":
    main()
