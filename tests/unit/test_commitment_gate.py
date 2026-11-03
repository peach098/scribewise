"""Issue #72: the ``extract.commitments`` pre-filter and the unchanged-draft reuse. Recall first:
every "must call" case below goes to the LLM; only messages with no cue at all are skipped.
Synthetic texts only."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from scribe.context.comment_commitments import CommentCommitmentInput, comment_gate
from scribe.context.commitment_gate import LONG_WORDS, needs_extraction
from scribe.context.commitments import CommitmentExtractor
from scribe.db.models import Draft
from scribe.sending.send import _unchanged_draft_commitment

SKIP_NO_CONTEXT = [
    "ок",
    "Ок",
    "окей",
    "ok",
    "+",
    "спасибо",
    "Спасибо!",
    "спс",
    "Понял, спасибо",
    "готово",
    "👍",
    "thanks",
    "Thanks, got it",
    "thx!",
    "done",
    "Привет",
    "Отлично, супер",
]

MUST_CALL = [
    # short acceptances that carry a deadline
    ("Ок, до пятницы", "promise"),
    ("ок, к понедельнику", "promise"),
    ("Да, завтра", "promise"),
    ("sure, by Friday", "promise"),
    ("ok, will do by Monday", "promise"),
    ("Хорошо, в среду", "promise"),
    # bare promises
    ("сделаю", "promise"),
    ("гляну", "promise"),
    ("will do", "promise"),
    ("on it", "promise"),
    ("I'll check", "promise"),
    # loose actions the strict detector does not know
    ("Пришлём вечером", "action"),
    ("постараюсь", "action"),
    ("будет к обеду", "action"),
    ("не сделаю", "action"),  # negation is the LLM's call, not the gate's
    ("we'll send it", "action"),
    ("let me see", "promise"),
    ("договорились", "action"),
    # date and time words alone
    ("до 15:00", "date"),
    ("к 12.11", "date"),
    ("tomorrow", "date"),
    ("by EOD", "date"),
    ("в четверг", "date"),
    ("next week", "date"),
    ("Monday then", "date"),
    ("на следующей неделе", "date"),
    ("3pm", "date"),
]


@pytest.mark.parametrize("text", SKIP_NO_CONTEXT)
def test_trivial_messages_are_skipped(text: str) -> None:
    decision = needs_extraction(text)
    assert not decision.extract and decision.reason == "no_cue"


@pytest.mark.parametrize(("text", "reason"), MUST_CALL)
def test_messages_with_a_cue_go_to_the_llm(text: str, reason: str) -> None:
    decision = needs_extraction(text)
    assert decision.extract and decision.reason == reason


def test_a_long_message_always_goes_to_the_llm() -> None:
    neutral = " ".join(["слово"] * (LONG_WORDS + 1))
    assert needs_extraction(neutral).reason == "long"
    assert not needs_extraction(" ".join(["слово"] * LONG_WORDS)).extract


@pytest.mark.parametrize(
    ("text", "context"),
    [
        ("ок", ["Пришлёшь отчёт до пятницы?"]),
        ("Ок", ["Сможешь глянуть PR?"]),
        ("+", ["Сделай выгрузку, пожалуйста"]),
        ("лады", ["Тогда созвон в четверг"]),
        ("sure", ["Can you review the deck?"]),
        ("👍", ["Please send the logs by Monday"]),
        ("ok", ["", "Could you take this one"]),
    ],
)
def test_an_acceptance_of_a_request_goes_to_the_llm(text: str, context: list[str]) -> None:
    decision = needs_extraction(text, context)
    assert decision.extract and decision.reason == "acceptance"


@pytest.mark.parametrize(
    ("text", "context"),
    [
        ("ок", ["Отчёт загрузил в папку"]),
        ("+", ["Всем спасибо за встречу"]),
        ("ok", ["FYI, the build is green"]),
        ("спасибо", ["Пришлёшь отчёт до пятницы?"]),  # thanks never accepts anything
        ("thanks", ["Can you review the deck?"]),
    ],
)
def test_an_acceptance_without_a_request_is_skipped(text: str, context: list[str]) -> None:
    assert needs_extraction(text, context).reason == "no_cue"


# --- the unchanged approved draft ----------------------------------------------------------------


def _draft(text: str, output_json: dict[str, Any] | None) -> Draft:
    return Draft(text=text, output_json=output_json)


def test_unchanged_draft_reuses_its_flag() -> None:
    assert _unchanged_draft_commitment(_draft("Ок", {"creates_commitment": False}), "Ок") is False
    draft = _draft("Пришлю в пятницу", {"creates_commitment": True})
    assert _unchanged_draft_commitment(draft, "Пришлю в пятницу") is True


def test_edited_draft_or_missing_flag_falls_back_to_the_gate() -> None:
    edited = _draft("Ок", {"creates_commitment": False})
    assert _unchanged_draft_commitment(edited, "Ок, пришлю в пятницу") is None
    assert _unchanged_draft_commitment(_draft("Ок", {}), "Ок") is None
    assert _unchanged_draft_commitment(_draft("Ок", None), "Ок") is None
    assert _unchanged_draft_commitment(_draft("Ок", {"creates_commitment": "yes"}), "Ок") is None


class _NoSessions:
    def __call__(self) -> Any:
        raise AssertionError("a draft flagged as no commitment needs no database work")


async def test_a_false_draft_flag_skips_before_any_work() -> None:
    extractor = CommitmentExtractor(_NoSessions(), llm_router=None, run_inline=True)  # type: ignore[arg-type]
    await extractor.submit(
        uuid.uuid4(),
        work_chat_id=uuid.uuid4(),
        text="Ок, до пятницы",
        sent_at=datetime(2026, 10, 14, tzinfo=UTC),
        draft_commitment=False,
    )
    assert extractor.pending == 0


# --- the user's Jira comments --------------------------------------------------------------------


def _comment(text: str, request: str | None = None) -> CommentCommitmentInput:
    at = datetime(2026, 10, 14, 12, tzinfo=UTC)
    return CommentCommitmentInput(
        user_id=uuid.uuid4(),
        issue_key="OPS-1",
        issue_title="Synthetic issue",
        comment_id="10002",
        comment_url="",
        comment_text=text,
        restricted=False,
        posted_at=at,
        updated_at=at,
        counterparty="Anna Testova",
        request_text=request,
        request_author="Anna Testova" if request else None,
        request_at=at if request else None,
    )


def test_jira_comment_accepting_the_request_goes_to_the_llm() -> None:
    assert comment_gate(_comment("Sure.", "Can you send the logs?")).reason == "acceptance"
    assert comment_gate(_comment("Ок", "Сделаешь выгрузку до пятницы?")).reason == "acceptance"
    assert comment_gate(_comment("Ок, до пятницы")).reason == "promise"


def test_jira_comment_without_cue_is_skipped() -> None:
    assert comment_gate(_comment("Thanks, noted.")).reason == "no_cue"
    assert comment_gate(_comment("Спасибо!", "Сделаешь выгрузку до пятницы?")).reason == "no_cue"
