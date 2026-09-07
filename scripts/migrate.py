"""Apply pending schema changes. Run from deploy.sh before restarting services.

Every statement in db.MIGRATIONS is idempotent, so this is safe to run any
number of times, and safe to run against a database that is already up to date.

    python migrate.py
"""

import logging
import sys

from turvo_db import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    try:
        db.apply_migrations(force=True)
    except Exception:
        logger.exception("Migration FAILED - services were not restarted")
        return 1
    logger.info("Migration OK | database=%s", db.config.DB_NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())
