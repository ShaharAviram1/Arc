"""Alembic environment.

The database URL comes from ``arc.config.Settings`` (i.e. from
``DATABASE_URL``), never from ``alembic.ini``, so migrations and the app can
never disagree about which database they are pointed at. The one exception is
a caller that has already set ``sqlalchemy.url`` on the config object — the
test suite does this to point migrations at its throwaway database — which
wins over the environment.

``target_metadata`` is ``arc.models.Base.metadata``. Importing ``arc.models``
(not ``arc.db``) is what registers every table.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from arc.config import get_settings
from arc.models import Base

config = context.config

if config.config_file_name is not None:
    # ``disable_existing_loggers=False``: this file is imported in-process by
    # the test suite (which runs ``alembic upgrade head`` to build the test
    # database), and the default would switch off every logger created before
    # that point — i.e. every ``arc.*`` module already imported, for the rest
    # of the run. Migrations should configure logging, not silence the
    # application.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# The placeholder in alembic.ini ("driver://user:pass@localhost/dbname") is
# never a real target, so anything else means a caller injected a URL.
_PLACEHOLDER_URL = "driver://user:pass@localhost/dbname"
if config.get_main_option("sqlalchemy.url", _PLACEHOLDER_URL) == _PLACEHOLDER_URL:
    # `%` is ConfigParser's interpolation character, so a URL containing one
    # (a percent-encoded password, say) must be escaped before it goes in.
    config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
