"""Jira wiring for integration tests (specs/003-jira-commitments): a fake Jira site behind the
process-wide task sources, and a stored Jira connection. Synthetic data only."""

from __future__ import annotations

from collections.abc import Sequence

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.context.tasks import service as service_module
from scribe.context.tasks.jira import JiraTaskSource
from scribe.context.tasks.jira.client import JiraOptions
from scribe.context.tasks.service import TaskSources
from scribe.context.tasks.trello import TrelloTaskSource
from scribe.db.models import TaskProvider, User
from scribe.db.repos import tasks_conn as tasks_conn_repo
from tests.contract.fixtures.jira import api
from tests.contract.fixtures.jira.api import FakeJira, Flavor


class JiraSite:
    """A respx router every Jira client of the process talks to."""

    def __init__(self) -> None:
        self.router = respx.Router(assert_all_called=False)
        self.source = JiraTaskSource(JiraOptions(), transport_factory=self.factory)
        self.fake: FakeJira | None = None

    def factory(self, _base: str) -> httpx.AsyncBaseTransport:
        return httpx.MockTransport(self.router.async_handler)

    def serve(self, fake: FakeJira) -> FakeJira:
        self.fake = fake
        api.mount(self.router, fake)
        return fake


def install_jira_site(monkeypatch: pytest.MonkeyPatch) -> JiraSite:
    site = JiraSite()
    sources = TaskSources({"trello": TrelloTaskSource(), "jira": site.source})
    monkeypatch.setattr(service_module, "_default", sources)
    return site


async def connect_jira(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
    flavor: Flavor = "cloud",
    project_keys: Sequence[str] = ("PAY",),
) -> None:
    async with session_factory() as session:
        await tasks_conn_repo.upsert_connection(
            session,
            user.id,
            provider=TaskProvider.jira,
            credentials={
                k: v
                for k, v in api.credentials(flavor).items()
                if k in {"email", "api_token", "pat"}
            },
            config_json=api.config(flavor, project_keys),
        )
        await session.commit()
