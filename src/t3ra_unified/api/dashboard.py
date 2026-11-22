"""JSON feed for the Coverage Metrics tab in the FreightX portal.

The GET endpoints read Postgres through
:mod:`t3ra_unified.covered.dashboard_repo`. The one write is ``PUT /loads/{shipment_num}/notes``, which
saves a user's note to this project's database only. Nothing here touches Turvo.

Keys stay snake_case; the portal camelises at its own API boundary.
"""

from __future__ import annotations

import hmac
import logging
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from t3ra_unified.config import get_settings
from t3ra_unified.covered import repository as covered_loads_repo
from t3ra_unified.covered import dashboard_repo
from t3ra_unified.db import job_status

logger = logging.getLogger(__name__)


#: Widest window the API will serve in one request. A year of covered loads is
#: already far past ``dashboard_row_limit``; anything larger is a mistake.
MAX_WINDOW_DAYS = 366

#: Longest note the API accepts.
MAX_NOTES_LENGTH = 2000


class NotesUpdate(BaseModel):
    notes: str = Field(..., max_length=MAX_NOTES_LENGTH)


def verify_dashboard_token(authorization: str | None) -> None:
    """Authenticate a portal read with ``Authorization: Bearer <token>``.

    Refuses everything while ``DASHBOARD_API_TOKEN`` is unset, so a deployment
    that forgets to configure it serves nothing rather than serving everyone.
    """
    settings = get_settings()

    if settings.dashboard_api_token is None:
        logger.error("Dashboard API called with DASHBOARD_API_TOKEN unset")
        raise HTTPException(status_code=503, detail="Dashboard API is not configured")

    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    scheme, _, provided = authorization.partition(" ")
    if scheme.lower() != "bearer" or not provided:
        raise HTTPException(
            status_code=401,
            detail="Authorization header must be a Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    expected = settings.dashboard_api_token.get_secret_value()
    if not hmac.compare_digest(provided.strip(), expected):
        raise HTTPException(
            status_code=401,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_dashboard_token(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> None:
    """Router-wide guard for every dashboard endpoint.

    A host that authenticates requests itself -- the Spot Bid backend, whose JWT
    middleware runs before any route -- replaces this through
    ``app.dependency_overrides`` rather than asking the portal for a second token.
    """
    verify_dashboard_token(authorization)


router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_dashboard_token)],
)


def _jsonable(value: Any) -> Any:
    """Postgres types FastAPI's default encoder would mangle or reject."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _serialize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: _jsonable(val) for key, val in row.items()} for row in rows]


def _window(start: date, end: date) -> tuple[datetime, datetime]:
    """Whole-day window: ``start`` 00:00 through the end of ``end``."""
    if end < start:
        raise HTTPException(status_code=422, detail="end must not be before start")
    if (end - start).days > MAX_WINDOW_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"Date range must not exceed {MAX_WINDOW_DAYS} days",
        )
    return datetime.combine(start, time.min), datetime.combine(end, time.max)


@router.get("/job-status")
async def read_job_status(
) -> dict[str, Any]:
    """Refresh-job heartbeat behind the status pill in the portal header.

    Never fails the page: a lookup error reports an unknown status rather than
    taking the whole tab down with it.
    """
    settings = get_settings()

    try:
        row = job_status.fetch_job_status(settings.refresh_job_name)
    except Exception:
        logger.exception("Job status lookup failed", extra={"job_name": settings.refresh_job_name})
        row = {}

    return {
        "job_name": settings.refresh_job_name,
        "status": row.get("status"),
        "started_at": _jsonable(row.get("started_at")),
        "finished_at": _jsonable(row.get("finished_at")),
        "last_success_at": _jsonable(row.get("last_success_at")),
        "next_run_at": _jsonable(row.get("next_run_at")),
        "message": row.get("message"),
        "updated_rows": row.get("updated_rows"),
        "timezone": settings.dashboard_timezone,
    }


@router.get("/changes")
async def read_changes(
) -> dict[str, Any]:
    """A version stamp for the loads, polled by the portal to refresh itself.

    Turvo's ``SHIPMENT_UPDATE`` / ``SHIPMENT_STATUS_UPDATE`` webhooks reach the
    database through the worker; ``version`` moves as soon as one is applied
    (or the refresh job finishes), and the portal refetches ``/loads`` only
    then. The value is opaque -- compare it, don't parse it.
    """
    settings = get_settings()

    try:
        row = dashboard_repo.fetch_data_version(settings.refresh_job_name)
    except Exception as exc:
        logger.exception("Data version lookup failed")
        raise HTTPException(status_code=503, detail="change feed unavailable") from exc

    last_webhook_at = _jsonable(row.get("last_webhook_at"))
    last_refresh_at = _jsonable(row.get("last_refresh_at"))

    return {
        "version": f"{last_webhook_at or '-'}|{last_refresh_at or '-'}",
        "last_webhook_at": last_webhook_at,
        "last_refresh_at": last_refresh_at,
    }


@router.get("/loads")
async def read_loads(
    start: date = Query(..., description="First covered date in the window (inclusive)"),
    end: date = Query(..., description="Last covered date in the window (inclusive)"),
) -> dict[str, Any]:
    """Covered loads in the window, split by whether a Greenscreens rate exists.

    ``lh_margin`` and ``gs_diff`` are fractions, not percentages -- the caller
    scales them. ``unpriced`` rows carry neither, by definition.
    """
    settings = get_settings()
    start_dt, end_dt = _window(start, end)

    priced = dashboard_repo.fetch_priced_loads(start_dt, end_dt)
    unpriced = dashboard_repo.fetch_unpriced_loads(start_dt, end_dt)

    logger.info(
        "Dashboard loads served",
        extra={"start": start.isoformat(), "end": end.isoformat(),
               "priced": len(priced), "unpriced": len(unpriced)},
    )

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "row_limit": settings.dashboard_row_limit,
        # A full page on either side means the window was clipped.
        "truncated": len(priced) >= settings.dashboard_row_limit
        or len(unpriced) >= settings.dashboard_row_limit,
        "timezone": settings.dashboard_timezone,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "priced": _serialize(priced),
        "unpriced": _serialize(unpriced),
    }


@router.put("/loads/{shipment_num}/notes")
async def update_load_notes(
    shipment_num: str,
    body: NotesUpdate,
) -> dict[str, Any]:
    """Save a user's note on a shipment. Stored in this database, never in Turvo."""

    stored = covered_loads_repo.update_notes(shipment_num, body.notes.strip())
    if stored is None:
        raise HTTPException(status_code=404, detail="Shipment not found")

    return {"shipment_num": shipment_num, "notes": stored}
