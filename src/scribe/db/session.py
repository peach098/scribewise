"""Async SQLAlchemy engine and session factory built from ``Settings``."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from scribe.config import Settings


def create_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Create the async engine (asyncpg). SQL echo stays off: statements may carry content."""
    return create_async_engine(database_url, echo=echo, pool_pre_ping=True)


def create_engine_from_settings(settings: Settings) -> AsyncEngine:
    return create_engine(settings.database_url.get_secret_value())


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
