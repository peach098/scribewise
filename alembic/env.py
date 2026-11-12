"""Alembic environment: runs migrations through the app's async engine (scribe.db.session).

The target metadata is ``Base.metadata`` with the core models plus, for each module in
``SCRIBE_PLUGINS``, its ``<module>.models`` submodule when it has one (docs/plugins.md), so
``alembic check`` and autogenerate also see plugin tables.
"""

import asyncio
import os
from logging.config import fileConfig
from typing import Any

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy.engine import Connection

import scribe.db.models  # noqa: F401  (registers every table on Base.metadata)
from scribe.crypto import EncryptedJSON, EncryptedText
from scribe.db.base import Base
from scribe.db.session import create_engine
from scribe.plugins import import_plugin_models, plugin_modules_from_env

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

import_plugin_models(plugin_modules_from_env())
target_metadata = Base.metadata


def _database_url() -> str:
    """SCRIBE_DATABASE_URL from the environment, else the full Settings (which also read .env)."""
    url = os.environ.get("SCRIBE_DATABASE_URL")
    if url:
        return url
    from scribe.config import get_settings

    return get_settings().database_url.get_secret_value()


def render_item(type_: str, obj: Any, autogen_context: AutogenContext) -> str | bool:
    """Render encrypted column types as plain ``sa.Text()``: migrations never import app code."""
    if type_ == "type" and isinstance(obj, EncryptedText | EncryptedJSON):
        return "sa.Text()"
    return False


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to a database."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_item=render_item,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_item=render_item,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_engine(_database_url())
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
