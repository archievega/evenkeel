import asyncio
from logging.config import fileConfig
from typing import Any, Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from evenkeel.infrastructure.sqla.tables import metadata
from evenkeel.infrastructure.sqla.types import TzAwareDateTime
from evenkeel.setup.config import load_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The DSN comes from the same settings object the application uses. A second
# connection string in alembic.ini is a second thing to keep in sync, and it
# drifts exactly when it matters -- during a production migration.
#
# A URL supplied by the caller wins. Overwriting it unconditionally makes the
# `sqlalchemy.url` on a Config object silently useless, so `command.upgrade()`
# against a throwaway test database quietly migrates whatever the environment
# points at instead -- which is the production database on the machine of
# whoever runs the suite with a real .env loaded.
# Read, never written back. Putting the DSN into the ini means ConfigParser
# interpolation sees it, and a `%` in the password raises a ValueError whose
# message contains the whole plaintext DSN — printed to stderr by a failing
# `alembic upgrade head`, straight into CI and container logs, bypassing the
# logging redaction entirely.
DATABASE_URL = (
    config.get_main_option("sqlalchemy.url", None)
    or load_settings().database.async_dsn.get_secret_value()
)

target_metadata = metadata


def render_item(
    type_: str, obj: Any, autogen_context: AutogenContext
) -> str | Literal[False]:
    """Render custom column types as the SQLAlchemy type they wrap.

    Autogenerate would otherwise emit `evenkeel.infrastructure.sqla.types.
    TzAwareDateTime` and add an import of application code to the migration.
    A migration must keep behaving exactly as it did the day it was written,
    and one that imports the app changes meaning every time the app does --
    then fails outright once the type is renamed or moved, taking `upgrade` on
    a fresh database with it.
    """
    if type_ == "type" and isinstance(obj, TzAwareDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        # Both are off by default, and both are what catch a column whose type
        # or default drifted from the model without anyone noticing.
        compare_type=True,
        compare_server_default=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: object) -> None:
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = create_async_engine(
        DATABASE_URL,
        # Migrations are a short-lived one-shot; a pool would keep connections
        # open past the process's usefulness.
        poolclass=NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
