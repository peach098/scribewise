"""Work account mode eligibility (specs/008 T010, FR-003): only a private chat with a real person
can become a work chat by the mode."""

from __future__ import annotations

import pytest

from scribe.chats.work_account import is_eligible


def test_private_chat_with_a_person_is_eligible() -> None:
    assert is_eligible("private", "user") is True


@pytest.mark.parametrize("peer_kind", ["bot", "self", "service", "deleted", None])
def test_other_private_peers_are_not(peer_kind: str | None) -> None:
    assert is_eligible("private", peer_kind) is False


@pytest.mark.parametrize("chat_kind", ["group", "channel_comments", None])
@pytest.mark.parametrize("peer_kind", ["user", None])
def test_groups_and_channels_never_are(chat_kind: str | None, peer_kind: str | None) -> None:
    assert is_eligible(chat_kind, peer_kind) is False
