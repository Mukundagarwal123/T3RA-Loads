"""Alembic environment: the database comes from the same settings as the app."""
from alembic import context
from sqlalchemy import create_engine, pool

from t3ra_unified.config import get_settings

target_metadata = None


def run_migrations_online() -> None:
    engine = create_engine(get_settings().sqlalchemy_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("Offline migrations are not supported; run against a database.")
run_migrations_online()
