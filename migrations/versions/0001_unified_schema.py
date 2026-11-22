"""Unified schema: one ``shipments`` table for the whole life of a shipment.

Covered phase (while the load is open) and completion phase (route complete /
completed, the former T3RA flow) write the same row, keyed by ``shipment_num``.

* Columns both flows used to compute are stored once and follow T3RA's logic:
  dates, customer_name, carrier_name/id/am and the four cost columns.
* Columns whose value differs by design stay separate so each flow's output is
  identical to today: ``equipment`` (T3RA) vs ``covered_equipment`` (Covered).
* Spot Bid keeps reading the old names through two read-only views:
  ``public.route_complete_shipments`` and ``covered_loads.covered_loads``.

Revision ID: 0001
"""
from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE shipments (
            id                       BIGSERIAL PRIMARY KEY,
            shipment_num             TEXT NOT NULL UNIQUE,
            shipment_id              BIGINT,

            -- shared, T3RA logic
            pickup_date              DATE,
            delivery_date            DATE,
            customer_name            TEXT,
            carrier_name             TEXT,
            carrier_id               BIGINT,
            carrier_id_source        TEXT,
            carrier_am               TEXT,
            customer_freight_cost    NUMERIC,
            carrier_freight_cost     NUMERIC,
            customer_total_cost      NUMERIC,
            carrier_total_cost       NUMERIC,

            -- completion phase (former T3RA)
            equipment                TEXT,
            origin_city              TEXT,
            origin_state             TEXT,
            origin_zip               TEXT,
            destination_city         TEXT,
            destination_state        TEXT,
            destination_zip          TEXT,
            total_stops              INTEGER,
            origin_kma               TEXT,
            destination_kma          TEXT,
            kma_mapped_at            TIMESTAMPTZ,

            -- covered phase (former Covered Loads)
            is_covered               BOOLEAN NOT NULL DEFAULT FALSE,
            covered_equipment        TEXT,
            equipment_raw            TEXT,
            lane                     TEXT,
            lane_raw                 TEXT,
            transport_mode           TEXT,
            status                   TEXT,
            gs_rate                  NUMERIC(10,2),
            dat_rate                 NUMERIC(10,2),
            gs_confidence_score      DOUBLE PRECISION,
            lh_margin                NUMERIC(10,2),
            gs_margin                NUMERIC(10,2),
            dat_margin               NUMERIC(10,2),
            t_margin                 NUMERIC(10,2),
            covered_date             DATE,
            carrier_notes            TEXT,
            notes                    TEXT,
            notes_updated_at         TIMESTAMPTZ,
            carrier_notes_updated_at TIMESTAMPTZ,

            -- lifecycle
            finalized_at             TIMESTAMPTZ,
            created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    op.execute("CREATE UNIQUE INDEX idx_shipments_shipment_id ON shipments (shipment_id) WHERE shipment_id IS NOT NULL;")
    op.execute("CREATE INDEX idx_rcs_lane_kma ON shipments (origin_kma, destination_kma);")
    op.execute("CREATE INDEX idx_rcs_carrier_id ON shipments (carrier_id);")
    op.execute("CREATE INDEX idx_rcs_kma_todo ON shipments (id) WHERE kma_mapped_at IS NULL;")
    op.execute("CREATE INDEX idx_rcs_pickup_date ON shipments (pickup_date);")
    op.execute("CREATE INDEX idx_shipments_covered_date ON shipments (covered_date DESC NULLS LAST) WHERE is_covered;")
    op.execute(
        "CREATE INDEX idx_shipments_refresh_scope ON shipments (covered_date DESC NULLS LAST) "
        "WHERE is_covered AND finalized_at IS NULL;"
    )
    op.execute("CREATE INDEX idx_shipments_finalized ON shipments (finalized_at) WHERE finalized_at IS NOT NULL;")

    op.execute(
        """
        CREATE TABLE carriers (
            id BIGSERIAL PRIMARY KEY,
            carrier_id BIGINT UNIQUE NOT NULL,
            name TEXT, email TEXT, country_code TEXT, phone TEXT, mc TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE kma_markets (
            market_id TEXT PRIMARY KEY, market_name TEXT NOT NULL, ref_city TEXT, ref_state TEXT,
            country TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE kma_postal_prefixes (
            prefix VARCHAR(3) PRIMARY KEY, country TEXT NOT NULL,
            market_id TEXT NOT NULL REFERENCES kma_markets(market_id),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX idx_kma_prefixes_market ON kma_postal_prefixes (market_id);
        CREATE TABLE kma_expanded_prefixes (
            market_id TEXT NOT NULL REFERENCES kma_markets(market_id),
            prefix VARCHAR(3) NOT NULL, PRIMARY KEY (market_id, prefix)
        );
        CREATE INDEX idx_kma_expanded_prefix ON kma_expanded_prefixes (prefix);
        CREATE TABLE kma_regions (
            region_name TEXT NOT NULL, prefix VARCHAR(3) NOT NULL, PRIMARY KEY (region_name, prefix)
        );
        CREATE TABLE webhook_queue (
            id BIGSERIAL PRIMARY KEY,
            event_key TEXT UNIQUE NOT NULL,
            shipment_id BIGINT,
            status_value TEXT,
            payload_json JSONB,
            state TEXT NOT NULL DEFAULT 'pending',  -- pending | processing | retry | done | ignored | dead
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            processed_at TIMESTAMPTZ,
            worker_id TEXT,
            last_error TEXT
        );
        CREATE INDEX idx_webhook_queue_state_received ON webhook_queue (state, received_at);
        CREATE INDEX idx_webhook_queue_state_next ON webhook_queue (state, next_attempt_at, received_at);
        CREATE INDEX idx_webhook_queue_done_processed_at ON webhook_queue (processed_at) WHERE state = 'done';
        CREATE TABLE job_run_status (
            job_name TEXT PRIMARY KEY, status TEXT NOT NULL,
            started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, last_success_at TIMESTAMPTZ, next_run_at TIMESTAMPTZ,
            message TEXT, updated_rows INTEGER, success_count INTEGER, failure_count INTEGER,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    # ---- read-only compatibility views for Spot Bid (old names, old order) ----
    op.execute(
        """
        CREATE VIEW route_complete_shipments WITH (security_invoker = true) AS
        SELECT id, shipment_num, shipment_id, equipment,
               origin_city, origin_state, origin_zip,
               destination_city, destination_state, destination_zip,
               total_stops, pickup_date, delivery_date, customer_name, carrier_name,
               customer_freight_cost, carrier_freight_cost, customer_total_cost, carrier_total_cost,
               created_at, updated_at, origin_kma, destination_kma, kma_mapped_at,
               carrier_id, carrier_id_source, carrier_am
        FROM shipments
        WHERE finalized_at IS NOT NULL;
        """
    )
    op.execute("CREATE SCHEMA IF NOT EXISTS covered_loads;")
    op.execute(
        """
        CREATE VIEW covered_loads.covered_loads WITH (security_invoker = true) AS
        SELECT shipment_num, shipment_id, lane, lane_raw, pickup_date, delivery_date,
               covered_equipment AS equipment, transport_mode,
               customer_name AS customer, carrier_name AS carrier, carrier_am, status,
               customer_freight_cost, customer_total_cost, carrier_freight_cost, carrier_total_cost,
               gs_rate, dat_rate, lh_margin, gs_margin, dat_margin, t_margin, covered_date,
               gs_confidence_score, equipment_raw, finalized_at,
               carrier_notes, notes, notes_updated_at, carrier_notes_updated_at
        FROM shipments
        WHERE is_covered;
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS covered_loads.covered_loads;")
    op.execute("DROP SCHEMA IF EXISTS covered_loads;")
    op.execute("DROP VIEW IF EXISTS route_complete_shipments;")
    for t in ("job_run_status", "webhook_queue", "kma_regions", "kma_expanded_prefixes",
              "kma_postal_prefixes", "kma_markets", "carriers", "shipments"):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE;")
