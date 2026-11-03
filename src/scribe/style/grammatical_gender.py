"""The user's grammatical gender for drafts (issue #46): a local, deterministic guess from the
user's own hand-written messages, used only to *suggest* an answer.

``users.grammatical_gender`` (``masculine`` / ``feminine`` / NULL) is a hard constraint for every
draft (``DraftRequest.user``, ``prompts/draft.md``). It is only ever set by the user (the
one-off question or /settings, ``telegram/control_bot/grammatical_gender.py``); the guess here
only decides which answer the question shows first, marked as suggested.

:func:`infer_grammatical_gender` counts first-person forms in Russian only (other languages give
no signal; the drafting constraint itself applies to every language). It is deliberately
conservative: anything unclear counts as nothing.

- ``я`` followed directly (spaces only) by the form, with at most :data:`MAX_FILLERS` words from
  a small closed list of adverbs and particles in between («я уже сделала», «я тут приложила»,
  «я не проверял»). Anything else in between (an object, a pronoun, a comma: «я тебе написала»,
  «я файл приложила», «я, мол, …») ends the check for that ``я``. Forms without ``я`` never
  count («Готова.», «Права, переделаю»: they may answer a question about something else).
- The form is a past-tense verb («сделал» / «сделала», «отписался» / «отписалась»: a vowel
  before ``-л``), a masculine past tense from a closed list with its feminine pair («смог» /
  «смогла», «пришёл» / «пришла», «отошёл» / «отошла»), or a short adjective from a closed list
  («готов» / «готова», «рад» / «рада», «должен» / «должна», «сам» / «сама»). Common words that
  look like these forms are never one («канал», «отдел», «протокол», «дела», «числа»,
  «правила», «Данила»; :data:`_NOUNS`), nor are words like «вопрос» or «коллег».
- Spellings that leave the gender open are never counted, with or without spaces:
  «сделал(а)», «упустил (а)», «посмотрел/а», «сделал / сделала», «готов(а)».

Each message counts once per form it contains (a message with both forms counts for both). The
result is a value only when one form is found in at least :data:`MIN_MESSAGES` messages and the
other in at most a fifth of the messages with a form (:data:`MAX_OTHER_SHARE`; 4 to 1 is clear, 3
to 1 is not); everything else is "no signal". At most :data:`MAX_TEXTS` most recent messages are
looked at (:func:`recent_texts`, :func:`own_texts`).

Only the user's own hand-written words count: at setup the messages just imported (in memory),
for users set up before this feature :func:`own_texts`, their outgoing text messages still
stored (FR-035: the raw import is not kept). Forwarded messages never count (someone else's
words; the import skips them, stored ones carry ``messages.is_forwarded``, #47), nor do messages
sent through the agent (their wording was the drafting model's guess, the very thing this
setting replaces; ``drafts_repo.agent_send``). Style profile examples are not used: a profile
refresh learns from agent sends too. Nothing here logs or stores message text; callers log
counts only.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.db.models import Message, WorkChat
from scribe.db.repos import drafts as drafts_repo
from scribe.llm.provider import StyleSample

GrammaticalGender = Literal["masculine", "feminine"]
GRAMMATICAL_GENDERS: tuple[GrammaticalGender, ...] = ("masculine", "feminine")

MIN_MESSAGES = 3  # messages with the dominant form needed for a value
MAX_OTHER_SHARE = 0.2  # share of the gendered messages the other form may have
MAX_FILLERS = 2  # adverbs / particles allowed between «я» and the form
MAX_TEXTS = 300  # the most recent own messages looked at

# Adverbs and particles that may stand between «я» and the form («я уже сделал»). Closed on
# purpose: pronouns and nouns («я тебе…», «я файл…») end the check.
_FILLERS = frozenset(
    {
        "не", "уже", "уж", "тоже", "также", "еще", "только", "лишь", "вчера", "сегодня",
        "сейчас", "утром", "днем", "вечером", "же", "ж", "бы", "ведь", "вот", "просто", "точно",
        "давно", "сразу", "наконец", "конечно", "вроде", "кажется", "почти", "даже", "опять",
        "снова", "тут", "там", "здесь", "правда", "недавно", "только-что", "мол",
    }
)  # fmt: skip

# Short adjectives / participles a person says about themselves (masculine -> feminine).
_SHORT_ADJECTIVES = {
    "готов": "готова",
    "рад": "рада",
    "должен": "должна",
    "уверен": "уверена",
    "согласен": "согласна",
    "занят": "занята",
    "свободен": "свободна",
    "сам": "сама",
    "благодарен": "благодарна",
    "признателен": "признательна",
    "обязан": "обязана",
    "болен": "больна",
    "доволен": "довольна",
    "счастлив": "счастлива",
    "удивлен": "удивлена",
    "расстроен": "расстроена",
    "намерен": "намерена",
    "способен": "способна",
    "вынужден": "вынуждена",
}
# Past tenses without the vowel + «л» pattern (masculine -> feminine).
_IRREGULAR_PAST = {
    f"{prefix}{stem}": f"{prefix}{feminine}"
    for stem, feminine, prefixes in (
        ("мог", "могла", ("", "с", "по", "пере", "за")),
        (
            "шел",
            "шла",
            (
                "",
                "при",
                "у",
                "на",
                "за",
                "вы",
                "по",
                "до",
                "подо",
                "пере",
                "про",
                "во",
                "ото",
                "со",
                "обо",
                "разо",
            ),  # fmt: skip
        ),
        ("нес", "несла", ("при", "от", "за", "до", "пере", "у", "вы")),
        ("вез", "везла", ("при", "от", "у", "за", "до", "вы")),
        ("лег", "легла", ("", "при", "у")),
        ("рос", "росла", ("вы", "под")),
        ("пек", "пекла", ("ис", "на")),
        ("вык", "выкла", ("при", "от")),
        ("мерз", "мерзла", ("за",)),
        ("мок", "мокла", ("про",)),
        ("влекся", "влеклась", ("от", "у")),
        ("ошибся", "ошиблась", ("",)),
    )
    for prefix in prefixes
}
_MASCULINE_WORDS = frozenset(_SHORT_ADJECTIVES) | frozenset(_IRREGULAR_PAST)
_FEMININE_WORDS = frozenset(_SHORT_ADJECTIVES.values()) | frozenset(_IRREGULAR_PAST.values())

# Nouns that look like a past tense (vowel + «л» / «ла»): never a form of the user.
_MASCULINE_NOUNS = (
    "канал", "материал", "сигнал", "журнал", "портал", "финал", "интервал", "капитал",
    "персонал", "зал", "бал", "идеал", "ритуал", "сериал", "туториал", "мануал", "минерал",
    "генерал", "отдел", "раздел", "пробел", "предел", "удел", "протокол", "стол", "пол", "футбол",
    "волейбол", "баскетбол", "гол", "укол", "прикол", "пул", "стул", "аул", "артикул", "тыл",
    "пыл", "крокодил", "михаил", "даниил", "павел", "гавриил", "данил",
)  # fmt: skip
_NOUNS = frozenset(
    {
        *_MASCULINE_NOUNS,
        *(f"{noun}а" for noun in _MASCULINE_NOUNS),
        # plurals / genitives of neuter nouns, feminine nouns
        "дел", "тел", "сил", "чисел", "правил", "кресел", "зеркал", "одеял", "светил", "перил",
        "дела", "тела", "числа", "правила", "кресла", "масла", "зеркала", "весла", "сала",
        "крыла", "светила", "перила", "одеяла", "лекала", "шила", "школа", "сила", "игла",
        "стрела", "скала", "мгла", "метла", "смола", "юла", "хвала", "гаврила", "никола",
        "мол", "фрол", "карл",
    }
)  # fmt: skip

_VOWELS = "аеиоуыяэю"
_MASCULINE_PAST = re.compile(rf"^[а-я]+[{_VOWELS}]л(?:ся)?$")
_FEMININE_PAST = re.compile(rf"^[а-я]+[{_VOWELS}]л(?:а|ась)$")
_WORD = re.compile(r"[а-я]+(?:-[а-я]+)?")
_SENTENCE = re.compile(r"[^.!?\n;…]+[.!?\n;…]*")
_GAP = re.compile(r"\s*")  # what may stand between two words of one check
_OPEN_SPELLING = "(/"  # «сделал(а)», «упустил (а)», «посмотрел/а», «сделал / сделала»


@dataclass(frozen=True)
class GenderInference:
    """What the user's messages say: the value (None: no clear signal) and the message counts."""

    value: GrammaticalGender | None
    masculine: int  # messages with a masculine first-person form
    feminine: int  # messages with a feminine first-person form


def _norm(text: str) -> str:
    return text.lower().replace("ё", "е")


def _form(word: str) -> GrammaticalGender | None:
    """The gender of a first-person past tense / short adjective, if ``word`` is one."""
    if word in _NOUNS:
        return None
    if word in _MASCULINE_WORDS:
        return "masculine"
    if word in _FEMININE_WORDS:
        return "feminine"
    if _FEMININE_PAST.match(word):
        return "feminine"
    if _MASCULINE_PAST.match(word):
        return "masculine"
    return None


def _open_spelling(sentence: str, match: re.Match[str]) -> bool:
    before = sentence[: match.start()].rstrip()[-1:]
    after = sentence[match.end() :].lstrip()[:1]
    return any(char and char in _OPEN_SPELLING for char in (before, after))


def _after_ya(sentence: str, words: list[re.Match[str]], start: int) -> GrammaticalGender | None:
    """The form right after the «я» at ``words[start - 1]``, past a few fillers."""
    previous = words[start - 1]
    fillers = 0
    for match in words[start:]:
        if not _GAP.fullmatch(sentence, previous.end(), match.start()):
            return None  # «я, мол, …», «я — …»: not one clause
        word = match.group()
        if word in _FILLERS and fillers < MAX_FILLERS:
            fillers += 1
            previous = match
            continue
        if _open_spelling(sentence, match):
            return None
        return _form(word)
    return None


def forms_in(text: str) -> set[GrammaticalGender]:
    """The first-person gendered forms found in one message."""
    found: set[GrammaticalGender] = set()
    for sentence in _SENTENCE.findall(_norm(text)):
        words = list(_WORD.finditer(sentence))
        for index, match in enumerate(words):
            if match.group() == "я":
                form = _after_ya(sentence, words, index + 1)
                if form is not None:
                    found.add(form)
    return found


def infer_grammatical_gender(texts: Iterable[str]) -> GenderInference:
    """Count messages with masculine / feminine first-person forms (duplicates once)."""
    masculine = feminine = 0
    for text in dict.fromkeys(texts):
        forms = forms_in(text)
        masculine += "masculine" in forms
        feminine += "feminine" in forms
    high, low = max(masculine, feminine), min(masculine, feminine)
    value: GrammaticalGender | None = None
    if high >= MIN_MESSAGES and low <= (high + low) * MAX_OTHER_SHARE:
        value = "masculine" if masculine > feminine else "feminine"
    return GenderInference(value, masculine, feminine)


def recent_texts(samples: Sequence[StyleSample], limit: int = MAX_TEXTS) -> list[str]:
    """Setup: the texts of the ``limit`` most recent imported own messages."""
    newest = sorted(samples, key=lambda sample: sample.sent_at, reverse=True)[:limit]
    return [sample.text for sample in newest if sample.text.strip()]


async def own_texts(session: AsyncSession, user_id: uuid.UUID, limit: int = MAX_TEXTS) -> list[str]:
    """An existing user's ``limit`` most recent hand-written text messages still stored (in
    enabled work chats, kept for the retention period only); agent sends are left out."""
    messages = await session.scalars(
        select(Message.text)
        .join(WorkChat, Message.work_chat_id == WorkChat.id)
        .where(
            Message.user_id == user_id,
            WorkChat.user_id == user_id,
            WorkChat.enabled.is_(True),
            Message.is_outgoing.is_(True),
            Message.has_media.is_(False),
            Message.is_forwarded.is_(False),  # #47: someone else's words
            ~drafts_repo.agent_send(user_id, Message.work_chat_id, Message.telegram_message_id),
        )
        .order_by(Message.sent_at.desc())
        .limit(limit)
    )
    return [text for text in messages.all() if text and text.strip()]


__all__ = [
    "GRAMMATICAL_GENDERS",
    "MAX_TEXTS",
    "GenderInference",
    "GrammaticalGender",
    "forms_in",
    "infer_grammatical_gender",
    "own_texts",
    "recent_texts",
]
