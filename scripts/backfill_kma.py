"""Stamp DAT market areas onto shipments that predate the ingest-time lookup.

    python backfill_kma.py --dry-run      # read the tally first
    python backfill_kma.py
    python backfill_kma.py --all          # restamp everything, e.g. after a DAT reissue

Re-runnable by construction: a stamped row drops out of the query, so an
interrupted run resumes where it stopped rather than starting over.

Uses kma.lookup_market - the same function the worker calls - rather than doing
the prefix arithmetic in SQL, so ingest and backfill cannot drift apart.
"""

import argparse
import logging
import sys
from collections import Counter

from turvo_db import db
from turvo_db import kma

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# The reference table holds ~1,132 prefixes. Anything far below that means
# import_kma.py has not run, and stamping now would mark every row "we looked,
# there is no market" - a lie that then hides behind its own stamp.
MIN_PREFIXES = 1100


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill KMA markets onto shipments")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--limit", type=int, help="stop after this many rows")
    parser.add_argument("--all", action="store_true",
                        help="include rows already stamped (restamp everything)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing")
    args = parser.parse_args()

    prefix_map = kma.load_prefix_map()
    if len(prefix_map) < MIN_PREFIXES:
        logger.error(
            "Refusing to run | prefixes=%d expected>=%d - run import_kma.py first",
            len(prefix_map), MIN_PREFIXES,
        )
        return 1

    after_id = 0
    seen = written = 0
    origin_hits = dest_hits = 0
    unmapped: Counter = Counter()

    while True:
        batch_size = args.batch_size
        if args.limit is not None:
            batch_size = min(batch_size, args.limit - seen)
            if batch_size <= 0:
                break

        rows = db.iter_shipments_for_kma(after_id, batch_size, include_mapped=args.all)
        if not rows:
            break

        updates = []
        for row in rows:
            origin = kma.lookup_market(row["origin_zip"])
            destination = kma.lookup_market(row["destination_zip"])

            if origin:
                origin_hits += 1
            else:
                unmapped[repr(row["origin_zip"])] += 1
            if destination:
                dest_hits += 1
            else:
                unmapped[repr(row["destination_zip"])] += 1

            updates.append({
                "id": row["id"],
                "origin_kma": origin,
                "destination_kma": destination,
            })

        after_id = rows[-1]["id"]
        seen += len(rows)

        if not args.dry_run:
            written += db.update_shipment_kma(updates)

        logger.info(
            "Progress | seen=%d written=%d last_id=%s origin_mapped=%d dest_mapped=%d",
            seen, written, after_id, origin_hits, dest_hits,
        )

    if seen == 0:
        logger.info("Nothing to do | every shipment already has a market stamp")
        return 0

    logger.info(
        "Done%s | rows=%d origin_mapped=%d (%.1f%%) destination_mapped=%d (%.1f%%)",
        " (dry run, nothing written)" if args.dry_run else "",
        seen,
        origin_hits, 100.0 * origin_hits / seen,
        dest_hits, 100.0 * dest_hits / seen,
    )

    if unmapped:
        logger.info("Top unmapped postal values (Canadian codes and typos expected):")
        for value, count in unmapped.most_common(20):
            logger.info("  %-14s %d", value, count)

    return 0


if __name__ == "__main__":
    sys.exit(main())
