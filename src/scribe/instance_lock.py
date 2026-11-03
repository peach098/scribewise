"""One Scribe instance per database (#60).

Only one process may run against a database: per-chat ordering uses process-local locks
(``intake/locks.py``), and a second process would start Telethon clients from the same session
strings, so Telegram would raise ``AuthKeyDuplicatedError`` and the sessions would be marked
revoked.

``python -m scribe`` takes a session-level PostgreSQL advisory lock on :data:`LOCK_KEY` with
``pg_try_advisory_lock`` on a dedicated connection before anything else starts, and holds it for
the process lifetime; a second instance finds it taken and exits with code 1. The lock is
released when the process exits (or its connection drops). Only ``__main__.run`` takes it, so
tests that start :class:`scribe.__main__.Service` several times in one process are unaffected.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

# A fixed signed int64 key for the whole application ("scribe" in ASCII).
LOCK_KEY = 0x736372696265


class InstanceLock:
    """The advisory lock and the one connection that holds it."""

    def __init__(self, engine: AsyncEngine, connection: AsyncConnection) -> None:
        self._engine = engine
        self._connection = connection

    @classmethod
    async def try_acquire(cls, database_url: str) -> InstanceLock | None:
        """The held lock, or None when another instance holds it. Database errors propagate."""
        engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
        try:
            connection = await engine.connect()
            try:
                # Autocommit: the lock is session-level, the connection never idles in a
                # transaction.
                await connection.execution_options(isolation_level="AUTOCOMMIT")
                acquired = (
                    await connection.execute(
                        text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}
                    )
                ).scalar_one()
            except BaseException:
                await connection.close()
                raise
        except BaseException:
            await engine.dispose()
            raise
        if not acquired:
            await connection.close()
            await engine.dispose()
            return None
        return cls(engine, connection)

    async def release(self) -> None:
        try:
            await self._connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY}
            )
        finally:
            await self._connection.close()
            await self._engine.dispose()
