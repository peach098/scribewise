"""``UpdateDialogUnreadMark`` -> ``UnreadMarkClearedEvent`` mapping (specs/009 T018).

Pure function, no Telegram: only ``unread=False`` on a plain dialog peer without
``saved_peer_id`` is a "the user looked" signal.
"""

from __future__ import annotations

import uuid

from telethon import types, utils

from scribe.telegram.gateway import UnreadMarkClearedEvent
from scribe.telegram.user_client import unread_mark_cleared_from_update

USER = uuid.uuid4()


def test_cleared_private_chat_maps_to_event() -> None:
    update = types.UpdateDialogUnreadMark(
        peer=types.DialogPeer(peer=types.PeerUser(5)), unread=False
    )
    assert unread_mark_cleared_from_update(USER, update) == UnreadMarkClearedEvent(
        user_id=USER, chat_id=5
    )


def test_group_and_channel_peers_use_marked_ids() -> None:
    chat = types.UpdateDialogUnreadMark(
        peer=types.DialogPeer(peer=types.PeerChat(77)), unread=False
    )
    channel = types.UpdateDialogUnreadMark(
        peer=types.DialogPeer(peer=types.PeerChannel(1234567890)), unread=False
    )
    chat_event = unread_mark_cleared_from_update(USER, chat)
    channel_event = unread_mark_cleared_from_update(USER, channel)
    assert chat_event is not None and chat_event.chat_id == utils.get_peer_id(types.PeerChat(77))
    assert chat_event.chat_id == -77
    assert channel_event is not None
    assert channel_event.chat_id == utils.get_peer_id(types.PeerChannel(1234567890))
    assert channel_event.chat_id == -1001234567890


def test_set_mark_is_ignored() -> None:
    update = types.UpdateDialogUnreadMark(
        peer=types.DialogPeer(peer=types.PeerUser(5)), unread=True
    )
    assert unread_mark_cleared_from_update(USER, update) is None


def test_missing_unread_flag_is_ignored() -> None:
    update = types.UpdateDialogUnreadMark(peer=types.DialogPeer(peer=types.PeerUser(5)))
    assert unread_mark_cleared_from_update(USER, update) is None


def test_folder_peer_is_ignored() -> None:
    update = types.UpdateDialogUnreadMark(peer=types.DialogPeerFolder(folder_id=1), unread=False)
    assert unread_mark_cleared_from_update(USER, update) is None


def test_saved_messages_sub_dialog_is_ignored() -> None:
    update = types.UpdateDialogUnreadMark(
        peer=types.DialogPeer(peer=types.PeerUser(5)),
        unread=False,
        saved_peer_id=types.PeerUser(9),
    )
    assert unread_mark_cleared_from_update(USER, update) is None


def test_other_updates_are_ignored() -> None:
    read = types.UpdateReadHistoryInbox(
        peer=types.PeerUser(5), max_id=10, still_unread_count=0, pts=1, pts_count=1
    )
    assert unread_mark_cleared_from_update(USER, read) is None
    assert unread_mark_cleared_from_update(USER, object()) is None
