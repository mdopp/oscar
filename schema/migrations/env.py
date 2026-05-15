"""Alembic env — minimal SQLite config.

Hand-written SQL via `op.execute(...)`, no SQLAlchemy ORM models, no
autogenerate. Migrations are portable to Postgres should Phase 3a
require it — see schema/README.md.

DSN resolution (in order):
  1. `-x dburl=...` on the alembic command line
  2. `OSCAR_DB_URL` environment variable
  3. the sqlalchemy.url default in alembic.ini
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool


config = context.config

x_args = context.get_x_argument(as_dictionary=True)
override = x_args.get("dburl") or os.environ.get("OSCAR_DB_URL")
if override:
    config.set_main_option("sqlalchemy.url", override)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
