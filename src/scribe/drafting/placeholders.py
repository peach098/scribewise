"""Filling a needs-info draft's ``[[...]]`` placeholders with values the user replies with
(contracts/control-chat.md "Draft card", issue #2).

The database-free core: :func:`fill_slots` lists what the card asks for, :func:`parse_values`
splits the user's reply, :func:`fill` substitutes. The slots are the placeholders the model
listed whose marker is in the text (in the model's order), then any other marker found in the
text (in text order, asked as the marker itself). A marker used twice is one slot, replaced
everywhere.

:func:`store_filled_version` stores the filled text as a new draft version, the way regenerate
does (old draft ``superseded``, under the draft -> turn row locks), but without an LLM call.
The filled text is the user's own words, so it is stored like an edit: in ``text`` (what the
card shows) and in ``edited_text``, both encrypted. Approving that version therefore sends it
as an edited reply (``edited_and_approved``, "✅ Sent (edited)"), which is what style learning
reads as a user edit. ``output_json`` gets ``needs_info=false`` and no placeholders, so Send no
longer asks about unresolved placeholders; it is re-packed through ``stored_output`` (content
encrypted, the draft text never in the JSON), so the values never reach the unencrypted JSON.
Values are message content: never log them.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.db.models import ActivityKind, ConversationTurn, Draft, DraftState, TurnState
from scribe.db.repos import activity as activity_repo
from scribe.db.repos import drafts as drafts_repo
from scribe.drafting.composer import DraftNoLongerPending
from scribe.drafting.selection import carried
from scribe.drafting.stored_output import REPLY_TO_KEY, load_stored_output, pack_output
from scribe.llm.provider import Placeholder
from scribe.logging import get_logger

log = get_logger("scribe.drafting")

PLACEHOLDER = re.compile(r"\[\[[^\[\]]+\]\]")


@dataclass(frozen=True)
class Slot:
    marker: str  # the exact [[...]] token in the draft text
    question: str  # what the card asks for


class ValueCountMismatch(ValueError):
    def __init__(self, expected: int, got: int) -> None:
        super().__init__(f"expected {expected} values, got {got}")
        self.expected = expected
        self.got = got


def fill_slots(text: str, placeholders: Sequence[Placeholder]) -> list[Slot]:
    """The placeholders still in ``text``, in the order the card lists them."""
    in_text = list(dict.fromkeys(PLACEHOLDER.findall(text)))
    slots: dict[str, Slot] = {}
    for placeholder in placeholders:
        marker = placeholder.marker.strip()
        if marker in in_text and marker not in slots:
            slots[marker] = Slot(marker, placeholder.question.strip() or marker[2:-2].strip())
    for marker in in_text:
        slots.setdefault(marker, Slot(marker, marker[2:-2].strip()))
    return list(slots.values())


def parse_values(reply: str, count: int) -> list[str]:
    """One slot: the whole reply. Several: one value per non-blank line, in order.

    Raises :class:`ValueCountMismatch` instead of guessing when the count differs.
    """
    if count == 1:
        value = reply.strip()
        values = [value] if value else []
    else:
        values = [line.strip() for line in reply.splitlines() if line.strip()]
    if len(values) != count:
        raise ValueCountMismatch(count, len(values))
    return values


def fill(text: str, slots: Sequence[Slot], values: Sequence[str]) -> str:
    """Replace every occurrence of each slot's marker in one pass (a value that happens to
    look like another marker is never substituted again)."""
    if len(slots) != len(values):
        raise ValueCountMismatch(len(slots), len(values))
    mapping = {slot.marker: value for slot, value in zip(slots, values, strict=True)}
    return PLACEHOLDER.sub(lambda m: mapping.get(m.group(0), m.group(0)), text)


def draft_slots(draft: Draft) -> list[Slot]:
    """The fillable placeholders of a stored draft (``drafts.output_json``, decrypted)."""
    output = load_stored_output(draft.output_json, draft.text).output
    # Unreadable or purged output: the markers in the text only.
    placeholders = output.placeholders if output is not None else []
    return fill_slots(draft.text or "", placeholders)


def _filled_output_json(old: Draft) -> dict[str, Any]:
    """``old``'s stored output without needs-info and placeholders (content stays encrypted)."""
    stored = load_stored_output(old.output_json, old.text)
    if stored.output is None:
        return {k: v for k, v in (old.output_json or {}).items() if k != "placeholders"} | {
            "needs_info": False
        }
    output = stored.output.model_copy(update={"needs_info": False, "placeholders": []})
    packed = pack_output(output, stored.system_warnings, stored.reply_to_message_id)
    if not stored.reply_to_recorded:  # keep "not recorded" (drafts from before it was kept)
        packed.pop(REPLY_TO_KEY, None)
    return packed


async def store_filled_version(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: uuid.UUID,
    draft_id: uuid.UUID,
    values: Sequence[str],
    deadlines_s: tuple[int, int],
    clock: Clock = utcnow,
) -> Draft:
    """Store ``draft_id`` with its placeholders filled as the turn's next pending version.

    Raises :class:`DraftNoLongerPending` when the draft or its turn moved on meanwhile and
    :class:`ValueCountMismatch` when ``values`` does not match the draft's slots.
    """
    now = clock()
    remind_after_s, expire_after_s = deadlines_s
    async with session_factory() as session:
        # Lock order everywhere: draft, then turn.
        old = (
            await session.execute(
                select(Draft)
                .where(Draft.user_id == user_id, Draft.id == draft_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if old is None or old.state != DraftState.pending or old.expires_at <= now:
            raise DraftNoLongerPending("draft is no longer pending")
        slots = draft_slots(old)
        filled = fill(old.text or "", slots, values)
        turn = (
            await session.execute(
                select(ConversationTurn)
                .where(
                    ConversationTurn.user_id == user_id,
                    ConversationTurn.id == old.conversation_turn_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if turn is None or turn.state != TurnState.pending_review:
            raise DraftNoLongerPending("conversation turn is no longer pending review")
        await drafts_repo.finalize(session, user_id, old.id, DraftState.superseded, at=now)
        version = await drafts_repo.next_version(session, user_id, turn.id)
        draft = await drafts_repo.create_draft(
            session,
            user_id,
            conversation_turn_id=turn.id,
            text=filled,
            output_json=_filled_output_json(old),
            prepared_at=now,
            version=version,
            style_profile_id=old.style_profile_id,
            prompt_version=old.prompt_version,
            llm_provider=old.llm_provider,
            llm_model=old.llm_model,
            # Issue #73: the same selection and regenerate count (no LLM ran for this version).
            llm_usage_json=carried(old.llm_usage_json),
            remind_after=timedelta(seconds=remind_after_s),
            expire_after=timedelta(seconds=expire_after_s),
        )
        draft.edited_text = filled
        draft.outdated = old.outdated  # filling covers no new messages
        await activity_repo.record(
            session,
            user_id,
            ActivityKind.draft_prepared,
            at=now,
            draft_id=draft.id,
            conversation_turn_id=turn.id,
            meta={
                "version": version,
                "previous_draft_id": str(old.id),
                "filled_placeholders": len(slots),
                "needs_info": False,
                "outdated": draft.outdated,
            },
        )
        await session.commit()
    log.info(
        "placeholders_filled",
        user_id=str(user_id),
        draft_id=str(draft.id),
        previous_draft_id=str(draft_id),
        version=version,
        filled=len(slots),
    )
    return draft
