"""Pre-filter for ``extract.commitments`` (issue #72, specs/012 research F9 / proposal 7).

Every outgoing message used to cost one LLM call, including «ок», «спасибо» and "+". This gate
skips the call only when nothing in the message could be a promise. It is tuned for recall: a
missed commitment is worse than a wasted call, so when in doubt it says "extract".

A message goes to the LLM (:attr:`GateDecision.extract`) when any of these holds, checked in
this order (the first one is the reported ``reason``):

1. ``long``: it has more than :data:`LONG_WORDS` words (a long message is not "trivial");
2. ``promise``: the strict draft detector (:func:`scribe.drafting.promises.detect_promise`)
   finds a promise;
3. ``action``: a loose action word, RU or EN, in any person or tense and without the negation
   and subject checks of the strict detector («сделаю», «пришлём», «будет готово», «постараюсь»,
   "will", "I'll", "on it", "send", "fix", ...);
4. ``date``: a date or time word or any digit (weekdays, months, «завтра», «до пятницы»,
   «к вечеру», "tomorrow", "by Friday", "EOD", "12.11", "15:00", "3pm");
5. ``acceptance``: a short acceptance («ок», «да», «договорились», «+», "sure", "ok", "👍")
   while the context it answers (``context``: the recent incoming messages, or the Jira request)
   asks for something or names a date: agreeing to a request is a promise (prompts/commitments.md).

Anything else («спасибо», «понял, спасибо», "thanks!", "+" with no request around) is skipped
with reason ``no_cue``. :func:`log_decision` logs every decision with ids and the reason only
(Principle II: never the text), so the skip rate can be measured from the logs.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from scribe.drafting.promises import detect_promise
from scribe.logging import get_logger

log = get_logger("scribe.commitments")

LONG_WORDS = 8  # more words than this always go to the LLM

GateReason = Literal[
    "long",
    "promise",
    "action",
    "date",
    "acceptance",
    "no_cue",
    "draft_flag_true",
    "draft_flag_false",
]
GateSource = Literal["telegram", "jira_comment"]

_WORD = re.compile(r"\w+")

# Loose action stems: any person, tense or aspect («сделал», «сделаем», «сделаю» all match).
# Over-matching only costs a call; these never decide that something *is* a promise.
_RU_ACTION_STEMS = (
    r"гля[нд]|посмотр|пришл|присыл|отпиш|отпис|сдела|дела[юе]|отправ|провер|скин|скида|напиш"
    r"|подготов|прочита|подпиш|возьм|бер[уёе]|займ|верн|уточн|разбер|поправ|исправ|обнов|довед"
    r"|доде|переда|созвон|позвон|наберу|постара|попробу|успе|обеща|договор|закин|выложу|выкач"
    r"|залью|залив|закро|закры|запущ|запуст|почин|собер|сброш|сброс|подним|подня|подключ|настро"
    r"|оформ|соглас|подтвер|решу|реши|допиш|доработ|протест|задеплою|выкат|релиз|смерж|замерж"
    r"|дам знать|буду|будет|будем|будут|сможе|могу|ждите|жди"
)
_EN_ACTION_WORDS = (
    r"i['’]ll|i['’]m|i am|we['’]ll|will|shall|gonna|going to|let me|let['’]s|on it|i can|we can"
    r"|promise\w*|send\w*|review\w*|check\w*|look\w*|share\w*|fix\w*|updat\w*|post\w*|prepar\w*"
    r"|writ\w*|sign\w*|handl\w*|tak\w*|ping\w*|repl\w*|call\w*|finish\w*|deliver\w*|ship\w*"
    r"|merg\w*|confirm\w*|test\w*|draft\w*|deploy\w*|follow\w*|get back|asap"
)
_ACTION = re.compile(
    rf"(?<![\w'’])(?:(?:{_RU_ACTION_STEMS})\w*|(?:{_EN_ACTION_WORDS}))(?![\w'’])",
    re.IGNORECASE,
)

# Date and time words, RU and EN, every case form; plus any digit (dates, times, durations).
_DATE = re.compile(
    r"\d"
    r"|(?<!\w)(?:"
    r"сегодня|завтра|послезавтра|вечер\w*|утр[оаеу]\w*|обед\w*|ночь\w*|ночью|днём|днем"
    r"|недел\w*|месяц\w*|выходн\w*|понедельн\w*|вторник\w*|сред[аеуы]|четверг\w*|пятниц\w*"
    r"|суббот\w*|воскресень\w*|январ\w*|феврал\w*|март\w*|апрел\w*|ма[йяе]|июн\w*|июл\w*"
    r"|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*|срок\w*|дедлайн\w*|позже|скоро|сразу"
    r"|сейчас|час\w*|минут\w*|до конца|на днях"
    r"|today|tonight|tomorrow|eod|eow|cob|noon|morning|afternoon|evening|night|weekend|week\w*"
    r"|month\w*|monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|tues|wed|thu"
    r"|thur|thurs|fri|sat|sun|january|february|march|april|may|june|july|august|september"
    r"|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec|deadline\w*"
    r"|later|soon|by|until|till|before|hours?|minutes?|mins?|asap"
    r")(?!\w)",
    re.IGNORECASE,
)

# A short acceptance: on its own it promises nothing, but it may accept a request.
_ACCEPTANCE = re.compile(
    r"(?<!\w)(?:ок(?:ей|и)?|окей|оке|ok(?:ay)?|kk?|да|ага|угу|ладно|лады|хорошо|норм|давай"
    r"|договорились|принято|принял\w*|понял\w*|конечно|без проблем|sure|yes|yep|yeah|yup|deal"
    r"|fine|got it|noted|roger|of course|no problem|np|sounds good|agreed)(?!\w)"
    r"|(?<![\w+])\+(?![\w+])|👍|👌|✅|🤝|🙆",
    re.IGNORECASE,
)

# The context asks the user for something: a question or a request word.
_REQUEST = re.compile(
    r"\?|(?<!\w)(?:можешь|сможешь|сможете|можете|пришли\w*|скинь\w*|сделай\w*|посмотри\w*"
    r"|глянь\w*|проверь\w*|отправь\w*|напиши\w*|подготовь\w*|нужно|надо|пожалуйста|прошу"
    r"|please|pls|can you|could you|would you|will you|need|needs|asap)(?!\w)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GateDecision:
    extract: bool
    reason: GateReason


def needs_extraction(text: str, context: Iterable[str] = ()) -> GateDecision:
    """Whether ``text`` (an outgoing message or the user's Jira comment) may hold a promise.

    ``context`` is what the message may answer (recent incoming messages, the Jira request): it
    only turns a short acceptance into a call."""
    if len(_WORD.findall(text)) > LONG_WORDS:
        return GateDecision(True, "long")
    if detect_promise(text) is not None:
        return GateDecision(True, "promise")
    if _ACTION.search(text):
        return GateDecision(True, "action")
    if _DATE.search(text):
        return GateDecision(True, "date")
    if _ACCEPTANCE.search(text) and any(
        _REQUEST.search(item) or _DATE.search(item) or _ACTION.search(item) for item in context
    ):
        return GateDecision(True, "acceptance")
    return GateDecision(False, "no_cue")


def log_decision(
    user_id: uuid.UUID,
    decision: GateDecision,
    *,
    source: GateSource,
) -> None:
    """One ``commitment_extraction_gate`` line per outgoing message or Jira comment: the skip
    rate is ``extract=false`` over all lines. Ids and the reason only, never the text."""
    log.info(
        "commitment_extraction_gate",
        user_id=str(user_id),
        source=source,
        extract=decision.extract,
        reason=decision.reason,
    )


__all__ = [
    "LONG_WORDS",
    "GateDecision",
    "GateReason",
    "GateSource",
    "log_decision",
    "needs_extraction",
]
