"""Populate pickup_on / delivery_on from the existing MM/DD/YYYY text columns.

    python backfill_dates.py --dry-run
    python backfill_dates.py

The text columns were written by extractors.format_mmddyyyy, so every stored
value is either NULL or exactly MM/DD/YYYY - the conversion is done in SQL for
that reason, and guarded by a format check so a stray value is skipped rather
than aborting the run.

Batched by id so the table is never locked for long: the worker keeps ingesting
throughout.
"""

import argparse
import logging
import sys

import config
import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATE_FORMAT = r"^\d{2}/\d{2}/\d{4}$"

COUNT_SQL = f"""
SELECT count(*) FILTER (WHERE pickup_date ~ %s AND pickup_on IS NULL),
       count(*) FILTER (WHERE delivery_date ~ %s AND delivery_on IS NULL),
       count(*) FILTER (WHERE pickup_date IS NOT NULL AND pickup_date !~ %s),
       count(*) FILTER (WHERE delivery_date IS NOT NULL AND delivery_date !~ %s)
FROM {config.TABLE_NAME};
"""

UPDATE_SQL = f"""
UPDATE {config.TABLE_NAME}
SET pickup_on   = CASE WHEN pickup_date   ~ %(fmt)s THEN to_date(pickup_date,   'MM/DD/YYYY') END,
    delivery_on = CASE WHEN delivery_date ~ %(fmt)s THEN to_date(delivery_date, 'MM/DD/YYYY') END
WHERE id > %(after_id)s AND id <= %(to_id)s
  AND (pickup_on IS NULL OR delivery_on IS NULL);
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill real date columns")
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db.apply_migrations()

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(COUNT_SQL, (DATE_FORMAT,) * 4)
            pickup_todo, delivery_todo, pickup_bad, delivery_bad = cur.fetchone()
            logger.info(
                "To convert | pickup=%d delivery=%d  unparseable | pickup=%d delivery=%d",
                pickup_todo, delivery_todo, pickup_bad, delivery_bad,
            )
            if args.dry_run:
                logger.info("Dry run, nothing written.")
                return 0

            cur.execute(f"SELECT coalesce(max(id), 0) FROM {config.TABLE_NAME};")
            max_id = cur.fetchone()[0]

            after_id, written = 0, 0
            while after_id < max_id:
                to_id = min(after_id + args.batch_size, max_id)
                cur.execute(UPDATE_SQL, {"fmt": DATE_FORMAT, "after_id": after_id, "to_id": to_id})
                written += cur.rowcount
                conn.commit()
                after_id = to_id
                logger.info("Progress | up_to_id=%d written=%d", after_id, written)

    logger.info("Done | rows_updated=%d", written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
