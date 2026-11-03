# Contract: Internal Interfaces — Work Account Mode (008)

Extends `specs/001-telegram-reply-agent/contracts/internal-interfaces.md`.

## Gateway (`src/scribe/telegram/gateway.py`)

```python
PeerKind = Literal["user", "bot", "self", "service", "deleted"]


@dataclass(frozen=True)
class IncomingMessage:
    ...  # unchanged fields
    peer_kind: PeerKind | None = None  # private chats only; None for groups


@dataclass(frozen=True)
class OutgoingEvent:
    ...  # unchanged fields
    chat_kind: DialogKind = "private"
    peer_kind: PeerKind | None = None
    chat_title: str = ""
```

- `TelethonUserClient` (`user_client.py`) fills `peer_kind` from the private chat's `User`
  entity: `bot` → `"bot"`, `is_self` → `"self"`, `deleted` → `"deleted"`, `support` or id
  `777000` → `"service"`, else `"user"`. For outgoing messages it reads `event.is_private`
  and `event.get_chat()` (entity cache).
- `dialog_kind()` returns `None` for deleted and service accounts too (they are no longer
  offered by `list_dialogs`).
- `FakeMessagingGateway` accepts the new fields; default `peer_kind=None` is **not** eligible,
  so tests must opt in.

## `scribe/chats/work_account.py` (new)

```python
def is_eligible(chat_kind: DialogKind, peer_kind: PeerKind | None) -> bool
    """Pure. True only for chat_kind == "private" and peer_kind == "user"."""

async def pick_up(
    session: AsyncSession, user_id: UUID, *, telegram_chat_id: int, title: str,
    chat_kind: DialogKind, peer_kind: PeerKind | None, direction: Literal["in", "out"],
    now: datetime,
) -> WorkChat | None
    """Called by intake / outgoing handling when no work_chats row exists. Reads the user row
    FOR SHARE; returns a new enabled `auto` row (and records `chat_added`) iff the mode is on
    and the chat is eligible; else None. Idempotent under the per-chat lock (unique
    (user_id, telegram_chat_id)); never raises for ineligible chats."""

async def switch(
    session_factory, user_id: UUID, *, on: bool, now: datetime,
) -> SwitchResult            # (changed: bool, paused_chats: int, dropped_draft_ids: list[UUID],
                             #  dropped_waiting: int)
    """Sets users.work_account (+ changed_at) under FOR UPDATE and commits; then re-enables
    (on) or deactivates (off, reason work_account_off) the `auto` chats, one chat per
    transaction, using the disable lock order (chat → pending drafts → turns). No-op with
    changed=False when already in that state. Records work_account_on/off once."""

async def preview_off(session, user_id: UUID) -> OffPreview   # (chats, pending_drafts, waiting)

async def exclude_chat(
    session_factory, user_id: UUID, *, work_chat_id: UUID | None = None,
    dialog: Dialog | None = None, now: datetime,
) -> list[UUID]              # dropped pending draft ids
    """auto → excluded=True, enabled=False; picked → enabled=False (same as disable).
    With `dialog` and no row: creates an `auto` excluded row (no contact, no messages).
    Drops turns/drafts with reason chat_disabled. Records chat_excluded."""

async def include_chat(session_factory, user_id: UUID, work_chat_id: UUID, *, now) -> None
    """auto → excluded=False, enabled=users.work_account; picked → enabled=True.
    Records chat_included."""

async def import_targets(
    session, gateway, user_id: UUID, *, max_auto: int,
) -> list[ImportTarget]      # (telegram_chat_id, title, kind, work_chat_id | None)
    """Enabled work chats, plus — when the mode is on — up to max_auto most recent eligible
    private dialogs that have no row or an enabled row. Never excluded or unpicked groups."""

async def materialise_imported(session, user_id, imported: ImportResult, targets, *, now)
    """Creates `auto` enabled rows (chat_added) for imported dialogs with ≥1 own message and
    no row, before build_profiles_from_import runs."""

async def reconcile_user(session_factory, user_id: UUID, *, now) -> int
    """Re-applies the `auto` invariant (research R8); returns rows fixed."""

async def excluded_count(session, user_id: UUID) -> int
async def paused_count(session, user_id: UUID) -> int
```

## Settings disable protocol (`control_bot/settings.py`)

`_disable_chat_once` is generalised to `deactivate_chat(session_factory, user_id,
work_chat_id, now, *, reason: DroppedReason, set_excluded: bool | None)` and moved to
`scribe/chats/work_account.py` (or `scribe/chats/deactivate.py`); `disable_chat` keeps its
signature and calls it with `reason=chat_disabled`. Lock order and retry on a draft appearing
are unchanged.

## Intake (`intake/router.py`) and outgoing (`intake/events.py`)

- `IntakeRouter._handle`: when `get_work_chat_by_telegram_id` returns `None` and the message is
  not outgoing, call `work_account.pick_up(..., direction="in")`; on a row continue the
  existing path (the pure `decide()` is unchanged); on `None` → `Action.ignore` as today.
- `OutgoingHandler` (`events.py`, both `chat is None` sites): same with `direction="out"`;
  the manual-reply logic then runs on the new, empty chat (nothing to drop) and stores the
  outgoing message as context.
- The per-chat lock (`intake/locks.py`) already serialises incoming and outgoing messages of
  one chat, so two first messages create one row.

## Style (`style/importer.py`, setup, `/profile` Rebuild)

`import_own_messages(..., targets: list[ImportTarget] | None = None)`: when `None`, behaves as
today (enabled work chats). Setup's `_import_and_finish` and `/profile` Rebuild pass
`await work_account.import_targets(...)` and call `materialise_imported` before building
profiles. The import never reads other people's messages (unchanged
`fetch_own_history(from_user="me")`).

## Logging (no content, no titles)

| Event | Fields |
|---|---|
| `work_account_switched` | user_id, on, paused_chats, dropped_drafts, dropped_waiting |
| `work_chat_picked_up` | user_id, work_chat_id, direction |
| `work_chat_excluded` / `work_chat_included` | user_id, work_chat_id, origin |
| `work_account_ineligible` (debug) | user_id, peer_kind |
| `work_account_reconciled` | user_id, fixed |
| `style_import_finished` (existing) | + auto_targets |
