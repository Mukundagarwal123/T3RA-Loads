from datetime import date, datetime, timezone
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


def parse_date(iso_ts: Optional[str]) -> Optional[date]:
    """A Turvo timestamp as a real date.

    These used to be stored as MM/DD/YYYY text, which Postgres compares as
    strings: the month is weighed first and the year last, so 01/03/2023 sorted
    before 12/30/2022 and min()/max() returned the first and last strings rather
    than the first and last loads. Recency is the whole basis of the market
    scoring, so the dates are converted here, once, at the point the shipment
    arrives.
    """
    if not iso_ts:
        return None
    text = str(iso_ts).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except Exception:
        pass
    # Fractional seconds of an unusual length (".12", ".123456789") are rejected by
    # fromisoformat before Python 3.11. The calendar date is the first ten characters
    # of any ISO-8601 timestamp, so fall back to that rather than losing the date.
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
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


def _carrier_orders_excluding_lumper(details: Dict[str, Any]) -> List[dict]:
    """Carrier orders that represent an actual haul.

    "Lumper Advance" is a fee line, not a trucking company - it has its own
    account (7028286) purely so the charge can be billed. Both of the lookups
    below used to fall back to carrierOrder[0] when they found nothing else,
    which on a shipment whose only order was the lumper fee stored the lumper as
    the carrier. That put 45 loads under "Lumper Advance runs Stockton to
    Stockton", and nothing downstream could tell it was nonsense.

    Returning nothing is the right answer there: a blank is visibly missing, a
    fee line dressed as a carrier is invisibly wrong.
    """
    orders = details.get("carrierOrder") or []
    live = [co for co in orders if co.get("deleted") is False]
    candidates = live or orders
    return [co for co in candidates
            if (co.get("carrier") or {}).get("name") != LUMPER_CARRIER_NAME]


def extract_carrier_name(details: Dict[str, Any]) -> Optional[str]:
    for co in _carrier_orders_excluding_lumper(details):
        name = (co.get("carrier") or {}).get("name")
        if name:
            return name
    return None


def extract_carrier_id(details: Dict[str, Any]) -> Optional[int]:
    for co in _carrier_orders_excluding_lumper(details):
        carrier_id = (co.get("carrier") or {}).get("id")
        if carrier_id:
            return carrier_id
    return None


def extract_carrier_am(details: Dict[str, Any]) -> Optional[str]:
    """The carrier's account manager - the Turvo owner of the carrier account.

    Read off the same carrier order extract_carrier_name picks, so the AM always
    belongs to the carrier stored on the row rather than to a lumper fee line or
    a second carrier further down the list.
    """
    for co in _carrier_orders_excluding_lumper(details):
        carrier = co.get("carrier") or {}
        if carrier.get("name"):
            return (carrier.get("owner") or {}).get("name")
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


def _main_carrier_order(details: Dict[str, Any]) -> Optional[dict]:
    """The carrier order that represents the haul: first live, non-lumper order.

    Falls back to the first order when none qualifies, as T3RA always has.
    """
    carrier_orders = details.get("carrierOrder") or []
    if not carrier_orders:
        return None
    for co in carrier_orders:
        if co.get("deleted") is False and (co.get("carrier") or {}).get("name") != LUMPER_CARRIER_NAME:
            return co
    return carrier_orders[0]


def compute_customer_freight_cost(details: Dict[str, Any]) -> Optional[float]:
    """customerOrder[0]'s latest ``freight-flat`` line. No layover, no accessorials.

    One rule for the whole shipment lifecycle (Covered phase and completion
    phase write the same value). None when Turvo has no freight-flat line yet:
    a missing cost must stay missing so margins are not computed against it.
    """
    customer_orders = details.get("customerOrder") or []
    if not customer_orders:
        return None
    line_items = ((customer_orders[0].get("costs") or {}).get("lineItem")) or []
    return _latest_freight_amount(line_items)


def compute_carrier_freight_cost(details: Dict[str, Any]) -> Optional[float]:
    """The main carrier order's latest ``freight-flat`` line. No layover, no lumper."""
    main_order = _main_carrier_order(details)
    if main_order is None:
        return None
    line_items = ((main_order.get("costs") or {}).get("lineItem")) or []
    return _latest_freight_amount(line_items)


def extract_totals(details: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    margin = details.get("margin") or {}
    customer_total = margin.get("totalReceivableAmount")
    carrier_total = margin.get("totalPayableAmount")
    return customer_total, carrier_total
