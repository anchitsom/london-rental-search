import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# Alembic Config object -- provides access to values in alembic.ini.
config = context.config

# Set up Python logging from alembic.ini if present.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import SQLModel metadata so autogenerate can inspect the schema.
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import SQLModel
import homehunt.core.db  # noqa: F401 -- side-effect: registers all SQLModel tables

target_metadata = SQLModel.metadata

# Fall back to the HOMEHUNT_DB env var or homehunt.db in the cwd.
def _get_url():
    db_path = os.environ.get("HOMEHUNT_DB", "homehunt.db")
    url = config.get_main_option("sqlalchemy.url")
    if url and url != "driver://user:pass@localhost/dbname":
        return url
    return f"sqlite:///{db_path}"


def run_migrations_offline() -> None:
    """Run migrations in offline mode (SQL emitted to stdout or file)."""
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = _get_url()

    connectable = engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
