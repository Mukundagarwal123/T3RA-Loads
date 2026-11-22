"""Heartbeat row per background job, read by the dashboard header."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from t3ra_unified.db import engine
from t3ra_unified.observability import traced_function

logger = logging.getLogger(__name__)

TABLE = "job_run_status"


@traced_function()
def upsert_job_status(
    *,
    job_name: str,
    status: str,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    last_success_at: datetime | None = None,
    next_run_at: datetime | None = None,
    message: str | None = None,
    updated_rows: int | None = None,
    success_count: int | None = None,
    failure_count: int | None = None,
) -> None:
    """Record where a job is.

    ``finished_at`` and ``last_success_at`` are COALESCEd so a "running" update
    does not erase the previous successful run's timestamps.
    """
    query = f"""
    INSERT INTO {TABLE} (
        job_name, status, started_at, finished_at, last_success_at, next_run_at,
        message, updated_rows, success_count, failure_count
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (job_name) DO UPDATE SET
        status = EXCLUDED.status,
        started_at = EXCLUDED.started_at,
        finished_at = COALESCE(EXCLUDED.finished_at, {TABLE}.finished_at),
        last_success_at = COALESCE(EXCLUDED.last_success_at, {TABLE}.last_success_at),
        next_run_at = EXCLUDED.next_run_at,
        message = EXCLUDED.message,
        updated_rows = EXCLUDED.updated_rows,
        success_count = EXCLUDED.success_count,
        failure_count = EXCLUDED.failure_count
    """
    with engine.cursor(commit=True) as cur:
        cur.execute(
            query,
            (
                job_name,
                status,
                started_at,
                finished_at,
                last_success_at,
                next_run_at,
                message,
                updated_rows,
                success_count,
                failure_count,
            ),
        )


@traced_function()
def fetch_job_status(job_name: str) -> dict[str, Any]:
    """Latest status row, or an empty dict when the job has never run."""
    query = f"""
    SELECT job_name, status, started_at, finished_at, last_success_at, next_run_at, message, updated_rows
    FROM {TABLE}
    WHERE job_name = %s
    LIMIT 1
    """
    with engine.cursor(dict_rows=True) as cur:
        cur.execute(query, (job_name,))
        row = cur.fetchone()
    return dict(row) if row else {}
