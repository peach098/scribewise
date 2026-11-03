"""``users`` and ``telegram_sessions`` rows."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.config import Settings
from scribe.db.models import LoginMethod, SessionStatus, TelegramSession, User, UserState


async def create_user(
    session: AsyncSession,
    *,
    control_chat_id: int,
    settings: Settings,
    display_name: str = "",
    timezone: str = "UTC",
    language: str = "en",
) -> User:
    """Create a user in ``setting_up`` with the global defaults from ``Settings``."""
    user = User(
        telegram_control_chat_id=control_chat_id,
        display_name=display_name,
        timezone=timezone,
        language=language,
        working_days=list(settings.working_days),
        work_start=settings.work_start,
        work_end=settings.work_end,
        delay_min_s=settings.delay_min_s,
        delay_max_s=settings.delay_max_s,
        state=UserState.setting_up,
    )
    session.add(user)
    await session.flush()
    return user


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


async def get_by_control_chat(session: AsyncSession, control_chat_id: int) -> User | None:
    result = await session.execute(
        select(User).where(User.telegram_control_chat_id == control_chat_id)
    )
    return result.scalar_one_or_none()


async def find_user_by_telegram_user_id(
    session: AsyncSession, telegram_user_id: int
) -> User | None:
    """Across users: who owns this Telegram account (FR-013 "account taken" check)."""
    result = await session.execute(select(User).where(User.telegram_user_id == telegram_user_id))
    return result.scalar_one_or_none()


async def update_user(session: AsyncSession, user_id: uuid.UUID, **values: Any) -> User | None:
    user = await session.get(User, user_id)
    if user is None:
        return None
    for key, value in values.items():
        setattr(user, key, value)
    await session.flush()
    return user


async def set_state(session: AsyncSession, user_id: uuid.UUID, state: UserState) -> None:
    await update_user(session, user_id, state=state)


async def delete_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Delete the user; every per-user row cascades and a trigger removes ``bot_fsm_state``."""
    await session.execute(delete(User).where(User.id == user_id))


async def count_by_state(session: AsyncSession, state: UserState) -> int:
    result = await session.execute(
        select(func.count()).select_from(User).where(User.state == state)
    )
    return int(result.scalar_one())


# --- telegram_sessions --------------------------------------------------------------------------


async def get_telegram_session(session: AsyncSession, user_id: uuid.UUID) -> TelegramSession | None:
    result = await session.execute(
        select(TelegramSession).where(TelegramSession.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def upsert_telegram_session(
    session: AsyncSession,
    user_id: uuid.UUID,
    session_string: str,
    *,
    method: LoginMethod,
    connected_at: datetime,
    account_label: str | None = None,
) -> TelegramSession:
    """Store a finished login; on reconnect the row is updated in place and keeps its id.

    ``account_label`` is the connected account as shown to the user (phone already masked).
    """
    row = await get_telegram_session(session, user_id)
    if row is None:
        row = TelegramSession(
            user_id=user_id,
            session_string=session_string,
            login_method=method,
            connected_at=connected_at,
            account_label=account_label,
        )
        session.add(row)
    else:
        row.session_string = session_string
        row.status = SessionStatus.active
        row.last_error = None
        row.login_method = method
        row.connected_at = connected_at
        row.account_label = account_label
    await session.flush()
    return row


async def delete_telegram_session(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Remove the user's session row (setup "This is not my account"); no-op if absent."""
    await session.execute(delete(TelegramSession).where(TelegramSession.user_id == user_id))


async def set_telegram_session_status(
    session: AsyncSession,
    user_id: uuid.UUID,
    status: SessionStatus,
    last_error: str | None = None,
) -> None:
    # A plain UPDATE: the row's session string is never loaded, so this works even when it
    # cannot be decrypted (the client pool marks such a session as ``error``, #59).
    await session.execute(
        update(TelegramSession)
        .where(TelegramSession.user_id == user_id)
        .values(status=status, last_error=last_error)
        .execution_options(synchronize_session="fetch")
    )


async def list_user_ids_with_session_status(
    session: AsyncSession, status: SessionStatus
) -> list[uuid.UUID]:
    """Across users: used by the client pool at boot. Users whose access is suspended
    (``users.access_suspended_at``, specs/005 research Q6) are left out."""
    result = await session.execute(
        select(TelegramSession.user_id)
        .join(User, User.id == TelegramSession.user_id)
        .where(TelegramSession.status == status, User.access_suspended_at.is_(None))
    )
    return list(result.scalars().all())


async def list_users(session: AsyncSession) -> list[User]:
    """Across users: the operator CLI (``scribe.admin list-users``) only."""
    result = await session.execute(select(User).order_by(User.created_at))
    return list(result.scalars().all())
