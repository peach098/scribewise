"""Sensitive-topic detection (FR-009): keyword rules merged with the model's own flags.

The keyword pass is a safety net that never removes a model flag: a draft is flagged when either
the model or a keyword says so. English and Russian stems; word-start anchored so that e.g.
"passing" does not match "password".
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import get_args

from scribe.llm.provider import Sensitive, SensitiveCategory

CATEGORY_ORDER: tuple[SensitiveCategory, ...] = get_args(SensitiveCategory)

_KEYWORDS: dict[SensitiveCategory, tuple[str, ...]] = {
    "credentials": (
        r"password",
        r"passcode",
        r"passphrase",
        r"credentials?",
        r"api[ -]?keys?",
        r"access[ -]?tokens?",
        r"secret[ -]?keys?",
        r"2fa",
        r"one[- ]time code",
        r"ssh[ -]?keys?",
        r"пароль",
        r"пароля",
        r"паролем",
        r"логин\w* и пароль",
        r"токен\w*",
        r"код\w* подтверждения",
        r"ключ\w* доступа",
        r"учётн\w* данн\w*",
        r"учетн\w* данн\w*",
    ),
    "payment": (
        r"invoices?",
        r"payments?",
        r"pay (?:the|for|me|us|it)",
        r"wire (?:the )?(?:money|funds|transfer)",
        r"bank (?:account|details|transfer)",
        r"iban",
        r"card number",
        r"refund",
        r"оплат\w*",
        r"плат[её]ж\w*",
        r"сч[её]т\w* (?:на оплату|поставщику|к оплате)",
        r"оплатить сч[её]т",
        r"перев[ео]д\w* (?:денег|средств|на карту)",
        r"реквизит\w*",
        r"номер\w* карты",
        r"возврат\w* (?:денег|средств)",
    ),
    "personal_data": (
        r"passports?",
        r"social security",
        r"ssn",
        r"date of birth",
        r"home address",
        r"medical (?:record|certificate)",
        r"personal data",
        r"паспорт\w*",
        r"снилс",
        r"инн",
        r"дат\w* рождения",
        r"домашн\w* адрес\w*",
        r"персональн\w* данн\w*",
        r"медицинск\w* (?:справк|карт)\w*",
    ),
    "legal": (
        r"legal",
        r"lawyers?",
        r"lawsuits?",
        r"contracts?",
        r"nda",
        r"court",
        r"litigation",
        r"liabilit\w+",
        r"юрист\w*",
        r"юридическ\w*",
        r"договор\w*",
        r"претензи\w*",
        r"иск(?:а|ом|у|и|ов)?",  # not "искать"
        r"суд(?:а|е|у|ом|ы|ебн\w*)?",  # not "судя"
    ),
    "hr": (
        r"salary",
        r"salaries",
        r"compensation",
        r"pay raise",
        r"layoffs?",
        r"fired",
        r"dismissal",
        r"resignation",
        r"performance review",
        r"disciplinary",
        r"sick leave",
        r"harassment",
        r"зарплат\w*",
        r"оклад\w*",
        r"премию|премии|премия",
        r"увольнен\w*",
        r"уволить|уволят|уволили",
        r"заявлени\w* об уходе",
        r"больничн\w*",
        r"выговор\w*",
        r"сокращени\w* штата",
    ),
}

_PATTERNS: dict[SensitiveCategory, re.Pattern[str]] = {
    category: re.compile(r"(?<![\w])(?:" + "|".join(words) + r")(?![\w])", re.IGNORECASE)
    for category, words in _KEYWORDS.items()
}


def detect_sensitive(*texts: str) -> list[SensitiveCategory]:
    """Categories found in any of ``texts``, in canonical order, without duplicates."""
    joined = "\n".join(texts)
    return [category for category in CATEGORY_ORDER if _PATTERNS[category].search(joined)]


def merge_sensitive(model: Sensitive, detected: Iterable[SensitiveCategory]) -> Sensitive:
    """Union of model and keyword categories; ``value`` is true when either side flags."""
    found = set(model.categories) | set(detected)
    categories = [c for c in CATEGORY_ORDER if c in found]
    return Sensitive(value=model.value or bool(categories), categories=categories)
