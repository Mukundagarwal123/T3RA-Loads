"""The service's HTTP app: the Turvo webhook receiver plus the dashboard feed.

The webhook is in :mod:`t3ra_unified.intake.webhook`: it authenticates a
delivery, queues it, and returns; the worker does all Turvo API work later.

The same process also serves ``/api/dashboard/*`` (see
:mod:`t3ra_unified.api.dashboard`), which the FreightX portal's Coverage
Metrics tab reads, plus the one write that saves a user's shipment note.
Those routes touch Postgres alone -- never Turvo.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from t3ra_unified.api.dashboard import router as dashboard_router
from t3ra_unified.intake.webhook import build_ready_router, build_webhook_router
from t3ra_unified.config import get_settings
from t3ra_unified.observability import (
    configure_logfire,
    configure_logging,
    instrument_fastapi,
    reset_request_id,
    set_request_id,
)

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()

    configure_logging(service_name="t3ra-api")
    configure_logfire(service_name="t3ra-api")

    app = FastAPI(title="Turvo Webhook Receiver", version="1.0.0")
    instrument_fastapi(app)
    logger.info("Webhook receiver starting", extra=settings.safe_summary())

    # The portal is a browser client, so the dashboard routes need CORS. The
    # allowed origins are configuration; an empty list allows no browser at all
    # rather than falling back to a wildcard.
    cors_origins = settings.dashboard_cors_origin_list
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "PUT", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        )
    else:
        logger.warning("DASHBOARD_CORS_ORIGINS is empty; no browser may call the dashboard API")

    app.include_router(dashboard_router)

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        token = set_request_id(request_id)
        started_at = time.perf_counter()
        try:
            response = await call_next(request)
            logger.info(
                "Request completed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
                },
            )
            response.headers["X-Request-ID"] = request_id
            return response
        except Exception:
            logger.exception(
                "Unhandled request exception",
                extra={"method": request.method, "path": request.url.path},
            )
            raise
        finally:
            reset_request_id(token)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """Liveness: the process is up. Cheap enough for a frequent probe."""
        return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}

    app.include_router(build_ready_router("/ready"))
    app.include_router(build_webhook_router())

    return app


app = create_app()
