"""Single source of configuration for every process in this project (webhook receiver,
worker, refresh job, dashboard API and the T3RA completion scripts).

Nothing else in the codebase may hardcode a host, URL, table name, credential,
or environment switch. Read values from ``get_settings()``.

Values come from the process environment, with a local ``.env`` file as a
convenience for development. In staging and production, inject them from the
secrets manager instead of shipping a ``.env``.

Variable names are unprefixed and follow the T3RA convention: ``DB_*``, ``TURVO_*``,
``TURVO_WEBHOOK_SHARED_TOKEN`` / ``WEBHOOK_PATH``; Covered Loads' own settings keep their names without the old ``COVERED_``
prefix (``GS_*``, ``DAT_*``, ``REFRESH_*``, ``DASHBOARD_*``, ``WORKER_*``).
"""

from __future__ import annotations

import re
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, DotEnvSettingsSource, EnvSettingsSource, SettingsConfigDict
from sqlalchemy.engine import URL, make_url


class Environment(str, Enum):
    local = "local"
    staging = "staging"
    prod = "prod"


_PROJECT_DIR = Path(__file__).resolve().parents[2]
_ENV_FILES = (str(_PROJECT_DIR / ".env"),)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=_ENV_FILES,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        """Every Covered-owned setting is read under two names: plain (``GS_CLIENT_ID``)
        and the old Covered Loads name (``COVERED_GS_CLIENT_ID``), so an existing
        Covered ``.env`` works unchanged. The plain name wins when both are set."""
        legacy_env = EnvSettingsSource(settings_cls, env_prefix="COVERED_")
        legacy_dotenv = DotEnvSettingsSource(settings_cls, env_prefix="COVERED_")
        return (init_settings, env_settings, legacy_env, dotenv_settings, legacy_dotenv, file_secret_settings)

    # ------------------------------------------------------------------
    # Runtime
    # ------------------------------------------------------------------
    environment: Environment = Environment.local
    log_level: str = "INFO"
    logfire_token: SecretStr | None = None

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    # Which Postgres to use, first match wins:
    #   1. DATABASE_URL
    #   2. DB_HOST / _USER / _PASSWORD (+ _PORT, _NAME, _SSLMODE)
    #   3. DATABASE_URL -- the Spot Bid backend's own database. The normal case:
    #      covered-loads shares Spot Bid's Postgres, kept apart in ``db_schema``.
    database_url: SecretStr | None = None
    shared_database_url: SecretStr | None = Field(default=None, validation_alias="DATABASE_URL")
    db_host: str | None = None
    db_port: int = 5432
    db_name: str = "postgres"
    db_user: str | None = None
    db_password: SecretStr | None = None
    db_sslmode: Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"] = "require"
    #: Postgres schema that holds every table of this project, including its
    #: Alembic version table. Connections see only this schema, so nothing here
    #: can read or write the Spot Bid tables in ``public``.
    db_schema: str = "public"
    db_connect_timeout: int = 10
    db_pool_min_size: int = 1
    db_pool_max_size: int = 8

    #: Table the pipeline writes covered loads into. Staging and production
    #: differ only by this value -- never by an edit to the source.
    shipments_table: str = "shipments"
    carrier_table: str = "carriers"
    #: Read-only view with Covered Loads' original column names; the dashboard and
    #: the Covered phase read through it.
    covered_view: str = "covered_loads.covered_loads"

    # ------------------------------------------------------------------
    # Turvo (READ-ONLY -- see claude.md)
    # ------------------------------------------------------------------
    # The same Turvo account as Spot Bid: each value falls back to Spot Bid's
    # own variable (TURVO_API_KEY, ...).
    turvo_base_url: str = Field(
        default="https://publicapi.turvo.com",
        validation_alias=AliasChoices("TURVO_BASE_URL", "TURVO_PUBLIC_API_BASE_URL", "COVERED_TURVO_BASE_URL"),
    )
    turvo_api_key: SecretStr = Field(validation_alias=AliasChoices("TURVO_API_KEY", "COVERED_TURVO_API_KEY"))
    turvo_client_id: str = Field(
        default="publicapi", validation_alias=AliasChoices("TURVO_CLIENT_ID", "COVERED_TURVO_CLIENT_ID")
    )
    turvo_client_secret: SecretStr = Field(
        validation_alias=AliasChoices("TURVO_CLIENT_SECRET", "COVERED_TURVO_CLIENT_SECRET")
    )
    turvo_username: str = Field(validation_alias=AliasChoices("TURVO_USERNAME", "COVERED_TURVO_USERNAME"))
    turvo_password: SecretStr = Field(validation_alias=AliasChoices("TURVO_PASSWORD", "COVERED_TURVO_PASSWORD"))
    turvo_customer_id: int | None = None
    turvo_customer_name: str | None = None
    turvo_timeout_seconds: int = 30
    #: A Turvo call slower than this is logged as a warning. Turvo latency was
    #: the visible problem in the 8 Sep review, so it is measured, not guessed.
    turvo_slow_request_ms: int = 5000

    # ------------------------------------------------------------------
    # Webhook receiver
    # ------------------------------------------------------------------
    #: Must not be ``/webhooks/turvo`` -- Spot Bid already serves that path for
    #: its tendered board, and the first route registered would take every
    #: delivery.
    webhook_path: str = "/webhooks/turvo"
    #: Bearer token Turvo sends in the ``Authorization`` header of every
    #: delivery. Turvo does not sign webhook bodies, so this token is the whole
    #: of the endpoint's authentication -- treat it like a password.
    webhook_shared_token: SecretStr = Field(
        validation_alias=AliasChoices("TURVO_WEBHOOK_SHARED_TOKEN", "WEBHOOK_SHARED_TOKEN", "COVERED_WEBHOOK_BEARER_TOKEN")
    )
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # ------------------------------------------------------------------
    # Greenscreens
    # ------------------------------------------------------------------
    gs_enabled: bool = True
    gs_client_id: str
    gs_client_secret: SecretStr
    gs_auth_url: str = "https://api.greenscreens.ai/v1/auth/token"
    gs_prediction_url: str = "https://api.greenscreens.ai/v3/prediction/network-rates"
    gs_batch_prediction_url: str = "https://app.greenscreens.ai/v3/prediction/batch-network-rates"
    gs_timeout_seconds: int = 30

    # ------------------------------------------------------------------
    # DAT
    # ------------------------------------------------------------------
    dat_enabled: bool = True
    dat_env: Literal["prod", "staging"] = "prod"
    dat_org_username: str
    dat_org_password: SecretStr
    dat_user_username: str
    dat_user_password: SecretStr
    dat_timeout_seconds: int = 20
    dat_retry_count: int = 2
    dat_retry_sleep_seconds: float = 1.2

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------
    worker_batch_size: int = 20
    #: Idle poll of the webhook queue; a busy queue drains without pausing.
    worker_sleep_seconds: float = 10.0
    worker_max_attempts: int = 10
    #: Run the queue worker as a thread inside the Spot Bid server. Turn off
    #: when running ``covered-loads-worker`` as its own process instead.
    worker_in_process: bool = True
    #: Run ``alembic upgrade head`` on this project's database before the
    #: in-process worker starts. Off when migrations run as a separate step.
    migrate_on_startup: bool = True

    # ------------------------------------------------------------------
    # Refresh job
    # ------------------------------------------------------------------
    refresh_job_name: str = "update_covered_loads"
    refresh_interval_minutes: int = 30
    #: Run the refresh job on a timer inside the Spot Bid server. Turn off when
    #: ``covered-loads-refresh`` is scheduled as its own process instead.
    refresh_in_process: bool = True
    refresh_batch_page_size: int = 200
    #: Stop re-pulling a covered load once it is this old. A load nobody has
    #: touched in two weeks is not going to change.
    refresh_max_age_days: int = 14
    #: Skip loads whose route has completed. The completion webhook already
    #: wrote the final state, so refreshing them again buys nothing.
    refresh_skip_finalized: bool = True


    # ------------------------------------------------------------------
    # T3RA completion phase (route complete / completed)
    # ------------------------------------------------------------------
    #: Turvo sessions are cached on disk so the receiver, worker, refresh job
    #: and scripts share one login instead of each making their own.
    token_cache_path: str = str(_PROJECT_DIR / ".turvo_token.json")
    #: After Turvo rejects our credentials, refuse another login for this long.
    auth_cooldown_seconds: int = 900
    queue_payload_retention_days: int = 30
    queue_event_retention_days: int = 90
    #: Statuses that run the completion (T3RA) phase, case-insensitive.
    completion_statuses: str = "route complete,completed"
    teams_webhook_url: str = ""

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------
    dashboard_timezone: str = "America/Los_Angeles"
    dashboard_row_limit: int = 6000
    dashboard_cache_ttl_seconds: int = 120

    #: Bearer token the portal sends on the read-only dashboard endpoints. The
    #: endpoints refuse every request while this is unset, so a deployment that
    #: forgets it is closed rather than open.
    dashboard_api_token: SecretStr | None = None
    #: Comma-separated browser origins allowed to call the dashboard endpoints.
    #: Empty means no browser may call them -- there is no wildcard default.
    dashboard_cors_origins: str = ""

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.upper()

    @field_validator("shipments_table", "covered_view")
    @classmethod
    def _validate_table_name(cls, value: str) -> str:
        """Reject anything that is not a bare identifier.

        The table name is interpolated into SQL (Postgres has no placeholder
        for identifiers), so it must never carry arbitrary text.
        """
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?", value):
            raise ValueError(f"shipments_table must be a plain identifier, got {value!r}")
        return value

    @field_validator("db_schema")
    @classmethod
    def _validate_schema(cls, value: str) -> str:
        """A bare lowercase identifier, and never Spot Bid's ``public``.

        Interpolated into ``CREATE SCHEMA`` and ``search_path``, so it is held
        to the same rule as the table name.
        """
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", value):
            raise ValueError(f"db_schema must be a lowercase identifier, got {value!r}")
        if value in {"pg_catalog", "information_schema"}:
            raise ValueError(f"db_schema must not be a system schema, got {value!r}")
        return value

    @model_validator(mode="after")
    def _require_postgres(self) -> Settings:
        """Fail at startup unless a Postgres database is configured."""
        backend = self.database_url_object().get_backend_name()
        if backend != "postgresql":
            raise ValueError(
                f"Covered Loads needs PostgreSQL, but the configured database is {backend!r}. "
                "Set DATABASE_URL (or DATABASE_URL) to a postgresql:// URL."
            )
        return self

    def database_url_object(self) -> URL:
        """The database to use, resolved in the order documented on the fields."""
        if self.database_url is not None:
            return make_url(self.database_url.get_secret_value())
        if self.db_host:
            if not self.db_user or self.db_password is None:
                raise ValueError("DB_HOST is set, so DB_USER and DB_PASSWORD are required")
            return URL.create(
                "postgresql+psycopg2",
                username=self.db_user,
                password=self.db_password.get_secret_value(),
                host=self.db_host,
                port=self.db_port,
                database=self.db_name,
                query={"sslmode": self.db_sslmode},
            )
        if self.shared_database_url is not None:
            return make_url(self.shared_database_url.get_secret_value())
        raise ValueError("No database configured: set DATABASE_URL, DATABASE_URL or DB_HOST")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dat_org_token_url(self) -> str:
        host = "identity.api.staging.dat.com" if self.dat_env == "staging" else "identity.api.dat.com"
        return f"https://{host}/access/v1/token/organization"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dat_user_token_url(self) -> str:
        host = "identity.api.staging.dat.com" if self.dat_env == "staging" else "identity.api.dat.com"
        return f"https://{host}/access/v1/token/user"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dat_rate_lookup_url(self) -> str:
        host = "analytics.api.staging.dat.com" if self.dat_env == "staging" else "analytics.api.dat.com"
        return f"https://{host}/linehaulrates/v1/lookups"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dashboard_cors_origin_list(self) -> list[str]:
        """``dashboard_cors_origins`` split into the list CORSMiddleware wants."""
        return [origin.strip() for origin in self.dashboard_cors_origins.split(",") if origin.strip()]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.environment is Environment.prod

    def psycopg_dsn(self) -> str:
        """libpq connection URI. Never log the result -- it holds the password.

        Pass :meth:`psycopg_connect_kwargs` alongside it: that is what confines
        the connection to ``db_schema``.
        """
        return self.database_url_object().set(drivername="postgresql").render_as_string(hide_password=False)

    def psycopg_connect_kwargs(self) -> dict[str, object]:
        """Extra libpq parameters for every connection this project opens.

        ``search_path`` is this project's schema *alone* -- not ``public`` after
        it -- so an unqualified table name can only ever resolve here, and a
        missing schema is an error rather than a silent write into Spot Bid's
        tables.
        """
        return {
            "connect_timeout": self.db_connect_timeout,
            "options": f"-c search_path={self.db_schema}",
        }

    def sqlalchemy_url(self) -> str:
        """URL for Alembic. Never log the result -- it holds the password."""
        return self.database_url_object().set(drivername="postgresql+psycopg2").render_as_string(hide_password=False)

    def db_endpoint(self) -> dict[str, object]:
        """Where the database is, without credentials. Safe to log."""
        url = self.database_url_object()
        if self.database_url is not None:
            source = "DATABASE_URL"
        elif self.db_host:
            source = "DB_HOST"
        else:
            source = "DATABASE_URL (shared with Spot Bid)"
        return {
            "db_source": source,
            "db_host": url.host,
            "db_port": url.port,
            "db_name": url.database,
            "db_user": url.username,
            "db_schema": self.db_schema,
        }

    def safe_summary(self) -> dict[str, object]:
        """Startup-log-safe view of the configuration. Contains no secrets."""
        return {
            "environment": self.environment.value,
            **self.db_endpoint(),
            "shipments_table": self.shipments_table,
            "turvo_base_url": self.turvo_base_url,
            "dat_env": self.dat_env,
            "gs_enabled": self.gs_enabled,
            "dat_enabled": self.dat_enabled,
            "webhook_auth": "shared-token",
            "dashboard_api_auth": "bearer" if self.dashboard_api_token else "unconfigured",
            "dashboard_cors_origins": self.dashboard_cors_origin_list,
            "refresh_max_age_days": self.refresh_max_age_days,
            "refresh_skip_finalized": self.refresh_skip_finalized,
        }


_UNSET_MESSAGE = (
    "Configuration is incomplete. Copy .env.example to .env and fill in the "
    "required values, or export them in the environment before starting."
)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and validate settings once per process.

    Raises at import/startup time when a required value is missing, so a
    misconfigured deployment fails immediately instead of at first request.
    """
    try:
        return Settings()  # type: ignore[call-arg]
    except Exception as exc:  # pragma: no cover - exercised by deployment, not tests
        raise RuntimeError(f"{_UNSET_MESSAGE}\n\n{exc}") from exc


# --------------------------------------------------------------------------
# Legacy constant access for the T3RA modules (``config.TABLE_NAME`` ...).
# Resolved lazily from Settings so importing this module needs no environment.
# --------------------------------------------------------------------------
def __getattr__(name: str):  # PEP 562
    s = get_settings()
    legacy = {
        "TABLE_NAME": lambda: s.shipments_table,
        "CARRIER_TABLE_NAME": lambda: s.carrier_table,
        "TURVO_BASE_URL": lambda: s.turvo_base_url,
        "TURVO_CLIENT_ID": lambda: s.turvo_client_id,
        "TURVO_CLIENT_SECRET": lambda: s.turvo_client_secret.get_secret_value(),
        "TURVO_API_KEY": lambda: s.turvo_api_key.get_secret_value(),
        "TURVO_USERNAME": lambda: s.turvo_username,
        "TURVO_PASSWORD": lambda: s.turvo_password.get_secret_value(),
        "TOKEN_CACHE_PATH": lambda: s.token_cache_path,
        "AUTH_COOLDOWN_SECONDS": lambda: s.auth_cooldown_seconds,
        "QUEUE_PAYLOAD_RETENTION_DAYS": lambda: s.queue_payload_retention_days,
        "QUEUE_EVENT_RETENTION_DAYS": lambda: s.queue_event_retention_days,
        "TEAMS_WEBHOOK_URL": lambda: s.teams_webhook_url,
        "TRIGGER_STATUSES": lambda: {x.strip().lower() for x in s.completion_statuses.split(",") if x.strip()},
        "DB_HOST": lambda: s.database_url_object().host,
        "DB_PORT": lambda: s.database_url_object().port or 5432,
        "DB_NAME": lambda: s.database_url_object().database,
        "DB_USER": lambda: s.database_url_object().username,
        "DB_PASSWORD": lambda: s.database_url_object().password,
        "WEBHOOK_SHARED_TOKEN": lambda: s.webhook_shared_token.get_secret_value(),
        "WEBHOOK_PATH": lambda: s.webhook_path,
    }
    if name in legacy:
        return legacy[name]()
    raise AttributeError(name)
