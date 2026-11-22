"""Console entry point: ``covered-loads-api``.

Development convenience only. In staging and production the app is served by
gunicorn/uvicorn workers under a process manager -- see docs/runbook.md.
"""

from __future__ import annotations

import uvicorn

from t3ra_unified.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "t3ra_unified.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,  # keep the project's own logging configuration
    )


if __name__ == "__main__":
    main()
