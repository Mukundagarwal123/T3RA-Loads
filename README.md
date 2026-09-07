# T3RA Loads

Ingests Turvo "route complete" shipments into Postgres, and places both ends of
every load in a DAT market area so carriers can be matched to lanes by market
rather than by mileage radius.

## How a load arrives

```
Turvo webhook  ->  webhook_server  ->  webhook_queue  ->  worker
                   (auth, dedupe)      (Postgres)         (fetch, transform, store)
```

`webhook_server` does as little as possible: check the shared token, hash the
body to dedupe, drop it on the queue, return 200. Turvo gets its answer whether
or not our downstream is healthy.

`worker` claims events in batches, fetches the full shipment from Turvo,
converts it, and writes it. A Turvo auth failure parks the whole batch and idles
for `AUTH_COOLDOWN_SECONDS` rather than replaying a rejected password until the
account locks out.

## Layout

```
src/turvo_db/          the service
  webhook_server.py    FastAPI receiver
  worker.py            queue consumer, the only writer of shipments
  webhook_queue.py     durable queue: claim, retry ladder, dead-lettering
  turvo_client.py      Turvo API client, shared on-disk token cache
  extractors.py        pure functions pulling fields out of a shipment payload
  shipment_processor.py / carrier_processor.py   payload -> database record
  kma.py               postal code -> DAT market area
  db.py                schema, migrations, every query
  config.py            environment
  healthcheck.py       watchdog, alerts to Teams

scripts/               run by hand or from deploy.sh
  migrate.py           apply schema changes (idempotent)
  import_kma.py        load the DAT reference data from data/
  import_carriers.py   load carriers from a CRM export
  backfill_kma.py      stamp markets on loads that predate the lookup
  backfill_carrier_ids.py   recover carrier ids by name
  resolve_carrier_ids.py    recover carrier ids from Turvo, authoritatively

tools/                 developer only, never deployed
  convert_kma_xlsx.py  DAT workbook -> data/*.csv

data/                  DAT market definitions, committed and diffable
deploy/                deploy.sh and the systemd units
```

## Running it

```bash
python -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env        # then fill it in

.venv/bin/python scripts/migrate.py
.venv/bin/python scripts/import_kma.py

.venv/bin/python -m turvo_db.webhook_server    # port 8000
.venv/bin/python -m turvo_db.worker
```

Deploy with `deploy/deploy.sh`, which pulls, installs, migrates and restarts
both systemd units. Migrations run **before** the restart: new code against an
old schema fails every shipment.

## Market areas

DAT divides North America into 149 markets and assigns every ZIP or postal
prefix to exactly one. `tools/convert_kma_xlsx.py` turns their workbook into the
CSVs under `data/`; `scripts/import_kma.py` loads those into Postgres; `kma.py`
does the lookup at ingest.

Two traps are worth knowing about, because both fail silently:

- **Excel eats leading zeros.** Prefixes `005`, `010`, `060` come back as `5`,
  `10`, `60`. Unpadded, the entire Northeast stops mapping and nothing errors.
  Both the converter and the importer assert those prefixes are present.
- **Canadian postal codes must be recognised before digits are extracted.**
  Stripping non-digits from `M5V 3A8` leaves `538`, a real US prefix, which
  quietly puts Toronto freight in a US market. Canadian markets are also mixed
  granularity: BC splits by full three-character FSA, every other province
  groups by two, so the lookup tries the longer key first.

## Schema notes

`route_complete_shipments` carries a few columns that exist to record *how much
to trust* the row, not just what it says:

- **`carrier_id_source`** - `turvo` (read off the shipment via the API),
  `turvo_export` (from a Turvo shipment export, verified against the API), or
  `name_match` (inferred by matching the carrier's name). Older loads stored
  only a name, and a name can be held by two carriers with different MC numbers,
  so an inferred id can be wrong. Filter to the first two when it matters.
- **`kma_mapped_at`** - set when the market lookup actually ran. Without it,
  "no market" and "never looked" are the same NULL, and the backfill cannot tell
  which rows still need work.

`pickup_date` and `delivery_date` are real `DATE` columns. They were TEXT in
`MM/DD/YYYY`, which Postgres compares as strings - month first, year last - so
`01/03/2023` sorted before `12/30/2022` and no query could ask for the last 90
days. Recency is the basis of the market scoring, so the conversion happens at
ingest.

## Environment

See `.env.example`. `TABLE_NAME` and `CARRIER_TABLE_NAME` select the tables;
`DB_NAME` selects the database, so a one-off run against another can override it
inline (`DB_NAME=postgres python scripts/migrate.py`) - `load_dotenv` does not
overwrite variables already in the environment.
