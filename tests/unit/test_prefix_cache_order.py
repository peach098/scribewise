"""Issue #70: consecutive turns in one chat send prompts with a long byte-identical prefix, so
the provider's prompt cache (DeepSeek automatic, Anthropic breakpoints) can reuse it.

The filter request puts the candidates first (tasks, commitments, then the anchored history)
and the incoming messages last; the draft request puts the profile and the per-chat fields
first and the incoming messages last; the history window's head only moves in steps. The
prompts are compared as the router sends them: with short per-request ids (issue #71)."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta

from scribe.context.history import PRIOR_WITH_CONTACT, RECENT_IN_CHAT, WINDOW_STEP, anchored_window
from scribe.llm.aliases import prompt_request
from scribe.llm.provider import (
    CommitmentRef,
    ContactInfo,
    DraftRequest,
    FilterCandidates,
    FilterContextRequest,
    HistoryItem,
    LLMRequest,
    MessageItem,
    ProfileContext,
    TaskItem,
)

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
WORDS = [
    "отчёт",
    "счёт",
    "макет",
    "релиз",
    "ревью",
    "договор",
    "оплата",
    "клиент",
    "сроки",
    "дизайн",
    "созвон",
    "пятница",
]


def _text(i: int) -> str:
    return " ".join(WORDS[(i * 7 + k) % len(WORDS)] for k in range(4 + i % 9)).capitalize()


def _message_id(i: int) -> str:
    return f"msg:{i:08d}-1111-4222-8333-{i:012d}"


def _stream(n: int) -> list[HistoryItem]:
    """A chat with ``n`` messages, oldest first, alternating the user and the contact."""
    return [
        HistoryItem(
            id=_message_id(i),
            is_user=i % 2 == 1,
            sender="me" if i % 2 == 1 else "Anna Testova",
            text=_text(i),
            sent_at=T0 + timedelta(minutes=3 * i),
        )
        for i in range(n)
    ]


TASKS = [
    TaskItem(
        id=f"task:jira:SYN-{100 + i}",
        title=_text(i + 3),
        status="In progress" if i % 2 else "To do",
        due=date(2026, 10, 15) + timedelta(days=i) if i % 3 else None,
        assignees=["Anna Testova"],
    )
    for i in range(30)
]
COMMITMENTS = [
    CommitmentRef(id=f"commit:{i:08d}-aaaa-4bbb-8ccc-{i:012d}", what=_text(i + 5), due=None)
    for i in range(4)
]
PROFILE = ProfileContext(
    rules_json={"formality": 2, "languages": ["ru"], "greetings": ["Привет"]},
    examples=[_text(i + 11) for i in range(20)],
)


def _filter_turn(n: int) -> FilterContextRequest:
    """The filter request of the turn whose incoming message is the chat's ``n``-th."""
    chat = _stream(n)
    *before, last = chat
    window = anchored_window(chat[::-1], n, minimum=RECENT_IN_CHAT, step=WINDOW_STEP)[::-1]
    history = [h for h in window if h.id != last.id]
    incoming = [MessageItem(id=last.id, sender=last.sender, text=last.text, sent_at=last.sent_at)]
    return FilterContextRequest(
        candidates=FilterCandidates(tasks=TASKS, commitments=COMMITMENTS, history=history),
        incoming_messages=incoming,
    )


def _draft_turn(n: int, selected: list[int]) -> DraftRequest:
    chat = _stream(n)
    last = chat[-1]
    return DraftRequest(
        profile=PROFILE,
        contact=ContactInfo(display_name="Anna Testova", kind="person"),
        timezone="Europe/Moscow",
        commitments=COMMITMENTS[:1],
        tasks=TASKS[:2],
        history=[chat[i] for i in selected],
        today=date(2026, 10, 14),
        incoming_messages=[MessageItem(id=last.id, sender=last.sender, text=last.text)],
    )


def _sent(req: LLMRequest) -> str:
    return "\n\n".join(b.render() for b in prompt_request(req).to_blocks())


def _shared(a: str, b: str) -> int:
    return len(os.path.commonprefix([a, b]))


def test_anchored_window_sizes_and_steps() -> None:
    stream = list(range(100))
    for total in range(0, 100):
        window = anchored_window(stream[:total][::-1], total, minimum=40, step=20)
        assert len(window) == total if total < 40 else 40 <= len(window) <= 59
        assert window == stream[:total][::-1][: len(window)]  # always the newest ones
    heads = [anchored_window(stream[:t][::-1], t, minimum=40, step=20)[-1] for t in range(40, 100)]
    assert heads == [0] * 20 + [20] * 20 + [40] * 20  # the head moves only in steps
    assert PRIOR_WITH_CONTACT < RECENT_IN_CHAT


def test_consecutive_filter_turns_share_a_long_prefix() -> None:
    """Turn 2 comes two messages later (the user's reply, then the contact's next message)."""
    for n in (45, 52, 70):
        first, second = _sent(_filter_turn(n)), _sent(_filter_turn(n + 2))
        assert _shared(first, second) / len(first) >= 0.9, n
    # Crossing a step moves the head once: the tasks and commitments are still shared.
    first, second = _sent(_filter_turn(59)), _sent(_filter_turn(61))
    assert '"history": [{"id": "h1", ' in first[: _shared(first, second)]


def test_filter_prompt_puts_incoming_last_and_history_after_tasks() -> None:
    blocks = prompt_request(_filter_turn(50)).to_blocks()
    assert [b.name for b in blocks] == ["candidates", "incoming_messages"]
    content = blocks[0].content
    assert content.index('"tasks"') < content.index('"commitments"') < content.index('"history"')


def test_consecutive_draft_turns_share_a_long_prefix() -> None:
    """The selected history differs per turn, but everything before it is the same."""
    first = _sent(_draft_turn(50, [44, 46, 47, 48]))
    second = _sent(_draft_turn(52, [46, 48, 49, 50]))
    shared = _shared(first, second)
    assert shared / len(first) >= 0.55
    profile = prompt_request(_draft_turn(50, [44])).to_blocks()[0].render()
    assert shared > len(profile) + 300  # before #70 only the profile was shared
    assert '<data name="history" trust="untrusted">\n[{"id": "h1",' in first[:shared]


def test_draft_blocks_order() -> None:
    names = [b.name for b in _draft_turn(50, [44]).to_blocks()]
    assert names == [
        "profile",
        "user",
        "contact",
        "chat_overrides",
        "user_state",
        "timezone",
        "commitments",
        "tasks",
        "history",
        "today",
        "calendar",
        "incoming_messages",
    ]
