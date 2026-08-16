import asyncio
from logging.config import fileConfig
from typing import Any, Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from appcore.infrastructure.sqla.tables import metadata
from appcore.infrastructure.sqla.types import TzAwareDateTime
from appcore.setup.config import load_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The DSN comes from the same settings object the application uses. A second
# connection string in alembic.ini is a second thing to keep in sync, and it
# drifts exactly when it matters -- during a production migration.
config.set_main_option(
    "sqlalchemy.url", load_settings().database.async_dsn.get_secret_value()
)

target_metadata = metadata


def render_item(
    type_: str, obj: Any, autogen_context: AutogenContext
) -> str | Literal[False]:
    """Render custom column types as the SQLAlchemy type they wrap.

    Autogenerate would otherwise emit `appcore.infrastructure.sqla.types.
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
        url=config.get_main_option("sqlalchemy.url"),
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
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
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
