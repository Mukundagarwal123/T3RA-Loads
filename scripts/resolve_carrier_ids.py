"""Recover carrier ids from Turvo for loads the name match could not settle.

    python resolve_carrier_ids.py --name "Gti Transportation Inc" --dry-run
    python resolve_carrier_ids.py --name "Gti Transportation Inc"
    python resolve_carrier_ids.py --limit 200          # anything unresolved

Why this exists rather than more clever name matching:

A load stores the carrier's *name*, never their MC number - the MC lives on the
carrier record, not on the shipment - so when two carriers share a name there is
nothing on the load to tell them apart. "Gti" is Guru Transportation under MC
772420 and Gill Transline under MC 187996; the loads say only "Gti".

Turvo does know. Every shipment carries carrierOrder[].carrier.id, we simply
never stored it. Re-fetching the shipment gives the real id instead of a guess,
so these rows are marked carrier_id_source='turvo' like any freshly ingested
load - not 'name_match'.

One API call per load, so it is aimed with --name rather than run over
everything at once. Turvo auth failures stop the run immediately: retrying
rejected credentials is what locks the account out.
"""

import argparse
import logging
import sys
import time
from collections import Counter

from turvo_db import db
from turvo_db.extractors import extract_carrier_id, extract_carrier_name
from turvo_db.turvo_client import TurvoAuthError, fetch_shipment_details
from turvo_db.worker import sync_carrier

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve carrier ids from Turvo")
    parser.add_argument("--name", help="only loads billed to this carrier name")
    parser.add_argument("--limit", type=int, help="stop after this many loads")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--sleep", type=float, default=0.15,
                        help="seconds between API calls")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and report, write nothing")
    args = parser.parse_args()

    db.apply_migrations()

    after_id = 0
    seen = resolved = failed = 0
    no_carrier = 0
    found: Counter = Counter()
    renamed: Counter = Counter()

    while True:
        batch_size = args.batch_size
        if args.limit is not None:
            batch_size = min(batch_size, args.limit - seen)
            if batch_size <= 0:
                break

        rows = db.iter_unresolved_carrier_loads(after_id, batch_size, args.name)
        if not rows:
            break
        after_id = rows[-1]["id"]

        updates = []
        for row in rows:
            seen += 1
            try:
                shipment = fetch_shipment_details(int(row["shipment_id"]))
            except TurvoAuthError:
                # Never retried: the next attempt replays the same rejected
                # credentials and walks the account towards a lockout.
                logger.error("Turvo auth failed - stopping. Fix TURVO_PASSWORD and re-run.")
                raise
            except Exception:
                failed += 1
                logger.exception("Fetch FAILED | shipment_num=%s", row["shipment_num"])
                continue

            details = shipment.get("details") or shipment
            carrier_id = extract_carrier_id(details)
            turvo_name = extract_carrier_name(details)

            if carrier_id is None:
                no_carrier += 1
                logger.warning(
                    "No carrier on shipment | shipment_num=%s stored_name=%r",
                    row["shipment_num"], row["carrier_name"],
                )
                continue

            found[(carrier_id, turvo_name)] += 1
            if turvo_name and row["carrier_name"] and \
                    turvo_name.strip().lower() != row["carrier_name"].strip().lower():
                # Turvo's name has changed since we stored ours. Worth knowing,
                # but the id is what we came for; the stored name is left as the
                # record of what the load actually said at the time.
                renamed[(row["carrier_name"], turvo_name)] += 1

            updates.append({"id": row["id"], "carrier_id": carrier_id})
            time.sleep(args.sleep)

        if updates and not args.dry_run:
            resolved += db.set_shipment_carrier_ids(updates)
            for update in updates:
                # Make sure the carrier exists locally; best effort, never fatal.
                sync_carrier(update["carrier_id"])
        elif updates:
            resolved += len(updates)

        logger.info("Progress | seen=%d resolved=%d failed=%d", seen, resolved, failed)

    if seen == 0:
        logger.info("Nothing to resolve%s", f" for {args.name!r}" if args.name else "")
        return 0

    logger.info(
        "Done%s | loads=%d resolved=%d no_carrier_on_shipment=%d fetch_failed=%d",
        " (dry run, nothing written)" if args.dry_run else "",
        seen, resolved, no_carrier, failed,
    )

    logger.info("Which carrier the loads actually belonged to:")
    for (carrier_id, name), count in found.most_common(20):
        logger.info("   %-9s %-40s %d loads", carrier_id, name, count)

    if renamed:
        logger.info("Loads whose carrier has since been renamed in Turvo:")
        for (old, new), count in renamed.most_common(10):
            logger.info("   %r -> %r  (%d)", old, new, count)

    return 0


if __name__ == "__main__":
    sys.exit(main())
