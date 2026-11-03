"""Keyword promise detection merged with the model's ``creates_commitment`` (issue #24)."""

from __future__ import annotations

from typing import Any

import pytest

from scribe.drafting.composer import ComposeInput, compose_output
from scribe.drafting.promises import detect_promise, merge_commitment
from scribe.drafting.stored_output import load_stored_output
from scribe.llm.adapters.fake import FakeAdapter
from scribe.llm.provider import (
    ContactInfo,
    CreatesCommitment,
    DraftOutput,
    MessageItem,
    ProfileContext,
)
from scribe.llm.registry import LLMRouter

NO_FLAG = CreatesCommitment(value=False, what=None, due=None)


@pytest.mark.parametrize(
    ("text", "what"),
    [
        # The canned replies of the four eval threads models missed (evals/threads).
        (
            "Hi Tom,\nYes, I'll review your pull request by Friday.\nBest, Alex",
            "I'll review your pull request by Friday",
        ),
        ("Привет! Возьму ревью нового API", "Возьму ревью нового API"),
        ("Привет! Ок, прочитаю и подпишу NDA до среды", "прочитаю и подпишу NDA до среды"),
        ("Привет! Ещё не успел, гляну сегодня и отпишусь", "гляну сегодня и отпишусь"),
        # Casual habits from the style profile.
        ("Привет, ок, гляну сегодня", "гляну сегодня"),
        ("Привет! Гляну и отпишусь", "Гляну и отпишусь"),
        ("Посмотрю вечером", "Посмотрю вечером"),
        ("Сделаю к вечеру", "Сделаю к вечеру"),
        ("Отправлю завтра утром", "Отправлю завтра утром"),
        ("Проверю и скину", "Проверю и скину"),
        ("Отпишусь, как закончу", "Отпишусь, как закончу"),
        # English future forms.
        ("sure, will send them tomorrow", "will send them tomorrow"),
        ("will check the board and get back to you", "will check the board and get back to you"),
        ("I will check it EOD", "I will check it EOD"),
        ("let me get back to you on that", "let me get back to you on that"),
        ("will do", "will do"),
        ("I'm on it", "I'm on it"),
        ("I’ll send it today", "I’ll send it today"),
        # A bare acceptance of a deadline is a promise too.
        ("Привет! Ок, до пятницы", "Ок, до пятницы"),
        ("sure, by Friday", "sure, by Friday"),
    ],
)
def test_promise_detected(text: str, what: str) -> None:
    assert detect_promise(text) == what


@pytest.mark.parametrize(
    "text",
    [
        "Спасибо, получил",
        "thanks, noted",
        "Thanks, got it today",  # a deadline word alone is not a promise
        "Yes, it shipped today",
        "Привет! Да не за что",
        "yep, deploy finished at 14:20",
        "Привет! Отчёт за Q3 будет в пятницу",  # status, not a new action
        # Negated.
        "Не смогу прислать до пятницы",
        "Я пока не гляну, извини",
        "I won't make it by Friday",
        "I can't send it today",
        "I'll never agree to that",
        "I will not sign it",
        # Quoted: someone else's words.
        "Он написал «пришлю завтра»",
        'Tom said "I\'ll send it tomorrow"',
        "> пришлю завтра\nок",
        # Someone else's action.
        "Boris will send it tomorrow",
        "I commented on it yesterday",
        # A restated promise is not new.
        "Привет! Да, пришлю в четверг, как договаривались",
        "ok, will send it thursday as planned",
        "Привет! Отчёт будет в пятницу, как и обещал",
        "",
    ],
)
def test_no_promise(text: str) -> None:
    assert detect_promise(text) is None


def test_the_sentence_with_a_deadline_wins() -> None:
    text = "on it, looking now. will post an update here asap"
    assert detect_promise(text) == "will post an update here asap"


def test_a_long_fragment_is_trimmed() -> None:
    found = detect_promise("Пришлю " + "очень " * 60 + "длинный отчёт")
    assert found is not None and len(found) == 160 and found.endswith("…")


def test_merge_never_removes_a_model_flag() -> None:
    model = CreatesCommitment(value=True, what="send the report", due="2026-10-16")
    assert merge_commitment(model, None) == model
    assert merge_commitment(model, "пришлю завтра") == model  # the model's wording is kept


def test_merge_fills_a_missing_what_only() -> None:
    model = CreatesCommitment(value=True, what=None, due="2026-10-16")
    merged = merge_commitment(model, "пришлю в пятницу")
    assert merged == CreatesCommitment(value=True, what="пришлю в пятницу", due="2026-10-16")


def test_merge_adds_a_keyword_flag_without_a_date() -> None:
    assert merge_commitment(NO_FLAG, None) == NO_FLAG
    merged = merge_commitment(NO_FLAG, "гляну сегодня и отпишусь")
    assert merged == CreatesCommitment(value=True, what="гляну сегодня и отпишусь", due=None)


def _draft(text: str, commitment: dict[str, Any]) -> DraftOutput:
    return DraftOutput.model_validate(
        {
            "text": text,
            "language": "ru",
            "needs_info": False,
            "placeholders": [],
            "sources": [{"kind": "message", "id": "msg:in1", "used_for": "the question"}],
            "flags": {
                "creates_commitment": commitment,
                "sensitive": {"value": False, "categories": []},
            },
            "conflicts": [],
            "tone_shift_applied": "none",
            "context_warnings": [],
        }
    )


def _input() -> ComposeInput:
    return ComposeInput(
        incoming=[
            MessageItem(id="msg:in1", sender="Synthetic Colleague", text="Ты видел мой коммент?")
        ],
        history=[],
        contact=ContactInfo(display_name="Synthetic Colleague", kind="person"),
        profile=ProfileContext(rules_json={"typical_phrases": ["гляну", "отпишусь"]}),
    )


async def test_composer_raises_the_flag_the_model_missed(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    missed = {"value": False, "what": None, "due": None}
    fake_llm.set_response("compose.draft", _draft("Привет! Ещё не успел, гляну сегодня", missed))

    result = await compose_output(llm_router, _input())

    assert result.output.flags.creates_commitment == CreatesCommitment(
        value=True, what="гляну сегодня", due=None
    )
    # Stored in the clear for the draft's metadata, and inside the encrypted output for the card.
    stored = result.output_json()
    assert stored["creates_commitment"] is True
    loaded = load_stored_output(stored, result.output.text).output
    assert loaded is not None and loaded.flags.creates_commitment.what == "гляну сегодня"


async def test_composer_keeps_the_model_flag(llm_router: LLMRouter, fake_llm: FakeAdapter) -> None:
    flagged = {"value": True, "what": "посмотреть комментарий Анны", "due": "2026-10-14"}
    fake_llm.set_response("compose.draft", _draft("Привет! Гляну сегодня", flagged))

    result = await compose_output(llm_router, _input())

    assert result.output.flags.creates_commitment.model_dump() == flagged


async def test_composer_leaves_a_plain_acknowledgement(
    llm_router: LLMRouter, fake_llm: FakeAdapter
) -> None:
    none = {"value": False, "what": None, "due": None}
    fake_llm.set_response("compose.draft", _draft("Спасибо, получил", none))

    result = await compose_output(llm_router, _input())

    assert result.output.flags.creates_commitment.value is False
