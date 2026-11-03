# Research: Mark Chats Unread After Agent Activity

**Feature**: `specs/009-mark-chats-unread` | **Date**: 2026-10-23

Grounded in the code on `main` at `2e8a82e` and Telethon 1.45.0 (the version in `uv.lock`).

## U1. How to mark a chat unread from a user account

**Decision**: `functions.messages.MarkDialogUnreadRequest(peer=types.InputDialogPeer(peer=<input
peer>), unread=True)` sent by `UserClient.mark_unread(chat_id)`. The input peer comes from the
existing `UserClient._resolve(chat_id)`. The call returns `Bool`; `True` and an already-marked
chat are both success.

**Rationale**: this is the MTProto method behind "Mark as unread" in the official apps. It is
available in Telethon 1.45.0 (checked: `MarkDialogUnreadRequest(peer: TypeInputDialogPeer,
unread: bool | None, parent_peer: TypeInputPeer | None)`). It changes only the dialog's
`unread_mark` flag, account-wide, on every device. It does not touch read state, so it cannot
un-read the messages the agent read (the colleague keeps seeing read ticks).

**Alternatives considered**:
- Leaving messages unread and only drafting (do not call `readHistory`): contradicts 001 US2,
  where reading after the delay is the visible signal to the colleague.
- `parent_peer` (forum topics / saved sub-dialogs): not needed; the spec marks the whole chat.
  Left `None`.

## U2. Does reading or sending clear the mark?

**Decision**: assume both can. The agent always marks **after** its read and **after** its send,
never before. Verified manually in `quickstart.md` (scenarios Q1 and Q2).

**Rationale**: official clients clear the mark when a chat is read, and sending a message from a
client normally opens the chat first. Whether the server itself resets `unread_mark` on
`messages.readHistory` / `messages.sendMessage` is not documented. Ordering "act, then mark"
gives the right result either way: if the server does not clear it, the second mark (after a
send) is a no-op.

**Alternatives considered**: marking once before reading. Rejected: a server-side reset on read
would remove it at once.

## U3. How the user's "seen" signals arrive

**Decision**: three signals, all handled after the existing handlers:

| Signal | Telegram update / gateway event | Seen up to |
|---|---|---|
| The user read the chat on another device | `UpdateReadHistoryInbox` / `UpdateReadChannelInbox` → existing `ReadEvent` → `ChatEventHandler.handle_read_elsewhere` | `event.max_message_id` |
| The user opened a chat that had only the mark (nothing unread) or used "Mark as read" | `UpdateDialogUnreadMark(peer=DialogPeer, unread=False)` → **new** `UnreadMarkClearedEvent` | newest stored message of that work chat (`messages.telegram_message_id`, incoming or outgoing) |
| The user sent a message by hand | `NewMessage(out=True)` not matched to an agent send → existing manual-reply branch of `handle_outgoing` | `event.message_id` |

**Rationale**: after the agent has read everything, opening the chat on a phone has nothing to
read, so the phone sends `messages.markDialogUnread(unread=false)` and **no** `readHistory`. The
existing `ReadEvent` alone would miss that case; `UpdateDialogUnreadMark` is the reliable
signal. `UpdateDialogUnreadMark` with `unread=True` (our own mark, or the user marking by hand)
is ignored. A `DialogPeerFolder` peer or a set `saved_peer_id` (Saved Messages sub-dialogs) is
ignored too. Telethon delivers the update through `events.Raw`, the same way the read updates
already arrive (`UserClient.start` registers `_handle_raw`).

**Alternatives considered**: polling `messages.getDialogUnreadMarks` / dialog state. Rejected:
extra requests, delay, and still no way to tell when the user looked.

## U4. Telling the user's read from the agent's own read (loop avoidance)

**Decision**: per work chat, one number `work_chats.unread_mark_floor`: the newest Telegram
message id the chat was marked for or the user is known to have seen. The rules
(`UnreadMarker`, contracts/internal-interfaces.md):

1. **Before** the agent reads up to message `n`: `prepare(n)` returns a ticket only if the
   setting is on and `floor` is null or `< n`. No write.
2. The agent reads (`mark_read`). If the read fails, nothing else happens (the turn is retried
   and step 1 runs again).
3. After a successful read: `commit(ticket)` runs `UPDATE work_chats SET unread_mark_floor = n
   WHERE id = … AND (unread_mark_floor IS NULL OR unread_mark_floor <= n)`. If a row was
   updated, the chat is marked (`gateway.mark_unread`).
4. After an approved send with message id `s`: the same conditional update with `<` (strict) and
   then the mark.
5. A seen signal up to `m`: `UPDATE … SET unread_mark_floor = GREATEST(COALESCE(floor, 0), m)`.

**Rationale**:
- The decision in step 1 is taken before the agent's own read, so if Telegram ever echoes the
  agent's read back as `UpdateReadHistoryInbox`, the echo (seen up to `n`) cannot cancel the
  mark: step 3 accepts `floor == n`. A real read by the user that went *further* than `n`
  (`floor > n`) does cancel it.
- The user read the chat while a message was still waiting out its delay → `floor >= n` before
  step 1 → no mark (spec US3 scenario 2).
- Retries, restarts (`requeue_interrupted_turns`), "Regenerate" re-reads: same `n`, `floor == n`
  already → step 1 says no → no second mark (US3 scenario 4).
- The mark never triggers a read: reads only come from due turns (001 FR-016). So there is no
  read → mark → read loop (FR-008).
- One column survives restarts; no in-memory echo table and no timing window.
- Telegram message ids grow monotonically within a chat (per account for private chats and
  basic groups, per channel for supergroups), and incoming and outgoing messages share the
  sequence, so one comparison covers reads and sends.

**Alternatives considered**:
- Echo filter in `UserClient` (remember the agent's own `max_id` per chat for N seconds and drop
  matching `ReadEvent`s). Rejected as the primary mechanism: timing-based, in memory, lost on
  restart; step 1 makes it unnecessary.
- Two columns (marked-up-to and seen-up-to). Rejected: the decision only needs the maximum; the
  structured log records which one moved it.
- Storing a "mark is on" boolean mirrored from Telegram. Rejected: it goes stale whenever the
  user's app clears the mark offline; the floor never needs to know the mark's current state.

## U5. Does the agent's own mark come back as an update?

**Decision**: irrelevant by design. The only update the mark can produce is
`UpdateDialogUnreadMark(unread=True)`, which the gateway ignores. The agent never sends
`unread=False`, so every `unread=False` update comes from the user's own sessions.

## U6. Flood waits and latency

**Decision**: `mark_unread` uses the search path's no-sleep behavior (`UserClient._no_sleep`):
a known or new flood wait raises `GatewayBusy` at once; the call is bounded by
`asyncio.wait_for(…, 5 s)`. `UnreadMarker` catches every exception, logs `unread_mark_failed`
and returns. No retry (FR-004).

**Rationale**: the read path sleeps through flood waits up to 60 s (`_guarded`); the mark must
not hold a read-semaphore slot that long (SC-004). A late retry could set the mark after the
user has already looked.

**Alternatives considered**: a background task per mark. Rejected: harder to test, and the
5 s bound already keeps the read path fast.

## U7. Where the read and send call sites are

- `scheduling/scheduler.py` `DueTurnsProcessor._read`: the only regular read (after the delay,
  "Read now", read elsewhere making a turn due). Gets `prepare` before and `commit` after
  `gateway.mark_read`.
- `telegram/control_bot/review.py` `_mark_turn_read`: reads before "Regenerate" of an outdated
  draft. Same treatment (FR-001).
- `sending/send.py` `send_approved_draft`: after the transaction commits (the draft row lock is
  released), call `marker.after_send(user_id, work_chat_id, telegram_message_id)`. Never inside
  the lock: a slow mark must not hold the draft row.
- Several turns of one chat read in one batch (AC US1-3): each reads up to its own last message
  (`readable_up_to`). The conditional update lets each newer one through; two marks in a row are
  idempotent in Telegram. Grouping reads per chat in the scheduler is not worth the change.

## U8. Setting storage and `/settings`

**Decision**: `users.mark_unread BOOLEAN NOT NULL DEFAULT true`, toggled by
`settings:mark_unread` exactly like `settings:search_earlier` (specs/004 S9): each press flips
the value and re-renders the `/settings` message. `/my_data` export already dumps every `users`
column (`retention/user_data.export_user_data`), and deletion cascades, so FR-011 needs only a
test. The floor column lives on `work_chats` and goes with the chat row.

## U9. Migration numbering

**Decision**: `alembic/versions/0009_mark_unread.py`, `down_revision = "0008"`.
`origin/006-group-mention-only` adds no migration yet; spec 008 (issue #15) may. Whichever
merges second renumbers, as 0008 did (see its docstring).

## U10. Agent context file

The plan template does not call `.specify/scripts/bash/update-agent-context.sh`, and the script
is not present in `.specify/scripts/bash/`. Skipped; nothing outside the feature directory is
changed by planning.
