"""Telethon user client (T018) against a fake Telethon client. Synthetic data, no network."""

from __future__ import annotations

import importlib.metadata
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import errors, functions, types, utils
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from scribe.config import Settings
from scribe.telegram.gateway import (
    AlreadyDelivered,
    GatewayBusy,
    GatewayError,
    IncomingMessage,
    OutgoingEvent,
    ReadEvent,
    SessionRevoked,
)
from scribe.telegram.user_client import (
    UserClient,
    dialog_kind,
    forward_origin,
    has_real_media,
    is_own_message,
    mentions_me,
    peer_kind_of,
    read_event_from_update,
    replies_to_message,
    sent_message_id,
)

USER = uuid.uuid4()


def _synthetic_session_string() -> str:
    session = StringSession()
    session.set_dc(2, "127.0.0.1", 443)
    session.auth_key = AuthKey(bytes(256))  # all-zero synthetic key, never used on a network
    return str(session.save())


SESSION = _synthetic_session_string()
T0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def _message(**values: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": 501,
        "message": "Synthetic question?",
        "media": None,
        "date": T0,
        "out": False,
        "reply_to": None,
        "mentioned": False,
    }
    base.update(values)
    return SimpleNamespace(**base)


class FakeTelethon:
    def __init__(self) -> None:
        self.init_kwargs: dict[str, Any] = {}
        self.authorized = True
        self.handlers: list[tuple[Any, Any]] = []
        self.read_acks: list[tuple[Any, int]] = []
        self.requests: list[Any] = []
        self.attempts: list[Any] = []  # every raw request, including failed ones
        # What a raw request returns (``send_text`` sends ``messages.sendMessage`` raw, #54).
        self.request_result: Any = types.UpdateShortSentMessage(
            out=True, id=777, pts=1, pts_count=1, date=T0
        )
        self.errors: list[Exception] = []
        self.known_entities: set[int] = set()
        self.dialogs_loaded = 0
        self.dialogs: list[Any] = []
        self.history: list[Any] = []
        self.disconnected = False
        self.connected = False
        self.logged_out = False
        self.log_out_error: Exception | None = None

    async def connect(self) -> None:
        self.connected = True

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_me(self) -> Any:
        return SimpleNamespace(id=42)

    async def disconnect(self) -> None:
        self.disconnected = True

    async def log_out(self) -> bool:
        if self.log_out_error is not None:
            raise self.log_out_error
        self.logged_out = True
        return True

    def add_event_handler(self, callback: Any, event: Any) -> None:
        self.handlers.append((callback, event))

    async def get_input_entity(self, chat_id: int) -> Any:
        if chat_id not in self.known_entities:
            raise ValueError("Could not find the input entity")
        return ("entity", chat_id)

    async def get_dialogs(self) -> list[Any]:
        self.dialogs_loaded += 1
        self.known_entities.update(d.id for d in self.dialogs)
        return self.dialogs

    def _maybe_fail(self) -> None:
        if self.errors:
            raise self.errors.pop(0)

    async def send_read_acknowledge(self, entity: Any, *, max_id: int) -> bool:
        self._maybe_fail()
        self.read_acks.append((entity, max_id))
        return True

    async def __call__(self, request: Any) -> Any:
        self.attempts.append(request)
        self._maybe_fail()
        self.requests.append(request)
        return self.request_result

    @property
    def sent(self) -> list[tuple[Any, str, int | None]]:
        return [
            (r.peer, r.message, r.reply_to.reply_to_msg_id if r.reply_to else None)
            for r in self.requests
            if isinstance(r, functions.messages.SendMessageRequest)
        ]

    async def iter_dialogs(self) -> Any:
        for dialog in self.dialogs:
            yield dialog

    async def iter_messages(
        self, entity: Any, *, from_user: str | None = None, limit: int | None = None
    ) -> Any:
        for message in self.history[:limit]:
            if from_user == "me" and not message.out:
                continue
            yield message


class Recorder:
    def __init__(self) -> None:
        self.incoming: list[IncomingMessage] = []
        self.outgoing: list[OutgoingEvent] = []
        self.reads: list[ReadEvent] = []

    async def on_new(self, message: IncomingMessage) -> None:
        self.incoming.append(message)

    async def on_out(self, event: OutgoingEvent) -> None:
        self.outgoing.append(event)

    async def on_read(self, event: ReadEvent) -> None:
        self.reads.append(event)


async def _started(fake: FakeTelethon | None = None) -> tuple[UserClient, FakeTelethon, Recorder]:
    fake = fake or FakeTelethon()
    recorder = Recorder()
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = UserClient(
        USER,
        SESSION,
        api_id=1,
        api_hash="hash",
        on_new_message=recorder.on_new,
        on_outgoing=recorder.on_out,
        on_read_elsewhere=recorder.on_read,
        client_factory=lambda *args, **kwargs: (fake.init_kwargs.update(kwargs), fake)[1],
        sleep=sleep,
    )
    client.sleeps = sleeps  # type: ignore[attr-defined]
    await client.start()
    return client, fake, recorder


def _event(message: Any, *, chat_id: int, is_private: bool, **extra: Any) -> SimpleNamespace:
    sender = types.User(id=1001, first_name="Synthetic", last_name="Colleague")
    chat = types.Chat(
        id=555,
        title="Synthetic Team",
        photo=types.ChatPhotoEmpty(),
        participants_count=3,
        date=T0,
        version=1,
    )

    async def get_sender() -> Any:
        return sender

    async def get_chat() -> Any:
        return chat

    async def get_reply_message() -> Any:
        return extra.get("reply")

    return SimpleNamespace(
        message=message,
        chat_id=chat_id,
        sender_id=extra.get("sender_id", 1001),
        is_private=is_private,
        get_sender=get_sender,
        get_chat=get_chat,
        get_reply_message=get_reply_message,
    )


async def test_start_registers_handlers_with_flood_threshold() -> None:
    client, fake, _ = await _started()
    assert client.me_id == 42
    assert fake.init_kwargs["flood_sleep_threshold"] == 60
    assert len(fake.handlers) == 2


async def test_telethon_client_receives_device_params() -> None:
    fake = FakeTelethon()
    client = UserClient(
        USER,
        SESSION,
        api_id=1,
        api_hash="hash",
        on_new_message=Recorder().on_new,
        on_outgoing=Recorder().on_out,
        on_read_elsewhere=Recorder().on_read,
        device_model="Synthetic Device",
        system_version="synthetic-os",
        app_version="0.0.0-test",
        client_factory=lambda *args, **kwargs: (fake.init_kwargs.update(kwargs), fake)[1],
    )
    await client.start()
    assert fake.init_kwargs["device_model"] == "Synthetic Device"
    assert fake.init_kwargs["system_version"] == "synthetic-os"
    assert fake.init_kwargs["app_version"] == "0.0.0-test"

    fake.init_kwargs.clear()
    await client.stop()
    await client.log_out()  # the temporary log-out client is built the same way
    assert fake.init_kwargs["device_model"] == "Synthetic Device"


async def test_default_device_params_match_settings_defaults() -> None:
    _, fake, _ = await _started()
    assert fake.init_kwargs["device_model"] == Settings.model_fields["tg_device_model"].default
    assert fake.init_kwargs["system_version"] == Settings.model_fields["tg_system_version"].default
    assert fake.init_kwargs["app_version"] == importlib.metadata.version("scribe")


async def test_unauthorised_session_raises_session_revoked() -> None:
    fake = FakeTelethon()
    fake.authorized = False
    with pytest.raises(SessionRevoked):
        await _started(fake)
    assert fake.disconnected


async def test_incoming_private_message_is_mapped_and_not_marked_read() -> None:
    _, fake, recorder = await _started()
    new_message_handler = fake.handlers[0][0]
    await new_message_handler(_event(_message(), chat_id=1001, is_private=True))

    (incoming,) = recorder.incoming
    assert incoming.user_id == USER
    assert (incoming.chat_id, incoming.message_id, incoming.sender_peer_id) == (1001, 501, 1001)
    assert incoming.sender_display_name == "Synthetic Colleague"
    assert incoming.text == "Synthetic question?"
    assert incoming.chat_kind == "private" and not incoming.has_media
    assert fake.read_acks == []  # receiving never acknowledges reads


async def test_group_reply_to_me_and_mentions() -> None:
    _, fake, recorder = await _started()
    message = _message(
        reply_to=SimpleNamespace(reply_to_msg_id=400),
        mentioned=True,
        media=types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=1)),
    )
    reply = SimpleNamespace(out=True)
    await fake.handlers[0][0](_event(message, chat_id=-100555, is_private=False, reply=reply))
    (incoming,) = recorder.incoming
    assert incoming.chat_kind == "group" and incoming.chat_title == "Synthetic Team"
    assert incoming.reply_to_id == 400
    assert incoming.mentions_me and incoming.is_reply_to_me
    assert incoming.has_media is False  # link previews are text messages


def test_mentions_me_ignores_forwarded_copies() -> None:
    # The server flag covers @username (MessageEntityMention) and name links
    # (MessageEntityMentionName) alike, so it drives the answer.
    for entity in (
        types.MessageEntityMention(offset=0, length=9),
        types.MessageEntityMentionName(offset=0, length=9, user_id=42),
    ):
        tagged = _message(mentioned=True, entities=[entity], fwd_from=None)
        assert mentions_me(tagged)
        forwarded = _message(
            mentioned=True, entities=[entity], fwd_from=types.MessageFwdHeader(date=T0)
        )
        assert not mentions_me(forwarded)
    assert not mentions_me(_message(mentioned=False))
    assert mentions_me(_message(mentioned=True))  # no fwd_from attribute at all


def test_replies_to_message_ignores_topic_headers_and_other_peers() -> None:
    chat_id = utils.get_peer_id(types.PeerChannel(555))  # -1000000000555
    topic_post = types.MessageReplyHeader(
        forum_topic=True, reply_to_msg_id=300, reply_to_top_id=None
    )
    assert not replies_to_message(_message(reply_to=topic_post), chat_id=chat_id)
    in_topic = types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=400, reply_to_top_id=300)
    assert replies_to_message(_message(reply_to=in_topic), chat_id=chat_id)
    plain = types.MessageReplyHeader(reply_to_msg_id=400)
    assert replies_to_message(_message(reply_to=plain), chat_id=chat_id)
    same_peer = types.MessageReplyHeader(
        reply_to_msg_id=400, reply_to_peer_id=types.PeerChannel(555)
    )
    assert replies_to_message(_message(reply_to=same_peer), chat_id=chat_id)
    other_peer = types.MessageReplyHeader(
        reply_to_msg_id=400, reply_to_peer_id=types.PeerChannel(777)
    )
    assert not replies_to_message(_message(reply_to=other_peer), chat_id=chat_id)
    story = types.MessageReplyStoryHeader(peer=types.PeerUser(1001), story_id=5)
    assert not replies_to_message(_message(reply_to=story), chat_id=chat_id)
    assert not replies_to_message(_message(), chat_id=chat_id)


def test_is_own_message_requires_an_ordinary_outgoing_message() -> None:
    assert is_own_message(SimpleNamespace(out=True, action=None))
    assert is_own_message(SimpleNamespace(out=True))
    assert not is_own_message(SimpleNamespace(out=False, action=None))
    topic_created = types.MessageActionTopicCreate(title="Synthetic topic", icon_color=0)
    assert not is_own_message(SimpleNamespace(out=True, action=topic_created))
    assert not is_own_message(None)


class _ReplyProbe:
    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.calls = 0

    async def __call__(self) -> Any:
        self.calls += 1
        return self.reply


async def _group_incoming(message: Any, reply: Any) -> tuple[IncomingMessage, int]:
    _, fake, recorder = await _started()
    event = _event(message, chat_id=-100555, is_private=False)
    probe = _ReplyProbe(reply)
    event.get_reply_message = probe
    await fake.handlers[0][0](event)
    (incoming,) = recorder.incoming
    return incoming, probe.calls


async def test_topic_post_is_not_a_reply_to_me() -> None:
    # The user created the topic: its service message is ``out``, but posting there is not a
    # reply to the user.
    header = types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=300, reply_to_top_id=None)
    service = SimpleNamespace(out=True, action=SimpleNamespace())
    incoming, calls = await _group_incoming(_message(reply_to=header), service)
    assert calls == 0
    assert not incoming.is_reply_to_me
    assert incoming.reply_to_id == 300  # stored as before


async def test_reply_inside_a_topic_to_my_message() -> None:
    header = types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=400, reply_to_top_id=300)
    incoming, calls = await _group_incoming(
        _message(reply_to=header), SimpleNamespace(out=True, action=None)
    )
    assert calls == 1
    assert incoming.is_reply_to_me


async def test_reply_to_a_service_message_or_another_peer_is_not_to_me() -> None:
    header = types.MessageReplyHeader(reply_to_msg_id=400)
    service = SimpleNamespace(out=True, action=SimpleNamespace())
    incoming, _ = await _group_incoming(_message(reply_to=header), service)
    assert not incoming.is_reply_to_me

    other = types.MessageReplyHeader(reply_to_msg_id=400, reply_to_peer_id=types.PeerChannel(777))
    incoming, calls = await _group_incoming(
        _message(reply_to=other), SimpleNamespace(out=True, action=None)
    )
    assert calls == 0
    assert not incoming.is_reply_to_me


async def test_forwarded_tag_is_not_a_mention() -> None:
    message = _message(mentioned=True, fwd_from=types.MessageFwdHeader(date=T0))
    incoming, _ = await _group_incoming(message, None)
    assert not incoming.mentions_me


def test_forward_origin_label_without_network() -> None:
    # #47: a hidden account's name from the header, else the resolved sender or channel.
    assert forward_origin(_message()) is None
    assert forward_origin(_message(fwd_from=None)) is None
    hidden = types.MessageFwdHeader(date=T0, from_name="Synthetic Hidden")
    assert forward_origin(_message(fwd_from=hidden)) == "Synthetic Hidden"
    header = types.MessageFwdHeader(date=T0, from_id=types.PeerUser(42))
    person = types.User(id=42, first_name="Synthetic", last_name="Origin")
    by_user = _message(fwd_from=header, forward=SimpleNamespace(sender=person, chat=None))
    assert forward_origin(by_user) == "Synthetic Origin"
    channel = types.Channel(
        id=7, title="Synthetic Channel", photo=types.ChatPhotoEmpty(), date=T0, access_hash=1
    )
    by_channel = _message(fwd_from=header, forward=SimpleNamespace(sender=None, chat=channel))
    assert forward_origin(by_channel) == "Synthetic Channel"
    assert forward_origin(_message(fwd_from=header)) is None  # origin not resolved: unknown


async def test_forwarded_messages_are_flagged_both_ways() -> None:
    _, fake, recorder = await _started()
    header = types.MessageFwdHeader(date=T0, from_name="Synthetic Hidden")
    await fake.handlers[0][0](
        _event(_message(id=8, fwd_from=header), chat_id=1001, is_private=True)
    )
    await fake.handlers[0][0](
        _event(_message(id=9, out=True, fwd_from=header), chat_id=1001, is_private=True)
    )
    (incoming,) = recorder.incoming
    assert (incoming.is_forwarded, incoming.forwarded_from) == (True, "Synthetic Hidden")
    (outgoing,) = recorder.outgoing
    assert (outgoing.is_forwarded, outgoing.forwarded_from) == (True, "Synthetic Hidden")
    await fake.handlers[0][0](_event(_message(id=10), chat_id=1001, is_private=True))
    assert (recorder.incoming[-1].is_forwarded, recorder.incoming[-1].forwarded_from) == (
        False,
        None,
    )


async def test_outgoing_message_goes_to_on_outgoing() -> None:
    _, fake, recorder = await _started()
    await fake.handlers[0][0](_event(_message(out=True, id=9), chat_id=1001, is_private=True))
    assert recorder.incoming == []
    (event,) = recorder.outgoing
    assert (event.chat_id, event.message_id) == (1001, 9)


async def test_handler_errors_are_contained() -> None:
    _, fake, recorder = await _started()
    await fake.handlers[0][0](SimpleNamespace())  # malformed event must not raise
    assert recorder.incoming == []


async def test_read_updates_map_to_read_events() -> None:
    _, fake, recorder = await _started()
    raw_handler = fake.handlers[1][0]
    await raw_handler(
        types.UpdateReadHistoryInbox(
            peer=types.PeerUser(1001), max_id=510, still_unread_count=0, pts=1, pts_count=1
        )
    )
    await raw_handler(
        types.UpdateReadChannelInbox(channel_id=555, max_id=77, still_unread_count=0, pts=1)
    )
    assert recorder.reads == [
        ReadEvent(user_id=USER, chat_id=1001, max_message_id=510),
        ReadEvent(user_id=USER, chat_id=-1000000000555, max_message_id=77),
    ]
    assert read_event_from_update(USER, types.UpdateConfig()) is None


async def test_mark_read_resolves_entity_via_dialogs_and_acknowledges() -> None:
    client, fake, _ = await _started()
    fake.dialogs = [SimpleNamespace(id=1001)]
    await client.mark_read(1001, 510)
    assert fake.dialogs_loaded == 1
    assert fake.read_acks == [(("entity", 1001), 510)]


async def test_send_text_sends_the_approved_text_verbatim() -> None:
    client, fake, _ = await _started()
    fake.known_entities.add(1001)
    text = "see my__config__file and **this** [invoice](https://example.com)"
    assert await client.send_text(1001, text, reply_to=None, random_id=-5) == 777
    assert fake.sent == [(("entity", 1001), text, None)]
    (request,) = fake.requests
    assert request.entities == []  # no Markdown: the contact gets exactly this text
    assert request.no_webpage is False  # link previews as with send_message's default
    assert request.random_id == -5


async def test_short_flood_wait_sleeps_and_retries() -> None:
    client, fake, _ = await _started()
    fake.known_entities.add(1001)
    fake.errors = [errors.FloodWaitError(request=None, capture=30)]
    assert await client.send_text(1001, "Synthetic reply", reply_to=501, random_id=9) == 777
    assert client.sleeps == [30]  # type: ignore[attr-defined]
    assert fake.sent == [(("entity", 1001), "Synthetic reply", 501)]

    first, second = fake.attempts  # the retry after the flood wait reuses the random_id
    assert first.random_id == second.random_id == 9


async def test_send_text_reads_the_id_from_updates_by_random_id() -> None:
    client, fake, _ = await _started()
    fake.known_entities.add(1001)
    new_message = types.UpdateNewChannelMessage(
        message=types.Message(id=812, peer_id=types.PeerChannel(1001), date=T0, message="x"),
        pts=1,
        pts_count=1,
    )
    fake.request_result = types.Updates(
        updates=[types.UpdateMessageID(id=811, random_id=4), new_message],
        users=[],
        chats=[],
        date=T0,
        seq=0,
    )
    assert await client.send_text(1001, "x", reply_to=None, random_id=4) == 811


def test_sent_message_id_falls_back_to_the_new_message() -> None:
    message = types.Message(id=5, peer_id=types.PeerUser(1), date=T0, message="x")
    update = types.UpdateNewMessage(message=message, pts=1, pts_count=1)
    assert sent_message_id(types.UpdateShort(update=update, date=T0), 3) == 5
    with pytest.raises(GatewayError):
        sent_message_id(types.UpdatesTooLong(), 3)


async def test_duplicate_random_id_returns_the_delivered_message() -> None:
    """#54: an earlier attempt was delivered; Telegram refuses the retry's random_id. The
    delivered message is found by its text and nothing is sent again."""
    client, fake, _ = await _started()
    fake.known_entities.add(1001)
    fake.errors = [errors.RandomIdDuplicateError(request=None)]
    fake.history = [
        _message(id=900, message="Synthetic reply", out=False),
        _message(id=901, message="Synthetic reply", out=True),
    ]
    assert await client.send_text(1001, "Synthetic reply", reply_to=None, random_id=4) == 901
    assert fake.requests == []


async def test_duplicate_random_id_lookup_ignores_surrounding_whitespace() -> None:
    """Telegram stores the text trimmed: a draft ending in a newline is still found."""
    client, fake, _ = await _started()
    fake.known_entities.add(1001)
    fake.errors = [errors.RandomIdDuplicateError(request=None)]
    fake.history = [_message(id=902, message="Synthetic\nreply", out=True)]
    assert await client.send_text(1001, "Synthetic\r\nreply\n", reply_to=None, random_id=4) == 902
    assert fake.requests == []


async def test_duplicate_random_id_without_the_message_raises_already_delivered() -> None:
    client, fake, _ = await _started()
    fake.known_entities.add(1001)
    fake.errors = [errors.RandomIdDuplicateError(request=None)]
    with pytest.raises(AlreadyDelivered):
        await client.send_text(1001, "Synthetic reply", reply_to=None, random_id=4)


async def test_long_flood_wait_raises_gateway_busy() -> None:
    client, fake, _ = await _started()
    fake.known_entities.add(1001)
    fake.errors = [errors.FloodWaitError(request=None, capture=600)]
    with pytest.raises(GatewayBusy) as info:
        await client.mark_read(1001, 1)
    assert info.value.seconds == 600
    assert client.sleeps == []  # type: ignore[attr-defined]


async def test_revoked_session_during_operation() -> None:
    client, fake, _ = await _started()
    fake.known_entities.add(1001)
    fake.errors = [errors.AuthKeyUnregisteredError(request=None)]
    with pytest.raises(SessionRevoked):
        await client.send_text(1001, "x", reply_to=None, random_id=9)


async def test_list_dialogs_filters_kinds() -> None:
    client, fake, _ = await _started()

    def dialog(**values: Any) -> SimpleNamespace:
        base: dict[str, Any] = {
            "id": 1,
            "name": "Synthetic",
            "unread_count": 2,
            "is_user": False,
            "is_group": False,
            "entity": SimpleNamespace(bot=False, is_self=False),
        }
        base.update(values)
        return SimpleNamespace(**base)

    fake.dialogs = [
        dialog(id=1001, is_user=True),
        dialog(id=1002, is_user=True, entity=SimpleNamespace(bot=True, is_self=False)),
        dialog(id=1003, is_user=True, entity=SimpleNamespace(bot=False, is_self=True)),
        dialog(id=-100555, is_group=True, name="Team"),
        dialog(id=-100777),  # broadcast channel
    ]
    dialogs = await client.list_dialogs()
    assert [(d.chat_id, d.kind) for d in dialogs] == [(1001, "private"), (-100555, "group")]
    assert dialog_kind(fake.dialogs[4]) is None


async def test_fetch_own_history_stops_at_since() -> None:
    client, fake, _ = await _started()
    fake.known_entities.add(1001)
    fake.history = [
        _message(id=3, out=True, date=T0, message="newest own"),
        _message(id=2, out=True, date=T0 - timedelta(days=1), message=""),  # media only
        _message(id=1, out=True, date=T0 - timedelta(days=200), message="too old"),
    ]
    since = T0 - timedelta(days=180)
    history = [m async for m in client.fetch_own_history(1001, since)]
    assert [m.message_id for m in history] == [3]
    assert history[0].is_outgoing


def test_has_real_media() -> None:
    assert not has_real_media(_message())
    assert has_real_media(_message(media=types.MessageMediaPhoto()))


async def test_stop_disconnects() -> None:
    client, fake, _ = await _started()
    await client.stop()
    assert fake.disconnected
    with pytest.raises(RuntimeError):
        _ = client.client


def _not_started(fake: FakeTelethon, session_string: str = SESSION) -> UserClient:
    recorder = Recorder()
    return UserClient(
        USER,
        session_string,
        api_id=1,
        api_hash="hash",
        on_new_message=recorder.on_new,
        on_outgoing=recorder.on_out,
        on_read_elsewhere=recorder.on_read,
        client_factory=lambda *args, **kwargs: fake,
    )


async def test_log_out_terminates_session_and_disconnects() -> None:
    client, fake, _ = await _started()
    await client.log_out()
    assert fake.logged_out and fake.disconnected
    with pytest.raises(RuntimeError):
        _ = client.client


async def test_log_out_without_start_connects_just_to_log_out() -> None:
    fake = FakeTelethon()
    await _not_started(fake).log_out()
    assert fake.connected and fake.logged_out and fake.disconnected
    assert fake.handlers == []  # a temporary client never receives updates


async def test_log_out_of_unauthorised_session_counts_as_success() -> None:
    fake = FakeTelethon()
    fake.authorized = False
    await _not_started(fake).log_out()
    assert not fake.logged_out and fake.disconnected

    client, fake, _ = await _started()
    fake.log_out_error = errors.AuthKeyUnregisteredError(request=None)
    await client.log_out()
    assert fake.disconnected

    fake = FakeTelethon()
    await _not_started(fake, "not-a-session").log_out()
    assert not fake.connected


async def test_log_out_network_error_propagates() -> None:
    client, fake, _ = await _started()
    fake.log_out_error = ConnectionError("network down")
    with pytest.raises(ConnectionError):
        await client.log_out()
    assert fake.disconnected


async def test_invalid_session_string_is_revoked() -> None:
    recorder = Recorder()
    client = UserClient(
        USER,
        "not-a-session",
        api_id=1,
        api_hash="hash",
        on_new_message=recorder.on_new,
        on_outgoing=recorder.on_out,
        on_read_elsewhere=recorder.on_read,
        client_factory=lambda *args, **kwargs: FakeTelethon(),
    )
    with pytest.raises(SessionRevoked):
        await client.start()


# --- specs/008: peer kind of a private chat (T007) ----------------------------------------------


def _peer(**flags: Any) -> Any:
    return types.User(id=flags.pop("id", 900000101), first_name="Test Contact A", **flags)


@pytest.mark.parametrize(
    ("entity", "expected"),
    [
        (_peer(), "user"),
        (_peer(bot=True), "bot"),
        (_peer(is_self=True), "self"),
        (_peer(deleted=True), "deleted"),
        (_peer(support=True), "service"),
        (_peer(id=777000), "service"),
        (None, None),
        (types.Chat(id=555, title="Synthetic Team", photo=types.ChatPhotoEmpty(),
                    participants_count=3, date=T0, version=1), None),
    ],
)  # fmt: skip
def test_peer_kind_of(entity: Any, expected: str | None) -> None:
    assert peer_kind_of(entity) == expected


def _private_event(message: Any, peer: Any) -> SimpleNamespace:
    async def get_peer() -> Any:
        return peer

    async def get_reply_message() -> Any:
        return None

    return SimpleNamespace(
        message=message,
        chat_id=peer.id,
        sender_id=peer.id,
        is_private=True,
        get_sender=get_peer,
        get_chat=get_peer,
        get_reply_message=get_reply_message,
    )


async def test_incoming_and_outgoing_carry_peer_kind() -> None:
    _, fake, recorder = await _started()
    handler = fake.handlers[0][0]
    await handler(_private_event(_message(), _peer()))
    await handler(_private_event(_message(id=502), _peer(id=900000102, bot=True)))
    await handler(_private_event(_message(id=503, out=True), _peer(id=900000103)))
    await handler(_private_event(_message(id=504, out=True), _peer(id=42, is_self=True)))
    assert [m.peer_kind for m in recorder.incoming] == ["user", "bot"]
    first, saved = recorder.outgoing
    assert (first.chat_kind, first.peer_kind, first.chat_title) == (
        "private",
        "user",
        "Test Contact A",
    )
    assert saved.peer_kind == "self"


async def test_group_messages_have_no_peer_kind() -> None:
    _, fake, recorder = await _started()
    await fake.handlers[0][0](_event(_message(), chat_id=-100555, is_private=False))
    await fake.handlers[0][0](_event(_message(id=9, out=True), chat_id=-100555, is_private=False))
    assert recorder.incoming[0].peer_kind is None
    assert recorder.outgoing[0].chat_kind == "group" and recorder.outgoing[0].peer_kind is None


def test_dialog_kind_leaves_out_deleted_and_service_accounts() -> None:
    def dialog(entity: Any) -> SimpleNamespace:
        return SimpleNamespace(is_user=True, is_group=False, entity=entity)

    assert dialog_kind(dialog(_peer())) == "private"
    assert dialog_kind(dialog(_peer(deleted=True))) is None
    assert dialog_kind(dialog(_peer(id=777000))) is None
    assert dialog_kind(dialog(_peer(support=True))) is None
