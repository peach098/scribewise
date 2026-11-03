# Internal Interfaces: Mark Chats Unread After Agent Activity

Extends `specs/001-telegram-reply-agent/contracts/internal-interfaces.md`. Only additions and
changed call sites are listed.

## MessagingGateway (`telegram/gateway.py`)

```python
@dataclass(frozen=True)
class UnreadMarkClearedEvent:
    """The user's other session removed the chat's unread mark (opened it / "Mark as read")."""

    user_id: uuid.UUID
    chat_id: int


UnreadMarkClearedHandler = Callable[[UnreadMarkClearedEvent], Awaitable[None]]


class MessagingGateway(Protocol):
    ...

    async def mark_unread(self, user_id: uuid.UUID, chat_id: int) -> None:
        """Set the chat's unread mark (messages.markDialogUnread, unread=True).

        Never sleeps through a flood wait: raises ``GatewayBusy`` at once. Raises
        ``GatewayNotStarted`` / ``SessionRevoked`` like the other operations. Never touches
        read state and never removes a mark. Called only by ``UnreadMarker``.
        """

    def on_unread_mark_cleared(self, handler: UnreadMarkClearedHandler) -> None: ...
```

Rules added to the module docstring:

- `mark_unread` is called exclusively by `telegram/unread_mark.UnreadMarker`.
- Receiving `UpdateDialogUnreadMark` never marks or reads anything; only `unread=False` on a
  plain `DialogPeer` without `saved_peer_id` is forwarded.

### `UserClient` (`telegram/user_client.py`)

- `mark_unread(chat_id)`: `_resolve(chat_id)` then `_no_sleep(MarkDialogUnreadRequest(peer=
  InputDialogPeer(peer=entity), unread=True))`.
- `start()`: the `events.Raw` handler also listens to `types.UpdateDialogUnreadMark`.
- Pure helper `unread_mark_cleared_from_update(user_id, update) -> UnreadMarkClearedEvent | None`
  (unit-tested without Telegram), next to `read_event_from_update`.
- New constructor argument `on_unread_mark_cleared` (handler), dispatched by `_handle_raw`.

### `ClientPool` / `FakeMessagingGateway`

- `ClientPool.mark_unread` → `_call(user_id, lambda c: c.mark_unread(chat_id))`;
  `on_unread_mark_cleared` appends to a handler list fanned out like the read handlers.
- `FakeMessagingGateway`: `mark_unread_calls: list[tuple[UUID, int]]`,
  `fail_mark_unread_with: Exception | None`, `mark_unread_delay_s: float`,
  `emit_unread_mark_cleared(event)`; `reset_calls()` clears `mark_unread_calls`.

## UnreadMarker (`telegram/unread_mark.py`, new)

```python
@dataclass(frozen=True)
class MarkTicket:
    user_id: uuid.UUID
    work_chat_id: uuid.UUID
    telegram_chat_id: int
    up_to: int


class UnreadMarker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        gateway: MessagingGateway,
        *,
        timeout_s: float = 5.0,
    ) -> None: ...

    async def prepare(
        self, user_id: uuid.UUID, telegram_chat_id: int, up_to: int
    ) -> MarkTicket | None:
        """Before the agent reads up to ``up_to``: a ticket if the setting is on, the chat is an
        enabled work chat and its floor is NULL or < up_to. Reads only; never raises (logs and
        returns None on any error)."""

    async def commit(self, ticket: MarkTicket | None) -> None:
        """After a successful read: move the floor to ``up_to`` if it is NULL or <= up_to, then
        mark the chat. No-op for None. Never raises."""

    async def after_send(
        self, user_id: uuid.UUID, work_chat_id: uuid.UUID, sent_message_id: int
    ) -> None:
        """After an approved reply was sent and committed: if the setting is on and the floor is
        NULL or < sent_message_id, move it and mark the chat. Never raises."""

    async def seen(
        self, user_id: uuid.UUID, telegram_chat_id: int, up_to: int | None, *, source: str
    ) -> None:
        """A seen signal from the user's own session: floor = max(floor, up_to). ``up_to=None``
        means "everything stored so far" (newest messages.telegram_message_id of the chat).
        Applies whatever the setting is. Never raises."""

    async def handle_unread_mark_cleared(self, event: UnreadMarkClearedEvent) -> None:
        """Gateway handler: ``seen(event.user_id, event.chat_id, None, source="mark_cleared")``."""
```

Guarantees:

- No method raises; every failure is logged (`unread_mark_failed`) and swallowed (FR-004).
- `gateway.mark_unread` is awaited with `asyncio.wait_for(..., timeout_s)`; no retry.
- The floor update and the mark happen outside any caller's transaction or row lock; the floor
  update is its own short transaction committed **before** `mark_unread` is called.
- Never reads, drafts or sends; never calls `mark_read` (FR-008).

## Changed call sites

| Where | Change |
|---|---|
| `scheduling/scheduler.py` `DueTurnsProcessor` | new optional `marker: UnreadMarker \| None`; in `_read`: `ticket = await marker.prepare(...)` before `gateway.mark_read`, `await marker.commit(ticket)` after it succeeds (not on the `< 1` early return, not on failure) |
| `telegram/control_bot/review.py` `_mark_turn_read` | same prepare / commit around its `gateway.mark_read` (Regenerate of an outdated draft); the marker comes from the dispatcher's workflow data |
| `sending/send.py` `send_approved_draft` | new optional `marker` keyword; after `session.commit()` and before commitment extraction: `await marker.after_send(user_id, work_chat_id, telegram_message_id)` |
| `telegram/control_bot/review.py` send callbacks | pass the marker to `send_approved_draft` |
| `intake/events.py` `ChatEventHandler` | new optional `marker`; `handle_read_elsewhere` calls `marker.seen(..., event.max_message_id, source="read_elsewhere")` for enabled work chats after its commit; `handle_outgoing` calls `marker.seen(..., event.message_id, source="manual_reply")` for a manual (non-agent) outgoing message after its commit |
| `__main__.py` | build one `UnreadMarker(session_factory, pool)`, pass it to `DueTurnsProcessor`, `ChatEventHandler`, the dispatcher (`create_dispatcher(..., unread_marker=...)`), and register `pool.on_unread_mark_cleared(marker.handle_unread_mark_cleared)` |

Default `None` everywhere keeps existing tests and callers unchanged.
