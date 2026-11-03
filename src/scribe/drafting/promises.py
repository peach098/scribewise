"""Promise detection (issue #24): keyword rules merged with the model's ``creates_commitment``.

Models tend to read the user's casual habits («гляну», «пришлю», «отпишусь», "will check")
as style, not as promises, while ``creates_commitment`` is a hard stop for auto-send. This
pass is a safety net run on the draft text: it never removes or rewrites a model flag, it only
raises one the model missed (OR merge, like :func:`scribe.drafting.flags.merge_sensitive`).

Rules (English and Russian, word-start anchored):

- A promise needs a *future action by the user*: a first-person future verb («гляну»,
  «пришлю», «сделаю», …) or an English future form ("I'll", "I will", "will send", "let me get
  back", "I'm on it", …). A deadline word alone («сегодня», "today", "by Friday") never fires:
  "Thanks, got it today" is an acknowledgement, not a promise.
- Deadlines only strengthen: when several clauses hold an action, the one with a deadline is
  reported; and a very short acceptance of a deadline («Ок, до пятницы», "sure, by Friday")
  counts on its own, since agreeing to a date is a promise even without a verb.
- Cheap false-positive guards: quoted text («…», "…", “…”, lines starting with ">") is ignored;
  an action negated in its clause («не гляну», "I'll never", "I will not") does not count; a
  clause that only restates an earlier promise («как договаривались», "as promised") does not
  count either (the prompt says repeating a known promise is not new).

The reported fragment (``what``) is the clause the action starts, up to the end of its
sentence, trimmed; ``due`` is left to the model (no date resolution here).
"""

from __future__ import annotations

import re

from scribe.llm.provider import CreatesCommitment

_MAX_WHAT = 160

# First-person future verbs (Russian perfective present = future). Word-start anchored; each
# entry is a full word form, not a stem, so «посмотрю» matches but «посмотри» does not.
_RU_ACTIONS: tuple[str, ...] = (
    r"гляну",
    r"посмотрю",
    r"пришлю",
    r"отпишусь",
    r"сделаю",
    r"отправлю",
    r"проверю",
    r"скину",
    r"напишу",
    r"подготовлю",
    r"прочитаю",
    r"подпишу",
    r"возьму",
    r"беру",
    r"займусь",
    r"вернусь",
    r"уточню",
    r"разберусь",
    r"поправлю",
    r"исправлю",
    r"обновлю",
    r"доделаю",
    r"передам",
    r"созвонюсь",
    r"буду (?:делать|смотреть|готовить)",
)

_EN_VERBS = (
    r"send|review|check|look|do|get back|follow up|share|fix|update|post|prepare|write|sign"
    r"|read|handle|take|ping|reply|call|finish|deliver|ship|merge|confirm|test|draft"
)

_EN_ACTIONS: tuple[str, ...] = (
    r"i['’]ll",
    r"i will",
    r"i['’]m going to",
    r"i am going to",
    r"i['’]m gonna",
    rf"will (?:{_EN_VERBS})",
    r"let me (?:get back|check|look|see|find out|follow up)",
    r"(?:i['’]ll|will) get back",
    r"i['’]ll take it",
    r"i['’]m on it",
    r"i am on it",
)

_ACTION = re.compile(
    r"(?<![\w'’])(?:" + "|".join((*_RU_ACTIONS, *_EN_ACTIONS)) + r")(?![\w'’])",
    re.IGNORECASE,
)

_DEADLINE = re.compile(
    r"(?<![\w])(?:"
    r"сегодня|завтра|послезавтра|к вечеру|к утру|до вечера|до конца (?:дня|недели|месяца)"
    r"|до (?:понедельника|вторника|среды|четверга|пятницы|субботы|воскресенья)"
    r"|к (?:понедельнику|вторнику|среде|четвергу|пятнице|субботе|воскресенью)"
    r"|в (?:понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)"
    r"|на (?:этой|следующей) неделе|через (?:час|пару часов|\d+ (?:минут\w*|час\w*))"
    r"|today|tonight|tomorrow|eod|eow|end of (?:the )?(?:day|week|month)"
    r"|(?:by|on|before|until|till) (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|tomorrow|tonight|noon|the end of)"
    r"|this (?:afternoon|evening|week)|next week|asap"
    r")(?![\w])",
    re.IGNORECASE,
)

# A sentence that only accepts a deadline: «Ок, до пятницы», "sure, by Friday".
_ACCEPT = re.compile(
    r"^\W*(?:ок(?:ей)?|окей|да|хорошо|договорились|конечно|принято|ok(?:ay)?|sure|yes|yep|deal"
    r"|of course|will do|sounds good)(?![\w])",
    re.IGNORECASE,
)
_ACCEPT_MAX_OTHER_WORDS = 1  # «Ок, тогда до пятницы»; not "Yes, it shipped today"

# A clause that is only "on it" (a common casual "I'm doing it now").
_ON_IT = re.compile(r"^\W*on it\W*$", re.IGNORECASE)

# "will send" with no subject is the user's own («sure, will send»); "Boris will send" is not.
_SUBJECTLESS_WILL_AFTER = re.compile(
    r"(?:^|(?<![\w])(?:and|then|so|but|ok|okay|sure|yes|yep|i))\W*$", re.IGNORECASE
)

_NEGATION_BEFORE = re.compile(
    r"(?<![\w])(?:не|ни|нельзя|not|never|won['’]t|can['’]t|cannot|don['’]t|didn['’]t"
    r"|wouldn['’]t|couldn['’]t|unable)(?![\w])",
    re.IGNORECASE,
)
_NEGATION_AFTER = re.compile(r"^\W*(?:not|never|no longer)(?![\w])", re.IGNORECASE)
_NEGATION_WINDOW_WORDS = 3

_RESTATEMENT = re.compile(
    r"(?<![\w])(?:как (?:и )?(?:договаривались|договорились|обещал\w*|планировал\w*)"
    r"|как и было|as (?:promised|planned|agreed|discussed)|like i said)(?![\w])",
    re.IGNORECASE,
)

_QUOTED = re.compile(r"«[^»]*»|\"[^\"\n]*\"|“[^”]*”|„[^“”]*[“”]|^>.*$", re.MULTILINE)
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]*")
_CLAUSE_BREAK = re.compile(r"[,;:—–]|\s-\s|\((?=\s*\w)")


def detect_promise(text: str) -> str | None:
    """The fragment of ``text`` that promises a future action by the user, or None."""
    unquoted = _QUOTED.sub(" ", text)
    best: tuple[int, str] | None = None
    for sentence in _SENTENCE.findall(unquoted):
        found = _sentence_promise(sentence)
        if found is None:
            continue
        rank = 1 if _DEADLINE.search(found) else 0
        if best is None or rank > best[0]:
            best = (rank, found)
    return None if best is None else best[1]


def merge_commitment(model: CreatesCommitment, detected: str | None) -> CreatesCommitment:
    """``value`` is true when either side says so. A model flag is kept as is; it only gains a
    ``what`` when it had none. A keyword-only flag carries the fragment and no ``due``."""
    if model.value:
        if model.what or not detected:
            return model
        return model.model_copy(update={"what": detected})
    if detected is None:
        return model
    return CreatesCommitment(value=True, what=detected, due=None)


def _sentence_promise(sentence: str) -> str | None:
    if _RESTATEMENT.search(sentence):
        return None
    if _accepts_deadline(sentence):
        return _trim(sentence)
    clauses = _clauses(sentence)
    for start, clause in clauses:
        if _ON_IT.search(clause):
            return _trim(sentence[start:])
    for match in _ACTION.finditer(sentence):
        clause_start = _clause_start(clauses, match.start())
        prefix = sentence[clause_start : match.start()]
        if match.group().lower().startswith("will") and not _SUBJECTLESS_WILL_AFTER.search(prefix):
            continue
        if _NEGATION_BEFORE.search(" ".join(prefix.split()[-_NEGATION_WINDOW_WORDS:])):
            continue
        if _NEGATION_AFTER.search(sentence[match.end() :]):
            continue
        return _trim(sentence[clause_start:])
    return None


def _accepts_deadline(sentence: str) -> bool:
    accept = _ACCEPT.search(sentence)
    if accept is None or _NEGATION_BEFORE.search(sentence):
        return False
    rest = sentence[accept.end() :]
    deadline = _DEADLINE.search(rest)
    if deadline is None:
        return False
    other = re.findall(r"\w+", rest[: deadline.start()] + " " + rest[deadline.end() :])
    return len(other) <= _ACCEPT_MAX_OTHER_WORDS


def _clauses(sentence: str) -> list[tuple[int, str]]:
    """(start offset, text) of each clause of ``sentence``."""
    out: list[tuple[int, str]] = []
    start = 0
    for brk in _CLAUSE_BREAK.finditer(sentence):
        out.append((start, sentence[start : brk.start()]))
        start = brk.end()
    out.append((start, sentence[start:]))
    return out


def _clause_start(clauses: list[tuple[int, str]], offset: int) -> int:
    start = 0
    for clause_start, _ in clauses:
        if clause_start <= offset:
            start = clause_start
    return start


def _trim(fragment: str) -> str:
    cleaned = " ".join(fragment.split()).strip(" .!?,;:—–-")
    if len(cleaned) > _MAX_WHAT:
        cleaned = cleaned[: _MAX_WHAT - 1].rstrip() + "…"
    return cleaned
