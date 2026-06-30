import logging
from contextlib import contextmanager

import psycopg2

import config

logger = logging.getLogger(__name__)

_SCHEMA_READY = False


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
        pickup_date TEXT,
        delivery_date TEXT,
        customer_name TEXT,
        carrier_name TEXT,
        customer_freight_cost NUMERIC,
        carrier_freight_cost NUMERIC,
        customer_total_cost NUMERIC,
        carrier_total_cost NUMERIC,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q)
        conn.commit()
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
