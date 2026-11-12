"""Shared test fixtures (T024).

Unit and contract tests never touch Postgres: the database fixtures below are only instantiated
when an (integration) test requests them. The database is either ``SCRIBE_TEST_DATABASE_URL``
(CI service container) or a throwaway ``postgres:16`` started with testcontainers on a random
port. The schema is created by running the real Alembic migrations once per session.

Every value here is synthetic.
"""

from __future__ import annotations

import base64
import io
import itertools
import json
import logging
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from scribe.config import Settings
from scribe.crypto import configure_keyring, keyring_from_master_key
from scribe.db.base import Base
from scribe.db.models import ChatKind, Contact, ContactKind, User, WorkChat
from scribe.db.repos import chats as chats_repo
from scribe.db.repos import users as users_repo
from scribe.db.session import create_engine
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.pricing import ModelPrice, PriceTable
from scribe.llm.registry import LLMRouter, PromptStore
from scribe.llm.routes import ProviderConfig, RouteConfig, RouteTable, TieredRoute
from scribe.logging import configure_logging
from scribe.telegram.fake_gateway import FakeMessagingGateway

ROOT = Path(__file__).resolve().parent.parent
TEST_MASTER_KEY = bytes(range(32))
TEST_MASTER_KEY_B64 = base64.b64encode(TEST_MASTER_KEY).decode()
FROZEN_NOW = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)  # a Wednesday, inside working hours


# --- guard: only integration tests may use Postgres -----------------------------------------------

DB_FIXTURES = frozenset(
    {
        "database_url",
        "migrated_database_url",
        "db_connection",
        "session_factory",
        "db_session",
        "committed_session_factory",
        "make_user",
        "make_chat",
        "make_contact",
    }
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        used = DB_FIXTURES & set(getattr(item, "fixturenames", ()))
        if used and "integration" not in item.path.parts:
            raise pytest.UsageError(
                f"{item.nodeid} uses database fixtures {sorted(used)}; "
                "only tests/integration may touch Postgres"
            )


# --- encryption ---------------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="session")
def _test_keyring() -> None:
    """Encrypted column types need a process-wide keyring (synthetic test key)."""
    configure_keyring(keyring_from_master_key(TEST_MASTER_KEY))


# --- process-wide defaults (US3) ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_process_defaults() -> Iterator[None]:
    """The task-source cache, the default commitment extractor, the deadline memo (#73) and
    plugin texts (docs/plugins.md) never leak between tests."""
    from scribe.context.commitments import reset_default_extractor
    from scribe.context.tasks.deadlines import default_memo
    from scribe.context.tasks.service import reset_default_task_sources
    from scribe.i18n import clear_plugin_messages

    reset_default_task_sources()
    reset_default_extractor()
    default_memo().clear()
    clear_plugin_messages()
    yield
    reset_default_task_sources()
    reset_default_extractor()
    default_memo().clear()
    clear_plugin_messages()


# --- logging ------------------------------------------------------------------------------------


class LogCapture:
    def __init__(self, stream: io.StringIO) -> None:
        self.stream = stream

    @property
    def raw(self) -> str:
        return self.stream.getvalue()

    def events(self, name: str | None = None) -> list[dict[str, Any]]:
        lines = [json.loads(line) for line in self.raw.splitlines() if line.strip()]
        return [line for line in lines if name is None or line.get("event") == name]


@pytest.fixture
def log_capture() -> Iterator[LogCapture]:
    """JSON log lines produced by the real logging configuration (with redaction)."""
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    yield LogCapture(stream)
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()


# --- settings / clock ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    """Settings with a 0-0 reading delay so flows run without waiting."""
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        master_key=TEST_MASTER_KEY_B64,
        bot_token="123456:TEST-TOKEN-NOT-REAL",
        tg_api_id=1,
        tg_api_hash="test-api-hash",
        delay_min_s=0,
        delay_max_s=0,
        open_bot=True,  # issue #53: the tests run ungated on purpose
    )


class FrozenClock:
    """Callable clock that only moves when told to."""

    def __init__(self, now: datetime = FROZEN_NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> datetime:
        self.now += timedelta(**delta)
        return self.now

    def set(self, now: datetime) -> None:
        self.now = now


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


# --- LLM / Telegram fakes -----------------------------------------------------------------------

FAKE_PROMPTS = {
    "filter_context": "Select relevant context ids.",
    "draft": "Draft a reply in the user's style.",
    "style_profile": "Build a style profile.",
    "commitments": "Extract commitments.",
    "deadlines": "Extract task deadlines.",
    "style_judge": "Judge style similarity.",
    "reply_needed": "Decide whether the turn needs a reply.",
}


def fake_route_table() -> RouteTable:
    small = RouteConfig(provider="fake", model="fake-small", effort="low", max_tokens=1000)
    large = RouteConfig(provider="fake", model="fake-large", effort="medium", max_tokens=2000)
    return RouteTable(
        routes={
            "retrieve.filter_context": small,
            "classify.reply_needed": small,
            "extract.commitments": small,
            "extract.deadlines": small,
            "compose.draft": TieredRoute(tiers={"quality": large, "economy": small}),
            "profile.build": large.model_copy(update={"prefer_batch": True}),
            "eval.style_judge": small,
        },
        providers={"fake": ProviderConfig(data_use_confirmed=True)},
    )


def fake_price_table() -> PriceTable:
    price = ModelPrice(input_per_m=1.0, output_per_m=2.0, cached_input_per_m=0.1)
    return PriceTable(models={"fake-small": price, "fake-large": price})


@pytest.fixture
def prompts_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "prompts"
    directory.mkdir()
    for name, body in FAKE_PROMPTS.items():
        (directory / f"{name}.md").write_text(f"---\nversion: 1\n---\n{body}\n", encoding="utf-8")
    return directory


@pytest.fixture
def fake_llm() -> FakeAdapter:
    return FakeAdapter()


@pytest.fixture
def llm_router(fake_llm: FakeAdapter, prompts_dir: Path) -> LLMRouter:
    return LLMRouter(
        routes=fake_route_table(),
        prices=fake_price_table(),
        adapters={"fake": fake_llm},
        prompts=PromptStore(prompts_dir),
    )


@pytest.fixture
def gateway() -> FakeMessagingGateway:
    return FakeMessagingGateway()


# --- Postgres -----------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    url = os.environ.get("SCRIBE_TEST_DATABASE_URL")
    if url:
        yield url
        return
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:16", driver="asyncpg") as postgres:
        yield postgres.get_connection_url()


@pytest.fixture(scope="session")
def migrated_database_url(database_url: str) -> str:
    """Apply the real migrations once per session (in a subprocess: env.py runs its own loop)."""
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "heads"],
        cwd=ROOT,
        env={**os.environ, "SCRIBE_DATABASE_URL": database_url},
        check=True,
        capture_output=True,
    )
    return database_url


@pytest.fixture
async def db_connection(migrated_database_url: str) -> AsyncIterator[AsyncConnection]:
    """One connection per test inside an outer transaction that is rolled back afterwards."""
    engine = create_engine(migrated_database_url)
    connection = await engine.connect()
    transaction = await connection.begin()
    try:
        yield connection
    finally:
        await transaction.rollback()
        await connection.close()
        await engine.dispose()


@pytest.fixture
def session_factory(db_connection: AsyncConnection) -> async_sessionmaker[AsyncSession]:
    """Sessions bound to the test transaction; their ``commit()`` only releases a savepoint.

    All sessions share one connection, so use them sequentially, not concurrently.
    """
    return async_sessionmaker(
        bind=db_connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )


@pytest.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest.fixture
async def committed_session_factory(
    migrated_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Real commits on a pooled engine (for row-locking / concurrency tests).

    Every table is truncated afterwards, so these tests do not leak rows.
    """
    engine = create_engine(migrated_database_url)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {tables} CASCADE"))
        await engine.dispose()


# --- factories ----------------------------------------------------------------------------------

_ids = itertools.count(10_000_000)


def next_telegram_id() -> int:
    return next(_ids)


MakeUser = Callable[..., Awaitable[User]]
MakeChat = Callable[..., Awaitable[WorkChat]]
MakeContact = Callable[..., Awaitable[Contact]]


@pytest.fixture
def make_user(db_session: AsyncSession, settings: Settings) -> MakeUser:
    async def _make(**values: Any) -> User:
        control_chat_id = values.pop("control_chat_id", None) or next_telegram_id()
        user = await users_repo.create_user(
            db_session,
            control_chat_id=control_chat_id,
            settings=settings,
            display_name=values.pop("display_name", "Test User"),
        )
        for key, value in values.items():
            setattr(user, key, value)
        await db_session.flush()
        return user

    return _make


@pytest.fixture
def make_chat(db_session: AsyncSession) -> MakeChat:
    async def _make(user: User, **values: Any) -> WorkChat:
        return await chats_repo.upsert_work_chat(
            db_session,
            user.id,
            telegram_chat_id=values.pop("telegram_chat_id", None) or next_telegram_id(),
            title=values.pop("title", "Synthetic chat"),
            kind=values.pop("kind", ChatKind.private),
            enabled=values.pop("enabled", True),
            **values,
        )

    return _make


@pytest.fixture
def make_contact(db_session: AsyncSession) -> MakeContact:
    async def _make(user: User, **values: Any) -> Contact:
        return await chats_repo.upsert_contact(
            db_session,
            user.id,
            telegram_peer_id=values.pop("telegram_peer_id", None) or next_telegram_id(),
            kind=values.pop("kind", ContactKind.person),
            display_name=values.pop("display_name", "Synthetic Contact"),
        )

    return _make
