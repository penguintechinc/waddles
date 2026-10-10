"""Alembic environment configuration for WaddleBot migrations."""

import importlib.util
import os
import sys
import types
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool, text
from alembic import context

# Stub the flask_core package so its __init__.py (which eagerly imports pydal,
# authlib, redis, etc.) never runs.  The migration container only ships
# alembic + sqlalchemy + psycopg2 — those heavy deps aren't installed.
# Model submodules import "from flask_core.models import db", so we add
# libs/flask_core to sys.path and replace the flask_core package entry.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'libs', 'flask_core'))
_stub = types.ModuleType('flask_core')
_stub.__path__ = [os.path.join(os.path.dirname(__file__), '..', 'libs', 'flask_core', 'flask_core')]
_stub.__package__ = 'flask_core'
sys.modules['flask_core'] = _stub

# Now import models — only triggers models/__init__.py (flask-sqlalchemy + model defs)
from flask_core.models import db  # noqa: E402

# this is the Alembic Config object
config = context.config

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Use DATABASE_URL environment variable
database_url = os.getenv('DATABASE_URL')
if database_url:
    # Convert postgresql:// to postgresql+psycopg2:// for SQLAlchemy compatibility
    if database_url.startswith('postgresql://'):
        database_url = database_url.replace('postgresql://', 'postgresql+psycopg2://', 1)
    config.set_main_option('sqlalchemy.url', database_url)

# Set up target_metadata for autogenerate
target_metadata = db.metadata


def include_name(name, type_, parent_names):
    """Filter autogenerate to only tables with SQLAlchemy models.

    Prevents Alembic from generating DROP TABLE for tables managed by
    Node.js hub-api or other systems that lack SQLAlchemy models.
    """
    if type_ == "table":
        return name in target_metadata.tables
    return True


def _load_service_roles():
    """Load scripts/db/service_roles.py by path (the migration image ships it beside alembic/)."""
    path = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'db', 'service_roles.py')
    spec = importlib.util.spec_from_file_location('waddles_service_roles_env', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _stage_dev_role_password_suffix(connection) -> None:
    """Bridge WADDLES_DEV_DB_ROLE_PW_SUFFIX into the session GUC legacy SQL 031 reads.

    SECURITY (H-1): the suffix lets docker-compose's db-migrations derive `<role><suffix>`
    dev passwords. `dev_suffix_from_env` raises when WADDLES_DEPLOYMENT_TIER is
    alpha/beta/gamma/production, so this can never take effect on a shared database.
    Always sets the GUC (to '' when unset) so a stale value never leaks across runs.
    """
    roles = _load_service_roles()
    suffix = roles.dev_suffix_from_env()
    connection.execute(
        text("SELECT set_config(:k, :v, false)"),
        {"k": roles.DEV_SUFFIX_GUC, "v": suffix},
    )


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_name=include_name,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode with advisory locking."""
    configuration = config.get_section(config.config_ini_section)
    configuration["sqlalchemy.url"] = config.get_main_option("sqlalchemy.url")

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        # Acquire PostgreSQL advisory lock to prevent concurrent migrations
        connection.execute(text("SELECT pg_advisory_lock(20250001)"))
        try:
            _stage_dev_role_password_suffix(connection)
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                include_name=include_name,
            )

            with context.begin_transaction():
                context.run_migrations()
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(20250001)"))
            connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
