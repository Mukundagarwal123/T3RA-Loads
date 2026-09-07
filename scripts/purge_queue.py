"""Trim completed events from webhook_queue so the table stops growing forever.

    python scripts/purge_queue.py --dry-run
    python scripts/purge_queue.py

Intended to run nightly. Safe to run at any time - it only ever touches events
that finished long ago, never anything pending, processing, retrying or dead.

See webhook_queue.purge_old_events for why payloads and rows are aged out on
different clocks.
"""

import argparse
import logging
import sys

from turvo_db import config, db
from turvo_db.webhook_queue import purge_old_events

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _table_size(label: str) -> None:
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT pg_size_pretty(pg_total_relation_size('webhook_queue')),
                   count(*), min(received_at)::date
            FROM webhook_queue;""")
            size, rows, oldest = cur.fetchone()
    logger.info("%s | size=%s rows=%s oldest=%s", label, size, f"{rows:,}", oldest)


def main() -> int:
    parser = argparse.ArgumentParser(description="Purge old webhook queue events")
    parser.add_argument("--payload-days", type=int, default=config.QUEUE_PAYLOAD_RETENTION_DAYS,
                        help="clear payload_json on finished events older than this")
    parser.add_argument("--event-days", type=int, default=config.QUEUE_EVENT_RETENTION_DAYS,
                        help="delete finished events older than this")
    parser.add_argument("--dry-run", action="store_true", help="report only")
    args = parser.parse_args()

    if args.payload_days > args.event_days:
        logger.error(
            "payload-days (%s) must not exceed event-days (%s): the row is deleted "
            "before its payload would ever be cleared.",
            args.payload_days, args.event_days,
        )
        return 1

    _table_size("Before")
    counts = purge_old_events(args.payload_days, args.event_days, dry_run=args.dry_run)

    if args.dry_run:
        logger.info(
            "Dry run | would clear %s payloads and delete %s events, nothing written",
            f"{counts['payloads_to_clear']:,}", f"{counts['events_to_delete']:,}",
        )
        return 0

    logger.info(
        "Done | payloads_cleared=%s events_deleted=%s",
        f"{counts['payloads_cleared']:,}", f"{counts['events_deleted']:,}",
    )
    _table_size("After")
    return 0


if __name__ == "__main__":
    sys.exit(main())
