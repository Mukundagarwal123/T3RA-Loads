import argparse
import logging
import socket
import time
from typing import Any, Dict

from carrier_processor import build_carrier_record
from db import carrier_exists, upsert_carriers, upsert_shipment
from extractors import extract_carrier_id
from shipment_processor import build_record
from turvo_client import fetch_carrier_details, fetch_shipment_details
from webhook_queue import claim_events, ensure_schema, mark_done, mark_retry_or_dead

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("worker")


def sync_carrier(carrier_id: int) -> None:
    """Add the carrier to our carriers table if we haven't seen it before."""
    try:
        if carrier_exists(carrier_id):
            logger.info("Carrier already on file | carrier_id=%s", carrier_id)
            return

        logger.info("New carrier detected, fetching from Turvo | carrier_id=%s", carrier_id)
        carrier_resp = fetch_carrier_details(carrier_id)
        carrier_details = carrier_resp.get("details") or carrier_resp
        carrier_record = build_carrier_record(carrier_details)
        upsert_carriers([carrier_record])
        logger.info(
            "Carrier added | carrier_id=%s name=%s", carrier_id, carrier_record.get("name"),
        )
    except Exception:
        # Carrier sync is a side effect of shipment processing; a failure here
        # shouldn't fail/retry the whole shipment event.
        logger.exception("Carrier sync FAILED | carrier_id=%s", carrier_id)


def process_one(event: Dict[str, Any], max_attempts: int) -> None:
    event_id = int(event["id"])
    attempt_count = int(event.get("attempt_count") or 1)
    shipment_id = event.get("shipment_id")

    logger.info(
        "Processing queue event | event_id=%s shipment_id=%s attempt=%s/%s",
        event_id, shipment_id, attempt_count, max_attempts,
    )

    try:
        logger.info("Fetching shipment from Turvo API | shipment_id=%s", shipment_id)
        shipment = fetch_shipment_details(int(shipment_id))
        logger.info("Shipment fetched | shipment_id=%s", shipment_id)

        record = build_record(shipment)
        logger.info("Record built | shipment_id=%s record=%s", shipment_id, record)

        upsert_shipment(record)

        details = shipment.get("details") or shipment
        carrier_id = extract_carrier_id(details)
        if carrier_id is not None:
            sync_carrier(carrier_id)
        else:
            logger.warning("No carrier id found on shipment | shipment_id=%s", shipment_id)

        mark_done(event_id)
        logger.info(
            "Event DONE | event_id=%s shipment_id=%s shipment_num=%s",
            event_id, shipment_id, record.get("shipment_num"),
        )
    except Exception as e:
        err = repr(e)
        mark_retry_or_dead(event_id, attempt_count, max_attempts, err)
        logger.exception("Event FAILED | event_id=%s shipment_id=%s error=%s", event_id, shipment_id, err)


def main() -> None:
    parser = argparse.ArgumentParser(description="Route-complete webhook queue worker")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--sleep-seconds", type=float, default=2.0)
    parser.add_argument("--max-attempts", type=int, default=10)
    args = parser.parse_args()

    worker_id = f"{socket.gethostname()}-{int(time.time())}"
    ensure_schema()
    logger.info("Worker started | worker_id=%s batch_size=%s sleep=%s", worker_id, args.batch_size, args.sleep_seconds)

    last_heartbeat = time.time()
    heartbeat_interval_seconds = 30

    while True:
        events = claim_events(max(1, args.batch_size), worker_id)
        if not events:
            if time.time() - last_heartbeat >= heartbeat_interval_seconds:
                logger.info("Worker idle, waiting for queue events...")
                last_heartbeat = time.time()
            time.sleep(max(0.2, args.sleep_seconds))
            continue

        logger.info("Claimed %s queue event(s)", len(events))
        for e in events:
            process_one(e, args.max_attempts)
        last_heartbeat = time.time()


if __name__ == "__main__":
    main()
