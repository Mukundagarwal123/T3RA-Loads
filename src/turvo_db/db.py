import logging
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import execute_values

from turvo_db import config

logger = logging.getLogger(__name__)

_SCHEMA_READY = False
_CARRIER_SCHEMA_READY = False
_MIGRATIONS_READY = False


def _convert_text_date_column(table: str, column: str) -> str:
    """DDL converting a MM/DD/YYYY text column to a real DATE, only if needed.

    Wrapped in a DO block that checks the current type first: ALTER ... TYPE
    with a regex in its USING clause fails outright once the column is already a
    date, which would make every later migration run abort.
    """
    return rf"""
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = '{table}' AND column_name = '{column}'
              AND data_type = 'text'
        ) THEN
            ALTER TABLE {table}
            ALTER COLUMN {column} TYPE DATE
            USING CASE
                WHEN {column} ~ '^[0-9]{{2}}/[0-9]{{2}}/[0-9]{{4}}$'
                    THEN to_date({column}, 'MM/DD/YYYY')
                WHEN {column} ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$'
                    THEN {column}::date
            END;
        END IF;
    END $$;
    """


# Statements applied by apply_migrations(). Every one is idempotent, so there is
# no version table and no ordering state to keep - re-running is always safe.
#
# Adding a column to the CREATE TABLE in ensure_schema() does nothing on a
# database where the table already exists, so anything beyond the original
# columns has to live here as well.
MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS kma_markets (
        market_id   TEXT PRIMARY KEY,
        market_name TEXT NOT NULL,
        ref_city    TEXT,
        ref_state   TEXT,
        country     TEXT NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS kma_postal_prefixes (
        prefix     VARCHAR(3) PRIMARY KEY,
        country    TEXT NOT NULL,
        market_id  TEXT NOT NULL REFERENCES kma_markets(market_id),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_kma_prefixes_market ON kma_postal_prefixes (market_id);",
    """
    CREATE TABLE IF NOT EXISTS kma_expanded_prefixes (
        market_id TEXT NOT NULL REFERENCES kma_markets(market_id),
        prefix    VARCHAR(3) NOT NULL,
        PRIMARY KEY (market_id, prefix)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_kma_expanded_prefix ON kma_expanded_prefixes (prefix);",
    """
    CREATE TABLE IF NOT EXISTS kma_regions (
        region_name TEXT NOT NULL,
        prefix      VARCHAR(3) NOT NULL,
        PRIMARY KEY (region_name, prefix)
    );
    """,
    f"ALTER TABLE {config.TABLE_NAME} ADD COLUMN IF NOT EXISTS origin_kma TEXT;",
    f"ALTER TABLE {config.TABLE_NAME} ADD COLUMN IF NOT EXISTS destination_kma TEXT;",
    f"ALTER TABLE {config.TABLE_NAME} ADD COLUMN IF NOT EXISTS kma_mapped_at TIMESTAMPTZ;",
    f"ALTER TABLE {config.TABLE_NAME} ADD COLUMN IF NOT EXISTS carrier_id BIGINT;",
    f"ALTER TABLE {config.TABLE_NAME} ADD COLUMN IF NOT EXISTS carrier_id_source TEXT;",
    # pickup_date/delivery_date were TEXT holding MM/DD/YYYY, which Postgres
    # compares as strings - month first, year last - so they could not be
    # sorted or filtered by time. Converted in place rather than replaced by new
    # columns, because other code reads these names. Guarded on the current type
    # so re-running is a no-op.
    _convert_text_date_column(config.TABLE_NAME, "pickup_date"),
    _convert_text_date_column(config.TABLE_NAME, "delivery_date"),
    f"ALTER TABLE {config.TABLE_NAME} DROP COLUMN IF EXISTS pickup_on;",
    f"ALTER TABLE {config.TABLE_NAME} DROP COLUMN IF EXISTS delivery_on;",
    f"CREATE INDEX IF NOT EXISTS idx_rcs_lane_kma ON {config.TABLE_NAME} (origin_kma, destination_kma);",
    f"CREATE INDEX IF NOT EXISTS idx_rcs_carrier_id ON {config.TABLE_NAME} (carrier_id);",
    f"CREATE INDEX IF NOT EXISTS idx_rcs_kma_todo ON {config.TABLE_NAME} (id) WHERE kma_mapped_at IS NULL;",
    f"CREATE INDEX IF NOT EXISTS idx_rcs_pickup_date ON {config.TABLE_NAME} (pickup_date);",
]


@contextmanager
def get_conn():
    conn = psycopg2.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        database=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
    )
    try:
        yield conn
    finally:
        conn.close()


def apply_migrations(force: bool = False) -> None:
    """Bring the schema up to date. Idempotent, safe to call from anywhere.

    Called by migrate.py as an explicit deploy step, and by ensure_schema() as a
    safety net - deploying code that references a column the database does not
    have would fail every shipment, so it must not be possible.
    """
    global _MIGRATIONS_READY
    if _MIGRATIONS_READY and not force:
        return

    with get_conn() as conn:
        with conn.cursor() as cur:
            for statement in MIGRATIONS:
                cur.execute(statement)
        conn.commit()
    _MIGRATIONS_READY = True
    logger.info("Migrations applied | statements=%d", len(MIGRATIONS))



def ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return

    q = f"""
    CREATE TABLE IF NOT EXISTS {config.TABLE_NAME} (
        id BIGSERIAL PRIMARY KEY,
        shipment_num TEXT UNIQUE NOT NULL,
        shipment_id BIGINT,
        equipment TEXT,
        origin_city TEXT,
        origin_state TEXT,
        origin_zip TEXT,
        destination_city TEXT,
        destination_state TEXT,
        destination_zip TEXT,
        total_stops INTEGER,
        pickup_date DATE,
        delivery_date DATE,
        customer_name TEXT,
        carrier_name TEXT,
        customer_freight_cost NUMERIC,
        carrier_freight_cost NUMERIC,
        customer_total_cost NUMERIC,
        carrier_total_cost NUMERIC,
        origin_kma TEXT,
        destination_kma TEXT,
        kma_mapped_at TIMESTAMPTZ,
        carrier_id BIGINT,
        carrier_id_source TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q)
        conn.commit()
    # The CREATE above only covers a fresh database; on an existing one the
    # newer columns arrive through the migration list.
    apply_migrations()
    _SCHEMA_READY = True
    logger.info("Table ensured | table_name=%s", config.TABLE_NAME)


def upsert_shipment(record: dict) -> None:
    ensure_schema()

    query = f"""
    INSERT INTO {config.TABLE_NAME} (
        shipment_num, shipment_id, equipment,
        origin_city, origin_state, origin_zip,
        destination_city, destination_state, destination_zip,
        total_stops, pickup_date, delivery_date,
        customer_name, carrier_name,
        customer_freight_cost, carrier_freight_cost,
        customer_total_cost, carrier_total_cost,
        origin_kma, destination_kma, kma_mapped_at,
        carrier_id, carrier_id_source,
        updated_at
    )
    VALUES (
        %(shipment_num)s, %(shipment_id)s, %(equipment)s,
        %(origin_city)s, %(origin_state)s, %(origin_zip)s,
        %(destination_city)s, %(destination_state)s, %(destination_zip)s,
        %(total_stops)s, %(pickup_date)s, %(delivery_date)s,
        %(customer_name)s, %(carrier_name)s,
        %(customer_freight_cost)s, %(carrier_freight_cost)s,
        %(customer_total_cost)s, %(carrier_total_cost)s,
        %(origin_kma)s, %(destination_kma)s, %(kma_mapped_at)s,
        %(carrier_id)s, %(carrier_id_source)s,
        NOW()
    )
    ON CONFLICT (shipment_num) DO UPDATE SET
        shipment_id = EXCLUDED.shipment_id,
        equipment = EXCLUDED.equipment,
        origin_city = EXCLUDED.origin_city,
        origin_state = EXCLUDED.origin_state,
        origin_zip = EXCLUDED.origin_zip,
        destination_city = EXCLUDED.destination_city,
        destination_state = EXCLUDED.destination_state,
        destination_zip = EXCLUDED.destination_zip,
        total_stops = EXCLUDED.total_stops,
        pickup_date = EXCLUDED.pickup_date,
        delivery_date = EXCLUDED.delivery_date,
        customer_name = EXCLUDED.customer_name,
        carrier_name = EXCLUDED.carrier_name,
        customer_freight_cost = EXCLUDED.customer_freight_cost,
        carrier_freight_cost = EXCLUDED.carrier_freight_cost,
        customer_total_cost = EXCLUDED.customer_total_cost,
        carrier_total_cost = EXCLUDED.carrier_total_cost,
        origin_kma = EXCLUDED.origin_kma,
        destination_kma = EXCLUDED.destination_kma,
        kma_mapped_at = EXCLUDED.kma_mapped_at,
        carrier_id = EXCLUDED.carrier_id,
        carrier_id_source = EXCLUDED.carrier_id_source,
        updated_at = NOW();
    """

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, record)
            conn.commit()
        logger.info(
            "Upsert SUCCEEDED | shipment_num=%s shipment_id=%s",
            record.get("shipment_num"), record.get("shipment_id"),
        )
    except Exception:
        logger.exception(
            "Upsert FAILED | shipment_num=%s shipment_id=%s",
            record.get("shipment_num"), record.get("shipment_id"),
        )
        raise


def ensure_carrier_schema() -> None:
    global _CARRIER_SCHEMA_READY
    if _CARRIER_SCHEMA_READY:
        return

    q = f"""
    CREATE TABLE IF NOT EXISTS {config.CARRIER_TABLE_NAME} (
        id BIGSERIAL PRIMARY KEY,
        carrier_id BIGINT UNIQUE NOT NULL,
        name TEXT,
        email TEXT,
        country_code TEXT,
        phone TEXT,
        mc TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q)
        conn.commit()
    _CARRIER_SCHEMA_READY = True
    logger.info("Table ensured | table_name=%s", config.CARRIER_TABLE_NAME)


def carrier_exists(carrier_id: int) -> bool:
    ensure_carrier_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT 1 FROM {config.CARRIER_TABLE_NAME} WHERE carrier_id = %s LIMIT 1;",
                (carrier_id,),
            )
            return cur.fetchone() is not None


def upsert_carriers(records: list) -> int:
    """Bulk upsert carrier records, keyed on carrier_id. Returns rows written."""
    ensure_carrier_schema()

    if not records:
        return 0

    query = f"""
    INSERT INTO {config.CARRIER_TABLE_NAME} (
        carrier_id, name, email, country_code, phone, mc, updated_at
    )
    VALUES %s
    ON CONFLICT (carrier_id) DO UPDATE SET
        name = EXCLUDED.name,
        email = EXCLUDED.email,
        country_code = EXCLUDED.country_code,
        phone = EXCLUDED.phone,
        mc = EXCLUDED.mc,
        updated_at = NOW();
    """

    template = "(%(carrier_id)s, %(name)s, %(email)s, %(country_code)s, %(phone)s, %(mc)s, NOW())"

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                execute_values(cur, query, records, template=template, page_size=500)
            conn.commit()
        logger.info("Carrier upsert SUCCEEDED | rows=%d", len(records))
        return len(records)
    except Exception:
        logger.exception("Carrier upsert FAILED | rows=%d", len(records))
        raise


# ---------------------------------------------------------------------------
# KMA reference data (DAT market areas)
# ---------------------------------------------------------------------------

def _bulk_upsert(query: str, template: str, records: list, label: str) -> int:
    if not records:
        return 0
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                execute_values(cur, query, records, template=template, page_size=500)
            conn.commit()
        logger.info("%s upsert SUCCEEDED | rows=%d", label, len(records))
        return len(records)
    except Exception:
        logger.exception("%s upsert FAILED | rows=%d", label, len(records))
        raise


def upsert_kma_markets(records: list) -> int:
    apply_migrations()
    query = """
    INSERT INTO kma_markets (market_id, market_name, ref_city, ref_state, country, updated_at)
    VALUES %s
    ON CONFLICT (market_id) DO UPDATE SET
        market_name = EXCLUDED.market_name,
        ref_city = EXCLUDED.ref_city,
        ref_state = EXCLUDED.ref_state,
        country = EXCLUDED.country,
        updated_at = NOW();
    """
    template = "(%(market_id)s, %(market_name)s, %(ref_city)s, %(ref_state)s, %(country)s, NOW())"
    return _bulk_upsert(query, template, records, "KMA market")


def upsert_kma_postal_prefixes(records: list) -> int:
    apply_migrations()
    query = """
    INSERT INTO kma_postal_prefixes (prefix, country, market_id, updated_at)
    VALUES %s
    ON CONFLICT (prefix) DO UPDATE SET
        country = EXCLUDED.country,
        market_id = EXCLUDED.market_id,
        updated_at = NOW();
    """
    template = "(%(prefix)s, %(country)s, %(market_id)s, NOW())"
    return _bulk_upsert(query, template, records, "KMA prefix")


def upsert_kma_expanded_prefixes(records: list) -> int:
    apply_migrations()
    query = """
    INSERT INTO kma_expanded_prefixes (market_id, prefix)
    VALUES %s
    ON CONFLICT (market_id, prefix) DO NOTHING;
    """
    return _bulk_upsert(query, "(%(market_id)s, %(prefix)s)", records, "KMA expanded")


def upsert_kma_regions(records: list) -> int:
    apply_migrations()
    query = """
    INSERT INTO kma_regions (region_name, prefix)
    VALUES %s
    ON CONFLICT (region_name, prefix) DO NOTHING;
    """
    return _bulk_upsert(query, "(%(region_name)s, %(prefix)s)", records, "KMA region")


def fetch_kma_prefix_map() -> dict:
    """Every postal prefix mapped to its market. Read once per process by kma.py."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT prefix, market_id FROM kma_postal_prefixes;")
            return {row[0]: row[1] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# Market backfill
# ---------------------------------------------------------------------------

def iter_shipments_for_kma(after_id: int, batch_size: int, include_mapped: bool = False) -> list:
    """One keyset page of shipments needing markets, ordered by id.

    Keyset rather than OFFSET so the scan cost does not grow as the backfill
    progresses, and so rows written by the worker mid-run cannot shift the page
    boundary and make us skip one.
    """
    where = "" if include_mapped else "AND kma_mapped_at IS NULL"
    query = f"""
    SELECT id, origin_zip, destination_zip
    FROM {config.TABLE_NAME}
    WHERE id > %s {where}
    ORDER BY id
    LIMIT %s;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (after_id, batch_size))
            return [
                {"id": row[0], "origin_zip": row[1], "destination_zip": row[2]}
                for row in cur.fetchall()
            ]


def update_shipment_kma(rows: list) -> int:
    """Write markets for a batch. Each row needs id, origin_kma, destination_kma."""
    if not rows:
        return 0

    query = f"""
    UPDATE {config.TABLE_NAME} AS s
    SET origin_kma = v.origin_kma,
        destination_kma = v.destination_kma,
        kma_mapped_at = NOW()
    FROM (VALUES %s) AS v(id, origin_kma, destination_kma)
    WHERE s.id = v.id
    RETURNING s.id;
    """
    template = "(%(id)s::bigint, %(origin_kma)s::text, %(destination_kma)s::text)"

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # fetch=True, not cur.rowcount: execute_values sends the rows
                # in pages and rowcount only reports the last one.
                written = len(execute_values(
                    cur, query, rows, template=template, page_size=500, fetch=True))
            conn.commit()
        return written
    except Exception:
        logger.exception("KMA backfill update FAILED | rows=%d", len(rows))
        raise


# ---------------------------------------------------------------------------
# Carrier id resolution
# ---------------------------------------------------------------------------

def iter_unresolved_carrier_loads(after_id: int, batch_size: int, carrier_name: str = None) -> list:
    """Loads carrying a carrier name we could not turn into an id.

    These are the rows where the name is held by more than one carrier, or by
    none at all. Neither can be settled from what the load stored - only Turvo
    knows which carrier was actually on the shipment.
    """
    name_filter = "AND lower(btrim(carrier_name)) = lower(btrim(%s))" if carrier_name else ""
    params = [after_id]
    if carrier_name:
        params.append(carrier_name)
    params.append(batch_size)

    query = f"""
    SELECT id, shipment_num, shipment_id, carrier_name
    FROM {config.TABLE_NAME}
    WHERE id > %s AND carrier_id IS NULL AND shipment_id IS NOT NULL {name_filter}
    ORDER BY id
    LIMIT %s;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return [
                {"id": r[0], "shipment_num": r[1], "shipment_id": r[2], "carrier_name": r[3]}
                for r in cur.fetchall()
            ]


def set_shipment_carrier_ids(rows: list) -> int:
    """Write authoritative carrier ids. Each row needs id and carrier_id.

    updated_at is left alone deliberately: recovering an id we always had is
    not a change to the shipment.
    """
    if not rows:
        return 0

    query = f"""
    UPDATE {config.TABLE_NAME} AS s
    SET carrier_id = v.carrier_id, carrier_id_source = 'turvo'
    FROM (VALUES %s) AS v(id, carrier_id)
    WHERE s.id = v.id
    RETURNING s.id;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # fetch=True, not cur.rowcount: execute_values pages the rows and
            # rowcount only reports the last page.
            written = len(execute_values(
                cur, query, rows,
                template="(%(id)s::bigint, %(carrier_id)s::bigint)",
                page_size=200, fetch=True))
        conn.commit()
    return written


def set_shipment_carrier_ids_from_export(rows: list) -> int:
    """Write carrier ids sourced from a Turvo export. Each row needs id, carrier_id."""
    if not rows:
        return 0

    query = f"""
    UPDATE {config.TABLE_NAME} AS s
    SET carrier_id = v.carrier_id, carrier_id_source = 'turvo_export'
    FROM (VALUES %s) AS v(id, carrier_id)
    WHERE s.id = v.id
    RETURNING s.id;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # fetch=True, not cur.rowcount: execute_values pages the rows and
            # rowcount only reports the last page.
            written = len(execute_values(
                cur, query, rows,
                template="(%(id)s::bigint, %(carrier_id)s::bigint)",
                page_size=500, fetch=True))
        conn.commit()
    return written
