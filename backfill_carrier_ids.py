"""Fill in carrier_id on historical shipments by matching carrier names.

    python backfill_carrier_ids.py            # dry run, the default
    python backfill_carrier_ids.py --apply

New shipments get carrier_id straight from Turvo. Older rows only ever stored
carrier_name, and Turvo exposes no bulk endpoint, so recovering the real id
would mean one API call per shipment - slow, and the sort of login volume the
auth cooldown exists to prevent.

Name matching instead, with two hard rules:

  * names are compared with punctuation and spacing removed, and nothing more.
    The carriers table holds 'Jj Barn Transport  Inc' while their 252 loads say
    'Jj Barn Transport, Inc' - the comma does not change which company is meant.
    Stripping LLC/INC would, collapsing "Smith Trucking LLC" and "Smith Trucking
    Inc" - different MC numbers, different companies - and no downstream report
    would ever reveal it.
  * a name held by more than one carrier is skipped, not guessed at. carriers
    has no unique constraint on name and is fed by both the CSV import and the
    API sync, so duplicates are expected.

Matched rows are marked carrier_id_source='name_match' so later work can
restrict itself to authoritative ids with WHERE carrier_id_source = 'turvo'.
"""

import argparse
import logging
import sys

import config
import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

def _key(column: str) -> str:
    """Normalise a carrier name for matching: lowercase, letters and digits only.

    Punctuation and spacing differ between how a name was typed on the carrier
    record and how it came through on the load - the carriers table holds
    'Jj Barn Transport  Inc' while their 252 loads say 'Jj Barn Transport, Inc'.
    Dropping the comma is safe: it does not change which company is meant.

    This is as far as the normalisation goes. Stripping LLC/INC would collapse
    "Smith Trucking LLC" and "Smith Trucking Inc" - different MC numbers,
    different companies - and no downstream report would ever reveal it.
    """
    return f"regexp_replace(lower(btrim({column})), '[^a-z0-9]', '', 'g')"


# Carrier names unique in the carriers table, and so safe to match on.
_UNAMBIGUOUS = f"""
    SELECT {_key('name')} AS name_key, min(carrier_id) AS carrier_id
    FROM {config.CARRIER_TABLE_NAME}
    WHERE name IS NOT NULL AND btrim(name) <> ''
    GROUP BY 1
    HAVING count(*) = 1
"""

PREVIEW_SQL = f"""
WITH unambiguous AS ({_UNAMBIGUOUS})
SELECT
    count(*) FILTER (WHERE s.carrier_id IS NOT NULL)                     AS already_set,
    count(*) FILTER (WHERE s.carrier_id IS NULL AND u.carrier_id IS NOT NULL) AS would_match,
    count(*) FILTER (WHERE s.carrier_id IS NULL AND u.carrier_id IS NULL
                       AND s.carrier_name IS NOT NULL)                   AS no_match,
    count(*) FILTER (WHERE s.carrier_name IS NULL)                       AS no_name
FROM {config.TABLE_NAME} s
LEFT JOIN unambiguous u ON u.name_key = regexp_replace(lower(btrim(s.carrier_name)), '[^a-z0-9]', '', 'g');
"""

AMBIGUOUS_SQL = f"""
SELECT {_key('name')} AS name_key, count(*) AS carriers
FROM {config.CARRIER_TABLE_NAME}
WHERE name IS NOT NULL AND btrim(name) <> ''
GROUP BY 1 HAVING count(*) > 1
ORDER BY 2 DESC, 1
LIMIT 20;
"""

APPLY_SQL = f"""
WITH unambiguous AS ({_UNAMBIGUOUS})
UPDATE {config.TABLE_NAME} s
SET carrier_id = u.carrier_id,
    carrier_id_source = 'name_match'
-- updated_at is deliberately left alone. It records when the shipment data
-- last changed, and recovering an id we already had is not a change to the
-- shipment. Bumping it would make the whole table look freshly modified, and
-- the old timestamps are not recoverable.
FROM unambiguous u
WHERE s.carrier_id IS NULL
  AND s.carrier_name IS NOT NULL
  AND regexp_replace(lower(btrim(s.carrier_name)), '[^a-z0-9]', '', 'g') = u.name_key;
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill carrier_id by name match")
    parser.add_argument("--apply", action="store_true",
                        help="write the matches (default is a dry run)")
    args = parser.parse_args()

    db.apply_migrations()

    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(PREVIEW_SQL)
            already_set, would_match, no_match, no_name = cur.fetchone()

            logger.info(
                "Preview | already_set=%d would_match=%d no_match=%d no_carrier_name=%d",
                already_set, would_match, no_match, no_name,
            )

            cur.execute(AMBIGUOUS_SQL)
            ambiguous = cur.fetchall()
            if ambiguous:
                logger.info("Names held by more than one carrier - skipped, not guessed:")
                for name_key, count in ambiguous:
                    logger.info("  %-40s %d carriers", name_key, count)

            if not args.apply:
                logger.info("Dry run, nothing written. Re-run with --apply to write.")
                return 0

            cur.execute(APPLY_SQL)
            updated = cur.rowcount
        conn.commit()

    logger.info("Done | rows_updated=%d source=name_match", updated)
    return 0


if __name__ == "__main__":
    sys.exit(main())
