import logging
from typing import Any, Dict

from turvo_db.extractors import (
    compute_carrier_freight_cost,
    compute_customer_freight_cost,
    extract_carrier_id,
    extract_carrier_name,
    extract_customer_name,
    extract_equipment,
    extract_stops,
    extract_totals,
    normalize_equipment,
    parse_date,
)

logger = logging.getLogger(__name__)


def _lower(s):
    return s.strip().lower() if isinstance(s, str) else s


def build_record(shipment: Dict[str, Any]) -> Dict[str, Any]:
    details = shipment.get("details") or shipment

    shipment_num = details.get("customId") or "UNKNOWN"
    shipment_id = details.get("id")

    equipment = normalize_equipment(extract_equipment(details))

    stops = extract_stops(details)
    origin = stops[0] if stops else {}
    destination = stops[-1] if stops else {}

    start_date_obj = details.get("startDate") or {}
    end_date_obj = details.get("endDate") or {}

    customer_total_cost, carrier_total_cost = extract_totals(details)

    record = {
        "shipment_num": shipment_num,
        "shipment_id": shipment_id,
        "equipment": equipment,
        "origin_city": _lower(origin.get("city")),
        "origin_state": _lower(origin.get("state")),
        "origin_zip": origin.get("zip"),
        "destination_city": _lower(destination.get("city")),
        "destination_state": _lower(destination.get("state")),
        "destination_zip": destination.get("zip"),
        "total_stops": len(stops),
        "pickup_date": parse_date(start_date_obj.get("date")),
        "delivery_date": parse_date(end_date_obj.get("date")),
        "customer_name": extract_customer_name(details),
        "carrier_name": extract_carrier_name(details),
        # Stored as well as used, so loads can be joined to carriers by id.
        # Matching on carrier_name alone breaks on "ABC Trucking LLC" versus
        # "ABC Trucking, LLC", and every per-carrier report depends on the join.
        "carrier_id": extract_carrier_id(details),
        "customer_freight_cost": compute_customer_freight_cost(details),
        "carrier_freight_cost": compute_carrier_freight_cost(details),
        "customer_total_cost": customer_total_cost,
        "carrier_total_cost": carrier_total_cost,
    }

    logger.info("Record built | shipment_num=%s shipment_id=%s", shipment_num, shipment_id)
    return record
