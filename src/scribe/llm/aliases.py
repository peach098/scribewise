"""Short per-request ids for the filter, rank and compose prompts (issue #71).

Real ids (``msg:<uuid>``, ``task:<…>``, ``commit:<uuid>``, ``earlier:<n>``) cost about 25
tokens each, and filter answers are mostly ids. The router therefore sends each request with
every id replaced by a short alias and maps the ids in the answer back *before* any validation
(``sanitize_filter_output``, ``invalid_source_ids``), so everything downstream, stored drafts and
selections included, only ever sees real ids.

Aliases, one counter per kind, numbered in list order from 1:

- ``t1…`` tasks, ``c1…`` commitments,
- ``h1…`` history messages, ``e1…`` history messages found by the earlier-conversation search
  (real id ``earlier:<n>``), so the prompt still tells the two apart,
- ``m1…`` incoming messages.

Each kind has its own counter, so the numbering of one block depends only on that block's own
list: a new incoming message does not renumber the history. History is numbered oldest first,
so a history window whose head does not move keeps the same aliases (and bytes) across turns
(issue #70).

A string in the answer that is not an alias of this request is passed through unchanged: it then
fails validation exactly like any id the request did not offer (real ids never appear in the
prompt, so it cannot match one by accident). If a real id of the request already looks like an
alias (synthetic test ids such as ``t1``), aliasing is skipped for that request and ids go out
as they are, so the mapping is never ambiguous.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from scribe.llm.provider import (
    CommitmentRef,
    DraftOutput,
    DraftRequest,
    FilterCandidates,
    FilterContextOutput,
    FilterContextRequest,
    HistoryItem,
    LLMRequest,
    MessageItem,
    TaskItem,
)

TASK_ALIAS = "t"
COMMITMENT_ALIAS = "c"
HISTORY_ALIAS = "h"
EARLIER_ALIAS = "e"
INCOMING_ALIAS = "m"

EARLIER_REF_PREFIX = "earlier:"  # context/earlier.py EARLIER_PREFIX (llm/ does not import context/)

_ALIAS = re.compile(r"[tchem][1-9][0-9]*")


def is_alias(value: str) -> bool:
    return _ALIAS.fullmatch(value) is not None


def is_earlier_alias(value: str) -> bool:
    """An alias of an earlier-conversation message (``e<n>``)."""
    return is_alias(value) and value.startswith(EARLIER_ALIAS)


class IdAliases:
    """Real id ↔ alias for one request. Empty (identity) when aliasing is off for it."""

    def __init__(self) -> None:
        self._alias: dict[str, str] = {}
        self._real: dict[str, str] = {}
        self._count: dict[str, int] = {}

    def add(self, real: str, kind: str) -> str:
        """The alias of ``real``, a new one of ``kind`` when it has none yet."""
        alias = self._alias.get(real)
        if alias is None:
            number = self._count.get(kind, 0) + 1
            self._count[kind] = number
            alias = f"{kind}{number}"
            self._alias[real] = alias
            self._real[alias] = real
        return alias

    def alias(self, real: str) -> str:
        return self._alias.get(real, real)

    def real(self, value: str) -> str:
        """The real id behind ``value``; ``value`` itself when it is not an alias of this
        request (it then fails validation like any id that was not offered)."""
        return self._real.get(value, value)

    def reals(self, values: Iterable[str]) -> list[str]:
        return [self.real(v) for v in values]

    def __len__(self) -> int:
        return len(self._alias)


def _history_kind(item: HistoryItem) -> str:
    return EARLIER_ALIAS if item.id.startswith(EARLIER_REF_PREFIX) else HISTORY_ALIAS


def _build(
    tasks: Sequence[TaskItem],
    commitments: Sequence[CommitmentRef],
    history: Sequence[HistoryItem],
    incoming: Sequence[MessageItem],
) -> IdAliases:
    """Stable parts first (tasks, commitments, history oldest first), incoming last; empty when
    a real id already looks like an alias."""
    aliases = IdAliases()
    ids = [
        *(t.id for t in tasks),
        *(c.id for c in commitments),
        *(h.id for h in history),
        *(m.id for m in incoming),
    ]
    if any(is_alias(i) for i in ids):
        return aliases
    for task in tasks:
        aliases.add(task.id, TASK_ALIAS)
    for commitment in commitments:
        aliases.add(commitment.id, COMMITMENT_ALIAS)
    for item in history:
        aliases.add(item.id, _history_kind(item))
    for message in incoming:
        aliases.add(message.id, INCOMING_ALIAS)
    return aliases


def _swap[T: (TaskItem, CommitmentRef, HistoryItem, MessageItem)](
    items: Sequence[T], aliases: IdAliases
) -> list[T]:
    return [item.model_copy(update={"id": aliases.alias(item.id)}) for item in items]


def alias_filter_request(req: FilterContextRequest) -> tuple[FilterContextRequest, IdAliases]:
    """The request as the model sees it, and the mapping to read its answer back."""
    candidates = req.candidates
    aliases = _build(
        candidates.tasks, candidates.commitments, candidates.history, req.incoming_messages
    )
    if not aliases:
        return req, aliases
    aliased = req.model_copy(
        update={
            "incoming_messages": _swap(req.incoming_messages, aliases),
            "candidates": FilterCandidates(
                history=_swap(candidates.history, aliases),
                tasks=_swap(candidates.tasks, aliases),
                commitments=_swap(candidates.commitments, aliases),
            ),
        }
    )
    return aliased, aliases


def unalias_filter_output(output: FilterContextOutput, aliases: IdAliases) -> FilterContextOutput:
    return output.model_copy(
        update={
            "history_ids": aliases.reals(output.history_ids),
            "task_ids": aliases.reals(output.task_ids),
            "commitment_ids": aliases.reals(output.commitment_ids),
        }
    )


def alias_draft_request(req: DraftRequest) -> tuple[DraftRequest, IdAliases]:
    aliases = _build(req.tasks, req.commitments, req.history, req.incoming_messages)
    if not aliases:
        return req, aliases
    aliased = req.model_copy(
        update={
            "incoming_messages": _swap(req.incoming_messages, aliases),
            "history": _swap(req.history, aliases),
            "tasks": _swap(req.tasks, aliases),
            "commitments": _swap(req.commitments, aliases),
        }
    )
    return aliased, aliases


def unalias_draft_output(output: DraftOutput, aliases: IdAliases) -> DraftOutput:
    if not aliases:
        return output
    return output.model_copy(
        update={
            "sources": [s.model_copy(update={"id": aliases.real(s.id)}) for s in output.sources],
            "conflicts": [
                c.model_copy(update={"with_id": aliases.real(c.with_id)}) for c in output.conflicts
            ],
        }
    )


def prompt_request(req: LLMRequest) -> LLMRequest:
    """What the router sends for ``req`` (aliased for the filter, rank and compose routes)."""
    if isinstance(req, FilterContextRequest):
        return alias_filter_request(req)[0]
    if isinstance(req, DraftRequest):
        return alias_draft_request(req)[0]
    return req
