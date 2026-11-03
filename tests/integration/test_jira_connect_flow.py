"""/tasks → Connect Jira (specs/003-jira-commitments T022, contracts/control-chat.md).

Cloud and self-hosted flows, certificate confirmation, error messages, the project picker cap,
replacement of Trello and disconnect. The fake Jira is served by a respx router behind
``JiraTaskSource(transport_factory=...)``. Synthetic data only."""

from __future__ import annotations

import ipaddress
import ssl
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, SendDocument, SendMessage
from aiogram.types import BufferedInputFile
from cryptography import x509
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import scribe.context.tasks.jira.source as source_module
from scribe.config import Settings
from scribe.context.tasks import service as service_module
from scribe.context.tasks.base import CertInfo, JiraUnreachable
from scribe.context.tasks.jira import JiraTaskSource
from scribe.context.tasks.jira.client import JiraOptions
from scribe.context.tasks.jira.tls import cert_info
from scribe.context.tasks.service import TaskSources
from scribe.context.tasks.trello import TrelloTaskSource
from scribe.db.models import ConnectionStatus, JiraCursorOutcome, TaskProvider, UserState
from scribe.db.repos import jira_cursor
from scribe.db.repos import tasks_conn as tasks_conn_repo
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.registry import LLMRouter
from scribe.telegram.fake_gateway import FakeMessagingGateway
from tests.conftest import FrozenClock, MakeUser
from tests.contract.fixtures.jira import api
from tests.contract.fixtures.jira.api import FakeJira, Flavor
from tests.contract.test_jira_tls_pinning import make_cert
from tests.integration.bot_support import callback_update, text_update
from tests.integration.us1_support import select_all_history
from tests.integration.us2_support import US2Harness, button_texts, callback_datas

_REAL = make_cert("jira.example.com", [x509.DNSName("jira.example.com")])
CERT = cert_info(_REAL.pem)


class _Jira:
    """The fake Jira site plus switches for the TLS case."""

    def __init__(self) -> None:
        self.router = respx.Router(assert_all_called=False)
        self.untrusted = False

    def mount(self, jira: FakeJira) -> None:
        api.mount(self.router, jira)

    def factory(self, _base: str) -> httpx.AsyncBaseTransport:
        def handler(request: httpx.Request) -> Any:
            if self.untrusted:
                raise httpx.ConnectError("verify failed") from ssl.SSLCertVerificationError("x")
            return self.router.handler(request)

        return httpx.MockTransport(handler)


@pytest.fixture
def jira(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Jira]:
    fake = _Jira()

    async def fetch(host: str, port: int, *, address: str | None = None) -> CertInfo:
        return CERT

    async def resolve(*_: Any, **__: Any) -> list[Any]:
        return [ipaddress.ip_address("203.0.113.7")]

    source = JiraTaskSource(JiraOptions(), transport_factory=fake.factory, cert_fetcher=fetch)
    sources = TaskSources({"trello": TrelloTaskSource(), "jira": source})
    monkeypatch.setattr(service_module, "_default", sources)
    monkeypatch.setattr(source_module, "check_address", resolve)
    yield fake


@pytest.fixture
def harness(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    llm_router: LLMRouter,
    fake_llm: FakeAdapter,
    gateway: FakeMessagingGateway,
    clock: FrozenClock,
) -> US2Harness:
    fake_llm.set_response("retrieve.filter_context", select_all_history)
    return US2Harness(session_factory, settings, llm_router, fake_llm, gateway, clock)


def _last(harness: US2Harness) -> SendMessage:
    return harness.messages()[-1]


def _answered(harness: US2Harness) -> str | None:
    return harness.api.of_type(AnswerCallbackQuery)[-1].text


async def _start(harness: US2Harness, chat_id: int, flavor: Flavor) -> None:
    await harness.feed(text_update(chat_id, "/tasks"))
    await harness.feed(callback_update(chat_id, "jira:connect"))
    assert "Where does your Jira run" in _last(harness).text
    assert callback_datas(_last(harness).reply_markup)[:2] == ["jdep:cloud", "jdep:self"]
    await harness.feed(callback_update(chat_id, "jdep:cloud" if flavor == "cloud" else "jdep:self"))
    await harness.feed(text_update(chat_id, "not a url"))
    assert "doesn't look like a Jira address" in _last(harness).text
    await harness.feed(text_update(chat_id, api.site(flavor) + "/"))
    if flavor == "cloud":
        assert "email" in _last(harness).text
        await harness.feed(text_update(chat_id, api.EMAIL))
        assert "read:jira-work" in _last(harness).text
        await harness.feed(text_update(chat_id, api.API_TOKEN))
    else:
        assert "personal access token" in _last(harness).text
        await harness.feed(text_update(chat_id, api.PAT))


@pytest.mark.parametrize("flavor", ["cloud", "self_hosted"])
async def test_connect_pick_projects_and_disconnect(
    flavor: Flavor,
    harness: US2Harness,
    jira: _Jira,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    clock: FrozenClock,
) -> None:
    jira.mount(FakeJira(flavor, projects=[(f"P{n:02d}", f"Project {n}") for n in range(1, 23)]))
    user = await make_user(state=UserState.active)
    chat_id = user.telegram_control_chat_id
    await _start(harness, chat_id, flavor)

    texts = [m.text for m in harness.messages()]
    assert any("Jira connected (read-only)" in t for t in texts)
    assert any("watch" in t for t in texts)  # the scope / autowatch note
    picker = _last(harness)
    assert "Pick the Jira projects" in picker.text
    secret = api.API_TOKEN if flavor == "cloud" else api.PAT
    assert len(harness.api.of_type(DeleteMessage)) == 1  # the token message
    assert all(secret not in m.text for m in harness.messages())

    async with session_factory() as session:
        conn = await tasks_conn_repo.get_connection(session, user.id)
        assert conn is not None and conn.provider == TaskProvider.jira
        assert conn.status == ConnectionStatus.active
        assert conn.config_json["deployment"] == flavor
        assert conn.config_json["site_url"] == api.site(flavor)
        assert conn.config_json["project_keys"] == []
        assert not {"api_token", "pat", "email"} & set(conn.config_json)
        raw = (
            await session.execute(
                text("SELECT credentials::text FROM task_source_connections WHERE user_id = :u"),
                {"u": user.id},
            )
        ).scalar_one()
    assert secret not in raw  # encrypted at rest

    await harness.feed(callback_update(chat_id, "tboard:done"))
    assert _answered(harness) == "Pick at least one project"

    keys = [f"P{n:02d}" for n in range(1, 23)]
    for key in keys[:20]:
        await harness.feed(callback_update(chat_id, f"tboard:{key}"))
    await harness.feed(callback_update(chat_id, f"tboard:{keys[20]}"))
    assert _answered(harness) == "Up to 20 projects"
    await harness.feed(callback_update(chat_id, "tboard:NOPE"))
    async with session_factory() as session:
        conn = await tasks_conn_repo.get_connection(session, user.id)
        assert conn is not None and conn.config_json["project_keys"] == keys[:20]

    await harness.feed(callback_update(chat_id, "tboard:done"))
    status = _last(harness).text
    assert "✅ Jira (" in status and "connected (read-only)" in status
    assert "P01, P02" in status
    async with session_factory() as session:
        conn = await tasks_conn_repo.get_connection(session, user.id)
        assert conn is not None and conn.next_sync_at == clock()
        await jira_cursor.upsert(
            session, user.id, issue_key="P01-1", comment_id="c1", comment_updated_at=clock(),
            body_hmac=b"\x01" * 32, outcome=JiraCursorOutcome.no_commitment,
        )  # fmt: skip
        await session.commit()

    await harness.feed(callback_update(chat_id, "tasks:disconnect"))
    assert "Jira disconnected" in _last(harness).text
    async with session_factory() as session:
        assert await tasks_conn_repo.get_connection(session, user.id) is None
        assert await jira_cursor.count_for_user(session, user.id) == 0


async def test_self_hosted_certificate_confirmation(
    harness: US2Harness,
    jira: _Jira,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    jira.mount(FakeJira("self_hosted"))
    jira.untrusted = True
    user = await make_user(state=UserState.active)
    chat_id = user.telegram_control_chat_id
    await _start(harness, chat_id, "self_hosted")
    card = _last(harness)
    assert "certificate your company issued" in card.text and CERT.sha256 in card.text
    trust = callback_datas(card.reply_markup)[0]
    assert trust == "jcert:" + CERT.sha256.replace(":", "")[:16].lower()
    async with session_factory() as session:
        assert await tasks_conn_repo.get_connection(session, user.id) is None  # not yet

    await harness.feed(callback_update(chat_id, "jcert:0000000000000000"))  # not this card
    async with session_factory() as session:
        assert await tasks_conn_repo.get_connection(session, user.id) is None

    jira.untrusted = False  # with the pinned certificate the handshake succeeds
    await harness.feed(callback_update(chat_id, trust))
    assert any("Jira connected" in m.text for m in harness.messages())
    async with session_factory() as session:
        conn = await tasks_conn_repo.get_connection(session, user.id)
        assert conn is not None
        assert conn.config_json["pinned_cert"]["sha256"] == CERT.sha256
        assert conn.credentials == {"pat": api.PAT}


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ({"myself_status": 401}, "Jira rejected the token"),
        ({"version": (9, 12, 0)}, "Jira 9.12.0 is not supported. Supported: 10.3 and newer"),
    ],
)
async def test_errors_have_their_own_message(
    setup: dict[str, Any],
    expected: str,
    harness: US2Harness,
    jira: _Jira,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    jira.mount(FakeJira("self_hosted", **setup))
    user = await make_user(state=UserState.active)
    await _start(harness, user.telegram_control_chat_id, "self_hosted")
    assert expected in _last(harness).text
    async with session_factory() as session:
        assert await tasks_conn_repo.get_connection(session, user.id) is None


async def test_classic_cloud_token_message(
    harness: US2Harness, jira: _Jira, make_user: MakeUser
) -> None:
    jira.mount(FakeJira("cloud", myself_status=401, site_myself_status=200))
    user = await make_user(state=UserState.active)
    await _start(harness, user.telegram_control_chat_id, "cloud")
    assert "classic token" in _last(harness).text


async def test_unreachable_and_blocked_messages(
    harness: US2Harness, jira: _Jira, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    jira.router.route().mock(side_effect=httpx.ConnectError("refused"))
    user = await make_user(state=UserState.active)
    await _start(harness, user.telegram_control_chat_id, "self_hosted")
    unreachable = _last(harness).text
    assert "couldn't reach this server" in unreachable
    # Issue #61: a private address gets the very same message (no probing oracle).
    jira.router.routes.clear()
    jira.router.route().mock(side_effect=JiraUnreachable("private", blocked=True))
    other = await make_user(state=UserState.active)
    await _start(harness, other.telegram_control_chat_id, "self_hosted")
    assert _last(harness).text == unreachable


async def test_port_outside_the_allowed_list_is_refused(
    harness: US2Harness, jira: _Jira, make_user: MakeUser
) -> None:
    """Issue #61: only 443 and 8443 by default, refused before any connection."""
    user = await make_user(state=UserState.active)
    chat_id = user.telegram_control_chat_id
    await harness.feed(text_update(chat_id, "/tasks"))
    await harness.feed(callback_update(chat_id, "jira:connect"))
    await harness.feed(callback_update(chat_id, "jdep:self"))
    await harness.feed(text_update(chat_id, "https://jira.example.com:8080/jira"))
    assert "only on these ports: 443, 8443" in _last(harness).text
    assert not jira.router.calls


async def test_connection_attempts_are_rate_limited(
    harness: US2Harness,
    jira: _Jira,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    """Issue #61: five checks per user in ten minutes, then a wait message without a check."""
    jira.mount(FakeJira("self_hosted", myself_status=401))
    user = await make_user(state=UserState.active)
    for _ in range(5):
        await _start(harness, user.telegram_control_chat_id, "self_hosted")
        assert "Jira rejected the token" in _last(harness).text
    calls = len(jira.router.calls)
    await _start(harness, user.telegram_control_chat_id, "self_hosted")
    assert "Too many connection attempts. Try again in 10 min" in _last(harness).text
    assert len(jira.router.calls) == calls
    other = await make_user(state=UserState.active)
    await _start(harness, other.telegram_control_chat_id, "self_hosted")
    assert "Jira rejected the token" in _last(harness).text
    async with session_factory() as session:
        assert await tasks_conn_repo.get_connection(session, user.id) is None


async def test_replacing_trello_asks_first(
    harness: US2Harness,
    jira: _Jira,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    jira.mount(FakeJira("cloud"))
    user = await make_user(state=UserState.active)
    chat_id = user.telegram_control_chat_id
    async with session_factory() as session:
        await tasks_conn_repo.upsert_connection(
            session, user.id, provider=TaskProvider.trello, credentials={"key": "k", "token": "t"},
            config_json={"board_ids": ["b1"]},
        )  # fmt: skip
        await session.commit()
    await harness.feed(callback_update(chat_id, "jira:connect"))
    assert "replaces it" in _last(harness).text
    assert button_texts(_last(harness).reply_markup) == ["Replace with Jira", "✖️ Cancel"]
    await harness.feed(callback_update(chat_id, "jira:cancel"))
    async with session_factory() as session:
        conn = await tasks_conn_repo.get_connection(session, user.id)
        assert conn is not None and conn.provider == TaskProvider.trello

    await harness.feed(callback_update(chat_id, "jira:replace"))
    await harness.feed(callback_update(chat_id, "jdep:cloud"))
    await harness.feed(text_update(chat_id, api.CLOUD_SITE))
    await harness.feed(text_update(chat_id, api.EMAIL))
    await harness.feed(text_update(chat_id, api.API_TOKEN))
    async with session_factory() as session:
        conn = await tasks_conn_repo.get_connection(session, user.id)
        assert conn is not None and conn.provider == TaskProvider.jira
        assert "board_ids" not in conn.config_json


async def test_other_command_drops_a_pending_token(
    harness: US2Harness, jira: _Jira, make_user: MakeUser
) -> None:
    jira.mount(FakeJira("self_hosted"))
    jira.untrusted = True
    user = await make_user(state=UserState.active)
    chat_id = user.telegram_control_chat_id
    await _start(harness, chat_id, "self_hosted")
    trust = callback_datas(_last(harness).reply_markup)[0]
    await harness.feed(text_update(chat_id, "/tasks"))  # abandons the card
    jira.untrusted = False
    await harness.feed(callback_update(chat_id, trust))
    assert not any("Jira connected" in m.text for m in harness.messages())


async def _fsm_state(session_factory: async_sessionmaker[AsyncSession], chat_id: int) -> Any:
    async with session_factory() as session:
        return (
            await session.execute(
                text("SELECT state FROM bot_fsm_state WHERE chat_id = :c"), {"c": chat_id}
            )
        ).scalar_one_or_none()


async def test_button_outside_the_flow_drops_a_pending_token(
    harness: US2Harness,
    jira: _Jira,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    """Issue #56: an older 📦 Export tapped at the certificate card clears the pending PAT
    before the export runs, so the file never holds it, and the card no longer connects."""
    jira.mount(FakeJira("self_hosted"))
    jira.untrusted = True
    user = await make_user(state=UserState.active)
    chat_id = user.telegram_control_chat_id
    await _start(harness, chat_id, "self_hosted")
    trust = callback_datas(_last(harness).reply_markup)[0]
    assert await _fsm_state(session_factory, chat_id) == "JiraStates:cert"

    await harness.feed(callback_update(chat_id, "mydata:export"))

    assert await _fsm_state(session_factory, chat_id) is None
    (document,) = harness.api.of_type(SendDocument)
    assert isinstance(document.document, BufferedInputFile)
    exported = document.document.data.decode()
    assert api.PAT not in exported and "pending" not in exported
    assert any("Jira connection cancelled" in m.text for m in harness.messages())
    jira.untrusted = False
    await harness.feed(callback_update(chat_id, trust))
    assert not any("Jira connected" in m.text for m in harness.messages())


async def test_the_flows_own_buttons_keep_the_pending_token(
    harness: US2Harness,
    jira: _Jira,
    session_factory: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
) -> None:
    """A stale certificate button (``jcert:``) is part of the flow: the card still works."""
    jira.mount(FakeJira("self_hosted"))
    jira.untrusted = True
    user = await make_user(state=UserState.active)
    chat_id = user.telegram_control_chat_id
    await _start(harness, chat_id, "self_hosted")
    trust = callback_datas(_last(harness).reply_markup)[0]

    await harness.feed(callback_update(chat_id, "jcert:0000000000000000"))
    assert await _fsm_state(session_factory, chat_id) == "JiraStates:cert"

    jira.untrusted = False
    await harness.feed(callback_update(chat_id, trust))
    assert any("Jira connected" in m.text for m in harness.messages())
