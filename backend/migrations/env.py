"""Alembic environment. Run migrations with: alembic upgrade head"""

from logging.config import fileConfig

from alembic import context

from app.config import get_settings
from app.db import build_engine
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    # Alembic can't compare expression indexes (the full-text search indexes), so it would
    # report them as changed on every run. They're created by migration 0001.
    if type_ == "index" and name and name.endswith("_fts"):
        return False
    return True


def _url() -> str:
    # Tests pass the URL through the Alembic config; normal runs read DATABASE_URL.
    return config.attributes.get("database_url") or get_settings().database_url


def run_migrations_offline() -> None:
    from app.db import normalize_database_url

    context.configure(
        url=normalize_database_url(_url()),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = build_engine(_url(), pool_size=1)
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, include_object=include_object
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
