from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

LUMPER_CARRIER_NAME = "Lumper Advance"


def _norm_cost_code(s: Optional[str]) -> str:
    if not s:
        return ""
    s = str(s).strip().lower()
    s = s.replace("–", "-").replace("—", "-")
    s = " ".join(s.split())
    s = s.replace(" - ", "-").replace("- ", "-").replace(" -", "-")
    return s


def _parse_timestamp(li: dict) -> datetime:
    ts = li.get("lastUpdatedOn") or li.get("date")
    if not ts:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def format_mmddyyyy(iso_ts: Optional[str]) -> Optional[str]:
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dt.strftime("%m/%d/%Y")
    except Exception:
        return None


def extract_equipment(details: Dict[str, Any]) -> Optional[str]:
    for eq in details.get("equipment") or []:
        if eq.get("deleted") is False:
            return (eq.get("type") or {}).get("value")
    return None


def normalize_equipment(equipment_value: Optional[str]) -> Optional[str]:
    if not equipment_value:
        return None
    value = str(equipment_value).lower()
    if "van" in value:
        return "Van"
    if "reefer" in value or "ref" in value:
        return "Reefer"
    return equipment_value


def extract_stops(details: Dict[str, Any]) -> List[dict]:
    """
    Stops come from customerOrder[0].route[] (not the top-level globalRoute),
    because globalRoute addresses do not carry a zip code.
    """
    customer_orders = details.get("customerOrder") or []
    if not customer_orders:
        return []

    route = customer_orders[0].get("route") or []
    stops = []
    for idx, stop in enumerate(route, start=1):
        if stop.get("deleted") is not False:
            continue
        addr = stop.get("address") or {}
        stops.append(
            {
                "sequence": stop.get("sequence", idx),
                "city": addr.get("city"),
                "state": addr.get("state"),
                "zip": addr.get("zip"),
            }
        )
    stops.sort(key=lambda x: x.get("sequence") or 0)
    return stops


def extract_customer_name(details: Dict[str, Any]) -> Optional[str]:
    customer_orders = details.get("customerOrder") or []
    if customer_orders:
        return (customer_orders[0].get("customer") or {}).get("name")
    return None


def extract_carrier_name(details: Dict[str, Any]) -> Optional[str]:
    carrier_orders = details.get("carrierOrder") or []
    for co in carrier_orders:
        if co.get("deleted") is False:
            name = (co.get("carrier") or {}).get("name")
            if name and name != LUMPER_CARRIER_NAME:
                return name
    if carrier_orders:
        name = (carrier_orders[0].get("carrier") or {}).get("name")
        if name:
            return name
    return None


def _sum_line_items(line_items: List[dict], code_match: str) -> float:
    total = 0.0
    for li in line_items:
        if li.get("deleted") is not False:
            continue
        if _norm_cost_code((li.get("code") or {}).get("value")) == code_match:
            total += float(li.get("amount") or 0)
    return total


def _latest_freight_amount(line_items: List[dict]) -> Optional[float]:
    latest_amount = None
    latest_ts = datetime.min.replace(tzinfo=timezone.utc)
    for li in line_items:
        if li.get("deleted") is not False:
            continue
        if _norm_cost_code((li.get("code") or {}).get("value")) != "freight-flat":
            continue
        ts = _parse_timestamp(li)
        if ts >= latest_ts:
            latest_ts = ts
            latest_amount = li.get("amount")
    return float(latest_amount) if latest_amount is not None else None


def compute_customer_freight_cost(details: Dict[str, Any]) -> Optional[float]:
    customer_orders = details.get("customerOrder") or []
    if not customer_orders:
        return None

    line_items = ((customer_orders[0].get("costs") or {}).get("lineItem")) or []
    freight = _latest_freight_amount(line_items) or 0.0
    layover = _sum_line_items(line_items, "accessorial-layover")
    return freight + layover


def compute_carrier_freight_cost(details: Dict[str, Any]) -> Optional[float]:
    """
    Freight-flat comes from the main carrier's own order (excluding the
    'Lumper Advance' sub-order). Layover is summed across ALL carrier
    orders, since accessorial charges can land on any sub-order.
    """
    carrier_orders = details.get("carrierOrder") or []
    if not carrier_orders:
        return None

    main_order = None
    for co in carrier_orders:
        if co.get("deleted") is False and (co.get("carrier") or {}).get("name") != LUMPER_CARRIER_NAME:
            main_order = co
            break
    if main_order is None:
        main_order = carrier_orders[0]

    main_line_items = ((main_order.get("costs") or {}).get("lineItem")) or []
    freight = _latest_freight_amount(main_line_items) or 0.0

    layover_total = 0.0
    for co in carrier_orders:
        line_items = ((co.get("costs") or {}).get("lineItem")) or []
        layover_total += _sum_line_items(line_items, "accessorial-layover")

    return freight + layover_total


def extract_totals(details: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    margin = details.get("margin") or {}
    customer_total = margin.get("totalReceivableAmount")
    carrier_total = margin.get("totalPayableAmount")
    return customer_total, carrier_total
