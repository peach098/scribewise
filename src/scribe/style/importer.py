"""One-time import of the user's own messages for style learning (FR-035).

Only the user's own messages, only in enabled work chats (plus, with work account mode on, the
recent private chats ``scribe.chats.work_account.import_targets`` adds; specs/008), only from
the last ``style_import_months``; messages the user forwarded are not their writing and are
skipped (#47). The samples live in memory only: they are handed to the profile
builder and then dropped; nothing raw is persisted (the profile keeps a few chosen examples).
Each sample knows its chat and whether it is a group chat (the global profile limits group
chats in its sample, ``style/profiles.py``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.db.models import ChatKind
from scribe.db.repos import chats as chats_repo
from scribe.llm.provider import StyleSample
from scribe.logging import get_logger
from scribe.telegram.gateway import MessagingGateway

log = get_logger("scribe.style")

DAYS_PER_MONTH = 30.44


@dataclass(frozen=True)
class ImportTarget:
    """One chat the import reads. ``work_chat_id`` is None for a private dialog that is not a
    work chat yet (work account mode, specs/008 research R6)."""

    telegram_chat_id: int
    title: str
    kind: ChatKind
    work_chat_id: uuid.UUID | None = None


@dataclass
class ImportResult:
    samples: list[StyleSample] = field(default_factory=list)
    # Own-message counts per Telegram chat id: input for per-contact profile slots (US4).
    per_chat_counts: dict[int, int] = field(default_factory=dict)
    # The same samples by Telegram chat id (a private chat is one person, a group one contact):
    # per-contact profiles are learned from them. In memory only, like ``samples``.
    by_chat: dict[int, list[StyleSample]] = field(default_factory=dict)


async def import_own_messages(
    gateway: MessagingGateway,
    session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    *,
    settings: Settings,
    clock: Clock = utcnow,
    targets: list[ImportTarget] | None = None,
) -> ImportResult:
    """``targets``: the chats to read; None reads the enabled work chats (as before 008)."""
    since = clock() - timedelta(days=round(settings.style_import_months * DAYS_PER_MONTH))
    if targets is None:
        async with session_factory() as session:
            chats = await chats_repo.list_work_chats(session, user_id, enabled_only=True)
        targets = [ImportTarget(c.telegram_chat_id, c.title, c.kind, c.id) for c in chats]

    result = ImportResult()
    for target in targets:
        chat_id, title = target.telegram_chat_id, target.title
        group = target.kind != ChatKind.private
        count = 0
        async for message in gateway.fetch_own_history(user_id, chat_id, since):
            if (
                not message.is_outgoing
                or message.is_forwarded  # #47: a forward is not the user's writing
                or not message.text.strip()
                or message.sent_at < since
            ):
                continue
            sample = StyleSample(
                text=message.text,
                sent_at=message.sent_at,
                contact_display_name=title,
                chat_id=chat_id,
                group=group,
            )
            result.samples.append(sample)
            result.by_chat.setdefault(chat_id, []).append(sample)
            count += 1
        result.per_chat_counts[chat_id] = count
    log.info(
        "style_import_finished",
        user_id=str(user_id),
        chats=len(targets),
        samples=len(result.samples),
        auto_targets=sum(1 for target in targets if target.work_chat_id is None),
    )
    return result
