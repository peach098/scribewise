"""Style profile sample selection and the lighter retry after a truncated answer (issue #30).

Fake LLM only, no database; every message text is synthetic.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from scribe.config import Settings
from scribe.llm.adapters.fake import FakeAdapter, FakeCall
from scribe.llm.provider import (
    ProviderOutputInvalid,
    ProviderUnavailable,
    StyleProfileOutput,
    StyleProfileRequest,
    StyleSample,
    Usage,
)
from scribe.llm.registry import LLMRouter, RoutedResult
from scribe.style.profiles import (
    BuildSummary,
    _BuildJob,
    _retry_if_truncated,
    _select,
    is_short_sample,
    select_contact_samples,
    select_global_samples,
)
from scribe.telegram.control_bot.profile_command import rebuild_result_text
from tests.conftest import LogCapture

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
GROUP_A, GROUP_B = -1001, -1002


def _samples(
    n: int,
    *,
    chat_id: int,
    group: bool,
    text: str = "synthetic message number {i} here",
    start: int = 0,
) -> list[StyleSample]:
    """``n`` messages in one chat, one minute apart (``start`` shifts them in time)."""
    return [
        StyleSample(
            text=text.format(i=i),
            sent_at=T0 + timedelta(minutes=start + i),
            contact_display_name="Synthetic chat",
            chat_id=chat_id,
            group=group,
        )
        for i in range(n)
    ]


def _private(n: int, chat_id: int = 1, **kwargs: Any) -> list[StyleSample]:
    return _samples(n, chat_id=chat_id, group=False, **kwargs)


def _group(n: int, chat_id: int = GROUP_A, **kwargs: Any) -> list[StyleSample]:
    return _samples(n, chat_id=chat_id, group=True, **kwargs)


def _select_global(samples: list[StyleSample], limit: int = 400) -> list[StyleSample]:
    return select_global_samples(samples, limit=limit, group_per_chat=40, group_share=0.3)


def _count(selected: list[StyleSample], *, group: bool) -> int:
    return sum(s.group == group for s in selected)


# --- selection ----------------------------------------------------------------------------------


def test_one_dominant_group_chat_is_capped_per_chat() -> None:
    """A busy group with far more (and newer) messages than the private chats gives at most
    its 40 newest while private chats can fill the rest."""
    samples = _group(1000, start=10_000) + _private(250, chat_id=1) + _private(250, chat_id=2)
    selected = _select_global(samples)
    assert len(selected) == 400
    assert _count(selected, group=True) == 40  # its per-chat cap, not 400 of its 1000
    group_part = [s for s in selected if s.group]
    newest_group = sorted((s for s in samples if s.group), key=lambda s: s.sent_at)[-40:]
    assert set(map(id, group_part)) == set(map(id, newest_group))
    assert selected == sorted(selected, key=lambda s: s.sent_at, reverse=True)

    # With fewer private messages than their part (360), the group fills the space left.
    fewer = _group(1000, start=10_000) + _private(300)
    selected = _select_global(fewer)
    assert len(selected) == 400 and _count(selected, group=False) == 300


def test_groups_take_at_most_their_share_when_private_chats_can_fill_the_rest() -> None:
    groups = [_group(100, chat_id=-2000 - k, start=10_000) for k in range(5)]
    samples = [s for chat in groups for s in chat] + _private(500)
    selected = _select_global(samples)
    assert len(selected) == 400
    assert _count(selected, group=True) == 120  # 30% of 400
    assert _count(selected, group=False) == 280
    per_chat: dict[int | None, int] = {}
    for sample in selected:
        if sample.group:
            per_chat[sample.chat_id] = per_chat.get(sample.chat_id, 0) + 1
    assert max(per_chat.values()) <= 40


def test_group_only_user_still_gets_a_full_sample() -> None:
    samples = _group(500, chat_id=GROUP_A) + _group(30, chat_id=GROUP_B)
    selected = _select_global(samples)
    assert len(selected) == 400
    assert {s.chat_id for s in selected} == {GROUP_A, GROUP_B}
    assert _count(selected, group=True) == 400


def test_private_only_user_gets_the_newest_messages() -> None:
    samples = _private(300, chat_id=1) + _private(300, chat_id=2, start=1000)
    selected = _select_global(samples)
    newest = sorted(samples, key=lambda s: s.sent_at, reverse=True)[:400]
    assert selected == newest


def test_private_chats_short_of_their_part_are_filled_by_groups() -> None:
    samples = _private(50) + _group(300, chat_id=GROUP_A) + _group(300, chat_id=GROUP_B)
    selected = _select_global(samples)
    assert len(selected) == 400
    assert _count(selected, group=False) == 50


def test_short_messages_are_kept_to_a_small_share() -> None:
    shorts = _private(300, text="+") + _private(100, chat_id=2, text="ok {i}", start=500)
    longer = _private(300, chat_id=3, start=-1000)  # older than every short one
    selected = _select_global(shorts + longer)
    short = [s for s in selected if is_short_sample(s.text)]
    assert len(short) == 60  # 15% of 400
    assert len(selected) == 360  # the other short messages are left out, not replaced by more


def test_short_messages_still_count_for_a_user_with_nothing_else() -> None:
    selected = _select_global(_private(10, text="👍"))
    assert len(selected) == 10


@pytest.mark.parametrize(
    ("text", "short"),
    [
        ("+", True),
        ("ок", True),
        ("👍👍", True),
        ("ok thanks", True),
        ("... !!! ???", True),
        ("Sounds good, will do", False),
        ("Пришлю отчёт завтра", False),
    ],
)
def test_is_short_sample(text: str, short: bool) -> None:
    assert is_short_sample(text) is short


def test_a_groups_own_profile_learns_from_all_its_messages() -> None:
    """The group cap applies to the global sample only; a contact gets its newest messages."""
    samples = _group(300, text="+ {i}")
    selected = select_contact_samples(samples, limit=400)
    assert len(selected) == 300
    assert (
        select_contact_samples(samples, limit=100)
        == sorted(samples, key=lambda s: s.sent_at, reverse=True)[:100]
    )


def test_select_uses_settings_and_scope(settings: Settings) -> None:
    samples = _group(200) + _private(10)
    tuned = settings.model_copy(
        update={"profile_group_samples_per_chat": 5, "profile_group_samples_share": 0.5}
    )
    global_job = _BuildJob(None, samples, {})
    contact_job = _BuildJob(uuid.uuid4(), samples, {})
    assert len(_select(global_job, tuned, 100)) == 100  # 10 private + groups fill the rest
    assert _count(_select(global_job, tuned, 20), group=True) == 10  # 5 capped + 5 to fill
    assert len(_select(contact_job, tuned, 100)) == 100


def test_chat_kind_never_reaches_the_prompt() -> None:
    request = StyleProfileRequest(samples=_group(2), scope="global")
    (samples_block,) = [b for b in request.to_blocks() if b.name == "samples"]
    sent = json.loads(samples_block.content)
    assert set(sent[0]) == {"text", "sent_at", "contact_display_name"}


# --- retry after a truncated answer -------------------------------------------------------------


def _output() -> StyleProfileOutput:
    return StyleProfileOutput.model_validate(
        {
            "rules_json": {
                "languages": ["en"], "formality": 2, "typical_length_words": 10,
                "greetings": [], "sign_offs": [], "emoji_usage": "none",
                "punctuation_habits": [], "typical_phrases": [], "never_does": [],
            },
            "summary_text": "Synthetic summary.",
            "chosen_example_indices": [0],
        }
    )  # fmt: skip


def _truncated() -> ProviderOutputInvalid:
    return ProviderOutputInvalid(
        "truncated", truncated=True, usage=Usage(output_tokens=2000), finish_reason="length"
    )


def _sample_count(call: FakeCall) -> int:
    (block,) = [b for b in call.user_blocks if b.name == "samples"]
    return len(json.loads(block.content))


async def test_truncated_answer_is_retried_lighter_and_succeeds(
    llm_router: LLMRouter, fake_llm: FakeAdapter, settings: Settings, log_capture: LogCapture
) -> None:
    def respond(call: FakeCall) -> StyleProfileOutput:
        if call.effort == "medium":  # the route's effort: the first attempt is cut off
            raise _truncated()
        return _output()

    fake_llm.set_response("profile.build", respond)
    job = _BuildJob(None, _private(300) + _group(100), {})
    used = _select(job, settings, settings.profile_max_samples)
    outcome, final = await _retry_if_truncated(
        llm_router, uuid.uuid4(), job, used, _truncated(), settings=settings
    )
    assert isinstance(outcome, RoutedResult)
    assert len(final) == len(used) // 2 == 200
    assert sum(s.group for s in final) == 40  # selected the same way: the group cap holds
    (call,) = fake_llm.calls_for("profile.build")
    assert (call.effort, call.batch, _sample_count(call)) == ("low", False, 200)
    (retry,) = log_capture.events("profile_build_retry")
    assert (retry["samples"], retry["retry_samples"], retry["scope"]) == (400, 200, "global")
    assert "synthetic message" not in log_capture.raw


async def test_failed_retry_returns_the_error(
    llm_router: LLMRouter, fake_llm: FakeAdapter, settings: Settings
) -> None:
    fake_llm.set_response("profile.build", _truncated())
    job = _BuildJob(uuid.uuid4(), _private(40), {})
    used = _select(job, settings, settings.profile_max_samples)
    outcome, final = await _retry_if_truncated(
        llm_router, uuid.uuid4(), job, used, _truncated(), settings=settings
    )
    assert isinstance(outcome, ProviderOutputInvalid) and outcome.truncated
    assert len(final) == 20
    assert len(fake_llm.calls_for("profile.build")) == 1  # retried once only


@pytest.mark.parametrize(
    "outcome",
    [ProviderOutputInvalid("failed validation"), ProviderUnavailable("synthetic outage")],
)
async def test_other_failures_are_not_retried(
    llm_router: LLMRouter, fake_llm: FakeAdapter, settings: Settings, outcome: Exception
) -> None:
    job = _BuildJob(None, _private(40), {})
    used = _select(job, settings, settings.profile_max_samples)
    assert isinstance(outcome, ProviderOutputInvalid | ProviderUnavailable)
    result, final = await _retry_if_truncated(
        llm_router, uuid.uuid4(), job, used, outcome, settings=settings
    )
    assert result is outcome and final is used
    assert fake_llm.calls == []


# --- the Rebuild note ---------------------------------------------------------------------------


def _profile(sample_size: int = 120, weak: bool = False) -> Any:
    return SimpleNamespace(sample_size=sample_size, rules_json={"weak": weak})


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_rebuild_note_when_only_the_global_profile_failed(settings: Settings, lang: Any) -> None:
    text = rebuild_result_text(
        BuildSummary(None, contact_profiles=3, retired=0), settings=settings, lang=lang
    )
    assert "⚠️" in text and "3" in text
    plain_failure = rebuild_result_text(
        BuildSummary(None, contact_profiles=0, retired=0), settings=settings, lang=lang
    )
    assert plain_failure != text and "⚠️" in plain_failure


def test_rebuild_note_counts_failed_contact_profiles(settings: Settings) -> None:
    summary = BuildSummary(_profile(), contact_profiles=2, retired=0, contact_failures=1)
    text = rebuild_result_text(summary, settings=settings, lang="en")
    assert text.startswith("✅ Rebuilt: global profile from 120 of your messages, 2 contact")
    assert "1 contact profile couldn't be rebuilt" in text
    ok = rebuild_result_text(
        BuildSummary(_profile(), contact_profiles=2, retired=0), settings=settings, lang="en"
    )
    assert "couldn't" not in ok
