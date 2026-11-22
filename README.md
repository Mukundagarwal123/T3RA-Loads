# t3ra-unified

One service for the whole life of a Turvo shipment. It replaces two repos
(Spot-Bid-Agent's `covered_loads` and `T3RA-Loads`) with one codebase, one
webhook, one queue and one database. **Business logic of both flows is unchanged**;
the only deliberate change is the four cost columns (below).

```
Turvo webhook ─► intake (auth, dedupe) ─► webhook_queue ─► worker ─┬─ Covered phase    (open shipments)
                                                                   └─ Completion phase (route complete / completed)
                                                                         ▼
                                                                  shipments (one row per shipment_num)
                                              views for Spot Bid: route_complete_shipments · covered_loads.covered_loads
```

## Layout
```
src/t3ra_unified/
  config.py            one Settings (DB_*, TURVO_* incl. TURVO_WEBHOOK_SHARED_TOKEN, GS_*, DAT_*, ...)
  intake/webhook.py    the single receiver: auth, queue, return 200
  worker.py            one Turvo fetch per event, then the phase(s) that apply
  covered/             Covered Loads: domain (rules, rate engines, margins), services, repository, dashboard queries
  completion/          former T3RA: extractors, KMA market lookup, carriers, repository
  clients/             Turvo (one login, on-disk token cache), Greenscreens, DAT, HTTP
  db/                  connection pool, queue, job status
  api/                 FastAPI app + /api/dashboard/*
  ops/healthcheck.py   watchdog (alerts to Teams)
migrations/            Alembic: the schema is owned here, nothing creates tables at runtime
scripts/               KMA/carrier imports and backfills (from T3RA), purge_queue, migrate
tests/                 unit (Covered's suite ported) + integration_test_lifecycle.py (real Postgres)
```

## Which phase runs
| event | Covered phase | Completion phase |
|---|---|---|
| SHIPMENT_UPDATE / SHIPMENT_STATUS_UPDATE, shipment already tracked (any status) | yes | only if status is route complete / completed |
| same events, unknown shipment, status covered / processing / completed | yes | only if completed |
| unknown shipment, route complete / completed (T3RA's old trigger) | no | yes |
| anything else, CARRIER_* events | ignored | ignored |

A completed shipment runs both phases from the same fetched payload, in that order.

## One row, who writes what
* **Shared, T3RA logic, written by both phases:** `pickup_date`, `delivery_date`, `customer_name`, `carrier_name`, `carrier_id`, `carrier_am`, and the four cost columns.
* **Cost columns (identical rule in both phases):** `customer_freight_cost` = `customerOrder[0]` latest `freight-flat`; `carrier_freight_cost` = main non-lumper carrier order's latest `freight-flat` (no layover); `customer_total_cost` = `margin.totalReceivableAmount`; `carrier_total_cost` = `margin.totalPayableAmount`. Missing stays NULL.
* **Completion only:** `equipment` (Van/Reefer/raw), origin/destination city-state-zip, `total_stops`, KMA columns.
* **Covered only:** `covered_equipment`, `equipment_raw`, `lane`, `lane_raw`, `transport_mode`, `status`, GS/DAT rates and margins, `covered_date`, notes, `is_covered`.
* `finalized_at` is set when the shipment reaches route complete / completed; it is what puts the row in `route_complete_shipments`.

Spot Bid keeps reading the old names through two read-only views; do not write to them.

## Environment names
* Database, Turvo and webhook use the names the two old projects shared: `DB_*`, `TURVO_*`, `TURVO_WEBHOOK_SHARED_TOKEN` (Spot Bid's name; T3RA's `WEBHOOK_SHARED_TOKEN` also works), `WEBHOOK_PATH`.
* Covered-owned settings keep the **old `COVERED_*` names** (`COVERED_GS_CLIENT_ID`, `COVERED_DAT_USER_PASSWORD`, `COVERED_REFRESH_INTERVAL_MINUTES`, ...). An existing Covered `.env` loads unchanged; the plain name (`GS_CLIENT_ID`) also works and wins if both are set. `COVERED_WEBHOOK_BEARER_TOKEN` and `COVERED_TURVO_*` are accepted as last-resort fallbacks.

## Run it locally
```bash
docker compose up -d                       # Postgres on 127.0.0.1:5441 (needs POSTGRES_PASSWORD in .env)
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env                       # fill it in
python scripts/migrate.py                  # alembic upgrade head
python scripts/import_kma.py               # DAT market reference data
python -m t3ra_unified.api                 # webhook + dashboard, port 8000
python -m t3ra_unified.worker
python -m t3ra_unified.covered.services.refresh_job   # one refresh pass (timer in production)
pytest -o addopts="" -m "not integration"  # unit tests
pytest tests/integration_test_lifecycle.py -o addopts=""   # needs the local Postgres
```

## Cutover notes (not automated)
* Point the Turvo webhook at this service's `WEBHOOK_PATH` with the same shared token (`TURVO_WEBHOOK_SHARED_TOKEN`, the name Spot Bid uses), and switch off Spot Bid's in-process Covered mount (its `COVERED_*` settings) so events are not written twice.
* Only new shipments are processed; no old rows are migrated. Spot Bid's reads (`TURVO_DB_URL`, `covered_loads.covered_loads`) must be pointed at this database to see them.
