"""Keyword-based sensitive-topic detection merged with the model's flags (T027, FR-009)."""

from __future__ import annotations

import pytest

from scribe.drafting.flags import detect_sensitive, merge_sensitive
from scribe.llm.provider import Sensitive, SensitiveCategory


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("Can you send me the password for the staging server?", "credentials"),
        ("Скинь, пожалуйста, логин и пароль от админки", "credentials"),
        ("What's the API key for the test account?", "credentials"),
        ("Please pay the invoice today", "payment"),
        ("Нужно срочно оплатить счёт поставщику", "payment"),
        ("Send me the IBAN and I'll wire the money", "payment"),
        ("I need a scan of your passport", "personal_data"),
        ("Пришли номер паспорта и СНИЛС для пропуска", "personal_data"),
        ("Legal wants to review the contract before we sign", "legal"),
        ("Юрист прислал претензию по договору", "legal"),
        ("Let's talk about your salary review", "hr"),
        ("Он написал заявление на увольнение", "hr"),
    ],
)
def test_categories_detected(text: str, category: SensitiveCategory) -> None:
    assert category in detect_sensitive(text)


@pytest.mark.parametrize(
    "text",
    [
        "When will the Q3 report be ready?",
        "Привет! Как дела с презентацией?",
        "Let's sync tomorrow at 11",
        "Passing the ball to the design team",  # "pass" is not "password"
        "The paycheck component renders fine",  # UI component, no money request
    ],
)
def test_ordinary_messages_not_flagged(text: str) -> None:
    assert detect_sensitive(text) == []


def test_several_texts_and_categories_are_unique_and_ordered() -> None:
    found = detect_sensitive("Send the password", "and pay the invoice", "password again")
    assert found == ["credentials", "payment"]


def test_merge_is_a_union_and_never_clears_model_flags() -> None:
    model = Sensitive(value=True, categories=["legal"])
    merged = merge_sensitive(model, ["payment", "legal"])
    assert merged.value is True
    assert merged.categories == ["payment", "legal"]

    nothing = merge_sensitive(Sensitive(value=False, categories=[]), [])
    assert nothing == Sensitive(value=False, categories=[])

    keyword_only = merge_sensitive(Sensitive(value=False, categories=[]), ["hr"])
    assert keyword_only == Sensitive(value=True, categories=["hr"])

    # A model that says "sensitive" without a category keeps value=True.
    vague = merge_sensitive(Sensitive(value=True, categories=[]), [])
    assert vague.value is True
