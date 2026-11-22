"""The one place a Postgres connection is created.

Every process shares a single pool, sized from configuration. Callers use
``connection()`` for read-only work and ``transaction()`` when they intend to
commit -- the latter commits on a clean exit and rolls back on any exception.
"""

from __future__ import annotations

import atexit
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

import psycopg2
from psycopg2 import pool as pg_pool
from psycopg2.extras import RealDictCursor

from t3ra_unified.config import get_settings

logger = logging.getLogger(__name__)

_pool: pg_pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


class DatabaseUnavailable(RuntimeError):
    """The database could not be reached.

    Distinguishes a connectivity problem -- wrong host, closed port, IP not
    allowed by the security group -- from a query that failed once connected.
    """


def _get_pool() -> pg_pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                settings = get_settings()
                endpoint = settings.db_endpoint()
                try:
                    _pool = pg_pool.ThreadedConnectionPool(
                        minconn=settings.db_pool_min_size,
                        maxconn=settings.db_pool_max_size,
                        dsn=settings.psycopg_dsn(),
                        # Confines every pooled connection to this project's schema.
                        **settings.psycopg_connect_kwargs(),
                    )
                except psycopg2.OperationalError as exc:
                    # The DSN holds the password, so report only the endpoint.
                    raise DatabaseUnavailable(
                        f"Cannot reach the database at "
                        f"{endpoint['db_host']}:{endpoint['db_port']} "
                        f"(database {endpoint['db_name']!r}, user {endpoint['db_user']!r}, "
                        f"from {endpoint['db_source']}) "
                        f"after {settings.db_connect_timeout}s.\n"
                        f"  {exc.args[0].strip() if exc.args else exc}\n"
                        "Check that this machine's IP is allowed by the instance's "
                        "security group and that outbound 5432 is open."
                    ) from exc

                logger.info(
                    "Database pool created",
                    extra={**endpoint, "pool_max_size": settings.db_pool_max_size},
                )
    return _pool


def close_pool() -> None:
    """Close every pooled connection. Registered for interpreter shutdown."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None
            logger.info("Database pool closed")


atexit.register(close_pool)


@contextmanager
def connection() -> Iterator[Any]:
    """Borrow a pooled connection and return it when the block exits.

    Does not commit. Use for reads, or wrap writes in ``transaction()``.
    """
    conn = _get_pool().getconn()
    try:
        yield conn
    finally:
        # Return the connection clean; a broken one is discarded by the pool.
        with suppress(psycopg2.Error):
            conn.rollback()
        _get_pool().putconn(conn)


@contextmanager
def transaction() -> Iterator[Any]:
    """Borrow a connection, commit on success, roll back on any exception."""
    conn = _get_pool().getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _get_pool().putconn(conn)


@contextmanager
def cursor(*, dict_rows: bool = False, commit: bool = False) -> Iterator[Any]:
    """Shorthand for the common ``with conn, conn.cursor()`` pair."""
    ctx = transaction() if commit else connection()
    factory = RealDictCursor if dict_rows else None
    with ctx as conn, conn.cursor(cursor_factory=factory) as cur:
        yield cur


def healthcheck() -> bool:
    """True when the database answers. Used by the API readiness probe."""
    try:
        with cursor() as cur:
            cur.execute("SELECT 1")
            return cur.fetchone() is not None
    except Exception:
        logger.exception("Database healthcheck failed")
        return False
