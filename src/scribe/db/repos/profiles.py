"""``style_profiles`` and ``style_rule_proposals`` rows, always scoped by ``user_id``."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.db.models import (
    ProfileScope,
    ProfileState,
    ProposalState,
    StyleProfile,
    StyleRuleProposal,
)


async def create_profile(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    scope: ProfileScope,
    rules_json: dict[str, Any],
    summary_text: str,
    examples: list[str],
    sample_size: int,
    contact_id: uuid.UUID | None = None,
    version: int = 1,
    user_rules_json: dict[str, Any] | None = None,
    state: ProfileState = ProfileState.current,
) -> StyleProfile:
    profile = StyleProfile(
        user_id=user_id,
        scope=scope,
        contact_id=contact_id,
        version=version,
        is_current=state == ProfileState.current,
        rules_json=rules_json,
        user_rules_json=user_rules_json or {},
        summary_text=summary_text,
        examples=examples,
        sample_size=sample_size,
        state=state,
    )
    session.add(profile)
    await session.flush()
    return profile


async def get_profile(
    session: AsyncSession, user_id: uuid.UUID, profile_id: uuid.UUID
) -> StyleProfile | None:
    result = await session.execute(
        select(StyleProfile).where(StyleProfile.user_id == user_id, StyleProfile.id == profile_id)
    )
    return result.scalar_one_or_none()


def _current[Q: Select[Any]](
    query: Q, user_id: uuid.UUID, scope: ProfileScope, contact_id: uuid.UUID | None
) -> Q:
    """``query`` (of the row or some of its columns) narrowed to the current profile."""
    query = query.where(
        StyleProfile.user_id == user_id,
        StyleProfile.scope == scope,
        StyleProfile.is_current.is_(True),
    )
    if scope == ProfileScope.contact:
        query = query.where(StyleProfile.contact_id == contact_id)
    return query.order_by(StyleProfile.version.desc()).limit(1)


async def get_current_profile(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    scope: ProfileScope,
    contact_id: uuid.UUID | None = None,
) -> StyleProfile | None:
    result = await session.execute(_current(select(StyleProfile), user_id, scope, contact_id))
    return result.scalar_one_or_none()


async def get_global_user_rules(session: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    """Only ``user_rules_json`` of the current global profile (``{}`` when there is none): no
    examples or summary are loaded and decrypted."""
    result = await session.execute(
        _current(select(StyleProfile.user_rules_json), user_id, ProfileScope.global_, None)
    )
    rules = result.scalar_one_or_none()
    return dict(rules) if rules else {}


async def list_profiles(
    session: AsyncSession, user_id: uuid.UUID, *, state: ProfileState | None = None
) -> list[StyleProfile]:
    query = select(StyleProfile).where(StyleProfile.user_id == user_id)
    if state is not None:
        query = query.where(StyleProfile.state == state)
    result = await session.execute(query.order_by(StyleProfile.scope, StyleProfile.version))
    return list(result.scalars().all())


async def create_proposal(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    style_profile_id: uuid.UUID,
    rule_json: dict[str, Any],
    evidence_draft_ids: list[uuid.UUID],
) -> StyleRuleProposal:
    proposal = StyleRuleProposal(
        user_id=user_id,
        style_profile_id=style_profile_id,
        rule_json=rule_json,
        evidence_draft_ids=evidence_draft_ids,
    )
    session.add(proposal)
    await session.flush()
    return proposal


async def get_proposal(
    session: AsyncSession, user_id: uuid.UUID, proposal_id: uuid.UUID
) -> StyleRuleProposal | None:
    result = await session.execute(
        select(StyleRuleProposal).where(
            StyleRuleProposal.user_id == user_id, StyleRuleProposal.id == proposal_id
        )
    )
    return result.scalar_one_or_none()


async def set_proposal_state(
    session: AsyncSession, user_id: uuid.UUID, proposal_id: uuid.UUID, state: ProposalState
) -> None:
    proposal = await get_proposal(session, user_id, proposal_id)
    if proposal is not None:
        proposal.state = state
        await session.flush()


# --- US4: lineages, proposals and slots ---------------------------------------------------------


async def list_lineage(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    scope: ProfileScope,
    contact_id: uuid.UUID | None = None,
) -> list[StyleProfile]:
    """Every version of one profile (the global one, or one contact's), newest first."""
    query = select(StyleProfile).where(StyleProfile.user_id == user_id, StyleProfile.scope == scope)
    if scope == ProfileScope.contact:
        query = query.where(StyleProfile.contact_id == contact_id)
    result = await session.execute(query.order_by(StyleProfile.version.desc()))
    return list(result.scalars().all())


async def list_current_contact_profiles(
    session: AsyncSession, user_id: uuid.UUID
) -> list[StyleProfile]:
    result = await session.execute(
        select(StyleProfile).where(
            StyleProfile.user_id == user_id,
            StyleProfile.scope == ProfileScope.contact,
            StyleProfile.state == ProfileState.current,
        )
    )
    return list(result.scalars().all())


async def list_proposals(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    profile_ids: list[uuid.UUID] | None = None,
    state: ProposalState | None = None,
) -> list[StyleRuleProposal]:
    query = select(StyleRuleProposal).where(StyleRuleProposal.user_id == user_id)
    if profile_ids is not None:
        query = query.where(StyleRuleProposal.style_profile_id.in_(profile_ids))
    if state is not None:
        query = query.where(StyleRuleProposal.state == state)
    result = await session.execute(query.order_by(StyleRuleProposal.created_at))
    return list(result.scalars().all())


async def lock_profiles(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Serialize every write to one user's profiles (builds, refresh, undo, rules, slots) for
    the rest of this transaction: ``pg_advisory_xact_lock(hashtext('profiles:<user_id>'))``.
    Re-entrant within a transaction; released at commit or rollback. Read the rows to change
    *after* taking it."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"profiles:{user_id}"}
    )
