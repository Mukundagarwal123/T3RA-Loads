import argparse
import csv
import logging
import re

import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_CSV_PATH = r"C:\Users\MukundAgarwal\PycharmProjects\Spot Bid Agent\Carrire Data.csv"


def _clean(value):
    if value is None:
        return None
    value = value.strip()
    return value or None


def _clean_country_code(value):
    value = _clean(value)
    if value is None:
        return None
    value = value.lstrip("`").strip()
    if value and not value.startswith("+"):
        value = "+" + value
    return value


def _clean_phone(value):
    value = _clean(value)
    if value is None:
        return None
    keep_plus = value.strip().startswith("+")
    digits = re.sub(r"\D", "", value)
    return ("+" if keep_plus else "") + digits


def row_to_record(row: dict) -> dict | None:
    carrier_id_raw = _clean(row.get("ID"))
    if not carrier_id_raw or not carrier_id_raw.isdigit():
        logger.warning("Skipping row with invalid ID: %s", row)
        return None

    return {
        "carrier_id": int(carrier_id_raw),
        "name": _clean(row.get("Account name (account/shipment)")),
        "email": _clean(row.get("Billing email")),
        "country_code": _clean_country_code(row.get("Country Code")),
        "phone": _clean_phone(row.get("Billing phone number")),
        "mc": _clean(row.get("MC number")),
    }


def main():
    parser = argparse.ArgumentParser(description="Import carrier CSV into the carriers table.")
    parser.add_argument("csv_path", nargs="?", default=DEFAULT_CSV_PATH)
    args = parser.parse_args()

    with open(args.csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        records = [r for r in (row_to_record(row) for row in reader) if r is not None]

    by_carrier_id = {r["carrier_id"]: r for r in records}
    records = list(by_carrier_id.values())

    logger.info("Parsed %d valid carrier records", len(records))

    written = db.upsert_carriers(records)
    logger.info("Done | rows_written=%d table=%s", written, db.config.CARRIER_TABLE_NAME)


if __name__ == "__main__":
    main()
