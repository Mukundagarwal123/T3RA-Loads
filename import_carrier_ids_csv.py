"""Fill in carrier ids from a Turvo shipment export.

    python import_carrier_ids_csv.py "Sheet 24_data (17).csv"       # dry run
    python import_carrier_ids_csv.py "Sheet 24_data (17).csv" --apply

The export's "Account ID" column is the Turvo carrier id that older loads never
stored. Checked against the 107 ids fetched straight from Turvo's API by
resolve_carrier_ids.py: 104 present, 104 agreeing, none disagreeing.

Two things have to be right or it quietly attributes loads to the wrong company:

  * A shipment appears once per carrier order, so any load carrying a lumper fee
    has two rows - the real carrier and Lumper Advance (account 7028286) - and
    both rows show the primary carrier's *name*, so only the account id differs.
    Taking the last row seen gets 1 in 20 wrong. Lumper Advance is excluded, the
    same way extractors.extract_carrier_id excludes it.
  * If a shipment still has two different carrier accounts after that, it is
    skipped rather than guessed at.

Ids already fetched from the API (carrier_id_source='turvo') are never
overwritten. Name-matched guesses are, because this export is the better
evidence - each corrected row is reported before anything is written.
"""

import argparse
import collections
import csv
import logging
import sys
from pathlib import Path

import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Turvo's account for the "Lumper Advance" fee line. Not a trucking company.
LUMPER_ACCOUNT_ID = 7028286


def read_export(path: Path) -> dict:
    """{shipment_num: carrier_id} for shipments with one unambiguous carrier."""
    if not path.exists():
        logger.error("Missing file | path=%s", path)
        sys.exit(1)

    accounts = collections.defaultdict(set)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    for row in rows:
        num = (row.get("Shipment number") or "").strip()
        account = (row.get("Account ID") or "").strip()
        if not num or not account.isdigit():
            continue
        account_id = int(account)
        if account_id == LUMPER_ACCOUNT_ID:
            continue
        accounts[num].add(account_id)

    clean = {num: ids.pop() for num, ids in accounts.items() if len(ids) == 1}
    ambiguous = sum(1 for ids in accounts.values() if len(ids) > 1)
    logger.info(
        "Export read | rows=%d shipments=%d usable=%d ambiguous=%d",
        len(rows), len(accounts), len(clean), ambiguous,
    )
    return clean


def main() -> int:
    parser = argparse.ArgumentParser(description="Import carrier ids from a Turvo export")
    parser.add_argument("csv_path")
    parser.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    args = parser.parse_args()

    db.apply_migrations()
    export = read_export(Path(args.csv_path))

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
            SELECT id, shipment_num, carrier_id, carrier_id_source, carrier_name
            FROM {db.config.TABLE_NAME};""")
            shipments = cur.fetchall()

    fills, corrections, protected, agreed = [], [], 0, 0
    for row_id, num, carrier_id, source, name in shipments:
        found = export.get(num)
        if found is None:
            continue
        if carrier_id == found:
            agreed += 1
        elif carrier_id is None:
            fills.append({"id": row_id, "carrier_id": found})
        elif source == "turvo":
            # Fetched from the API itself; nothing here outranks that.
            protected += 1
        else:
            corrections.append({"id": row_id, "carrier_id": found, "was": carrier_id, "num": num})

    logger.info(
        "Matched | already_correct=%d to_fill=%d to_correct=%d api_ids_left_alone=%d",
        agreed, len(fills), len(corrections), protected,
    )

    if corrections:
        logger.info("Corrections (our id -> export id):")
        with db.get_conn() as conn, conn.cursor() as cur:
            for c in corrections[:15]:
                cur.execute(f"""SELECT
                    (SELECT name FROM {db.config.CARRIER_TABLE_NAME} WHERE carrier_id = %s),
                    (SELECT name FROM {db.config.CARRIER_TABLE_NAME} WHERE carrier_id = %s);""",
                    (c["was"], c["carrier_id"]))
                old, new = cur.fetchone()
                logger.info("   %-8s %s -> %s", c["num"], old, new)
            if len(corrections) > 15:
                logger.info("   ...and %d more", len(corrections) - 15)

    if not args.apply:
        logger.info("Dry run, nothing written. Re-run with --apply.")
        return 0

    updates = fills + [{"id": c["id"], "carrier_id": c["carrier_id"]} for c in corrections]
    written = db.set_shipment_carrier_ids_from_export(updates)
    logger.info("Done | rows_written=%d source=turvo_export", written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
