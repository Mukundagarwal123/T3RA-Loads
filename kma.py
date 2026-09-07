"""Resolve a postal code to a DAT market area (KMA).

The one implementation of this rule. Ingest (worker.py) and the historical
backfill both go through here, so the two can never disagree - a second version
written in SQL for the backfill would diverge on exactly the Canadian case,
which is the case that fails silently.

Kept out of extractors.py deliberately: everything there is pure and does no
I/O, and this reads the database.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import db

logger = logging.getLogger(__name__)

# Loaded once per process. ~1,100 rows, so the memory is irrelevant and the
# webhook path gains no per-shipment query.
_PREFIX_MAP: Optional[Dict[str, str]] = None

# Canadian postal codes open letter-digit-letter (M5V 3A8). US ZIPs never
# contain a letter at all, which is what makes the two safely separable.
_CANADIAN_RE = re.compile(r"^[A-Z]\d[A-Z]")


def normalize_postal(value: Optional[str]) -> Optional[Tuple[str, str]]:
    """A raw postal code -> (lookup key, country), or None if it isn't one.

    Order matters. Canadian codes are recognised *before* digits are extracted:
    stripping non-digits from "M5V 3A8" leaves "538", which is a real US ZIP
    prefix, so a naive rule maps Toronto freight into a US market and nothing
    anywhere reports an error.
    """
    if not isinstance(value, str):
        return None

    cleaned = re.sub(r"[\s\-]", "", value).upper()
    if not cleaned:
        return None

    if _CANADIAN_RE.match(cleaned):
        return cleaned[:3], "CA"

    if cleaned.isdigit() and len(cleaned) in (5, 9):
        # 5 = ZIP, 9 = ZIP+4 with the dash already removed. Shorter inputs are
        # rejected rather than padded: a 4-digit value is truncation damage, and
        # guessing at it would map the load somewhere confidently wrong.
        return cleaned[:3], "US"

    return None


def load_prefix_map(force: bool = False) -> Dict[str, str]:
    """The prefix -> market table, read once and cached for the process.

    Raises if the read fails, deliberately. Returning an empty map instead would
    be indistinguishable from "no market matched", so annotate_record would
    stamp the row as looked-at-and-unmappable and the backfill would then skip
    it forever. Let it raise; annotate_record catches it and leaves the row
    unstamped for the backfill to heal.

    A failure is also never cached, so the next shipment retries the read rather
    than inheriting a broken map for the life of the process - which on a
    long-running worker could be weeks.
    """
    global _PREFIX_MAP
    if _PREFIX_MAP is not None and not force:
        return _PREFIX_MAP

    prefix_map = db.fetch_kma_prefix_map()
    _PREFIX_MAP = prefix_map
    logger.info("KMA prefix map loaded | prefixes=%d", len(prefix_map))
    return _PREFIX_MAP


def lookup_market(postal: Optional[str]) -> Optional[str]:
    """The market id for a postal code, or None if it maps to nothing.

    Canadian markets are defined at mixed granularity - BC is split between
    Vancouver and Prince George by full three-character FSA, every other
    province is grouped by the first two - so the lookup tries the longer key
    first and falls back to the shorter one.
    """
    parsed = normalize_postal(postal)
    if parsed is None:
        return None

    key, country = parsed
    prefix_map = load_prefix_map()

    if country == "CA":
        return prefix_map.get(key) or prefix_map.get(key[:2])
    return prefix_map.get(key)


def annotate_record(record: dict) -> dict:
    """Add origin_kma, destination_kma and kma_mapped_at to a shipment record.

    Never raises. The try/except lives here rather than at the call site so that
    every future caller is safe by construction - an exception escaping into
    worker.process_one would fail the queue event and retry the whole shipment
    over a lookup that is only ever advisory.

    The stamp is deliberately asymmetric: it is set when the lookup ran (even if
    both ends legitimately mapped to nothing) and left unset when the lookup
    itself broke. That is what keeps "we looked, there is no market here" apart
    from "we never managed to look", so backfill_kma.py can heal the second
    without re-treading the first.
    """
    try:
        record["origin_kma"] = lookup_market(record.get("origin_zip"))
        record["destination_kma"] = lookup_market(record.get("destination_zip"))
        record["kma_mapped_at"] = datetime.now(timezone.utc)
    except Exception:
        record["origin_kma"] = None
        record["destination_kma"] = None
        record["kma_mapped_at"] = None
        logger.exception(
            "KMA annotate FAILED | shipment_num=%s", record.get("shipment_num"),
        )
    return record
