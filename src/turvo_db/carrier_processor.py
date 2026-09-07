import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _pick_primary_phone(phones: List[dict]) -> Tuple[Optional[str], Optional[str]]:
    candidates = [p for p in phones if p.get("deleted") is not True]
    if not candidates:
        return None, None

    # Multiple entries can have isPrimary=true (e.g. Main + Fax), so prefer
    # the "Main" type first before falling back to isPrimary / first entry.
    for p in candidates:
        if (p.get("type") or {}).get("value") == "Main":
            return p.get("phone"), (p.get("country") or {}).get("value")
    for p in candidates:
        if p.get("isPrimary"):
            return p.get("phone"), (p.get("country") or {}).get("value")

    p = candidates[0]
    return p.get("phone"), (p.get("country") or {}).get("value")


def _pick_primary_email(emails: List[dict]) -> Optional[str]:
    candidates = [e for e in emails if e.get("deleted") is not True]
    if not candidates:
        return None

    for e in candidates:
        if (e.get("type") or {}).get("value") == "Main":
            return e.get("email")
    for e in candidates:
        if e.get("isPrimary"):
            return e.get("email")

    return candidates[0].get("email")


def build_carrier_record(details: Dict[str, Any]) -> Dict[str, Any]:
    phone, country_code = _pick_primary_phone(details.get("phone") or [])
    email = _pick_primary_email(details.get("email") or [])

    record = {
        "carrier_id": details.get("id"),
        "name": details.get("name"),
        "email": email,
        "country_code": country_code,
        "phone": phone,
        "mc": details.get("mcNumber"),
    }
    logger.info("Carrier record built | carrier_id=%s name=%s", record["carrier_id"], record["name"])
    return record
