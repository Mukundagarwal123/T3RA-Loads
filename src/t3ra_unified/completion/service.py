"""Completion phase: what happens once a shipment is route complete / completed.

This is the former T3RA worker's per-shipment body, unchanged in behaviour:
build the record, resolve both ends to DAT market areas, upsert the row, then
add the carrier to ``carriers`` if it is new.
"""

from __future__ import annotations

import logging
from typing import Any

from t3ra_unified.clients.turvo import TurvoAuthError, fetch_carrier_details
from t3ra_unified.completion import kma
from t3ra_unified.completion.carrier_processor import build_carrier_record
from t3ra_unified.completion.processor import build_record
from t3ra_unified.completion.repository import carrier_exists, upsert_carriers, upsert_shipment

logger = logging.getLogger(__name__)


def sync_carrier(carrier_id: int) -> None:
    """Add the carrier to our carriers table if we haven't seen it before."""
    try:
        if carrier_exists(carrier_id):
            logger.info("Carrier already on file | carrier_id=%s", carrier_id)
            return

        logger.info("New carrier detected, fetching from Turvo | carrier_id=%s", carrier_id)
        carrier_resp = fetch_carrier_details(carrier_id)
        carrier_details = carrier_resp.get("details") or carrier_resp
        carrier_record = build_carrier_record(carrier_details)
        upsert_carriers([carrier_record])
        logger.info("Carrier added | carrier_id=%s name=%s", carrier_id, carrier_record.get("name"))
    except TurvoAuthError:
        # Never swallowed: the caller has to stop working, not carry on.
        raise
    except Exception:
        # Carrier sync is a side effect of shipment processing; a failure here
        # shouldn't fail/retry the whole shipment event.
        logger.exception("Carrier sync FAILED | carrier_id=%s", carrier_id)


def process_completed_shipment(shipment: dict[str, Any]) -> dict[str, Any]:
    """Store a finished shipment's completion-phase facts. Returns the record written."""
    record = build_record(shipment)
    # Resolves both ZIPs to DAT market areas. Never raises: a market is
    # advisory, and losing a whole shipment event over one would be absurd.
    kma.annotate_record(record)
    record["carrier_id_source"] = "turvo" if record.get("carrier_id") is not None else None
    logger.info("Record built | shipment_num=%s shipment_id=%s", record.get("shipment_num"), record.get("shipment_id"))

    upsert_shipment(record)

    carrier_id = record.get("carrier_id")
    if carrier_id is not None:
        sync_carrier(carrier_id)
    else:
        logger.warning("No carrier id found on shipment | shipment_id=%s", record.get("shipment_id"))
    return record
