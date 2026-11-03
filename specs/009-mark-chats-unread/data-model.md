# Data Model: Mark Chats Unread After Agent Activity

**Feature**: `specs/009-mark-chats-unread` | **Migration**: `0009_mark_unread` (revises `0008`)

Two new columns, no new table, no new enum value, no message content.

## `users.mark_unread` (new)

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `mark_unread` | `BOOLEAN` | no | `true` (server default, so existing users are on) | FR-009: mark work chats unread after the agent reads or sends there |

- Toggled only by the user in `/settings` (`settings:mark_unread`).
- Read by `UnreadMarker.prepare` / `after_send` at the moment of each read or send. Turning it
  off or on has no other effect (FR-010): nothing is backfilled, nothing is cleared.
- Exported by `/my_data` (all `users` columns already are) and deleted with the user.

## `work_chats.unread_mark_floor` (new)

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `unread_mark_floor` | `BIGINT` | yes | `NULL` | FR-005: newest Telegram message id in this chat that the agent marked the chat for or the user is known to have seen. `NULL` = neither has happened yet |

- Compared only with Telegram message ids of the same chat (`messages.telegram_message_id`,
  `drafts.sent_telegram_message_id`). It never decreases.
- `CHECK (unread_mark_floor IS NULL OR unread_mark_floor > 0)`.
- Deleted with the work chat (existing cascade from `users`).

### Transitions

```text
                 prepare(n): floor IS NULL or floor < n  -> ticket (no write)
  agent read  -> commit(ticket): UPDATE SET floor = n WHERE floor IS NULL OR floor <= n
                   updated -> gateway.mark_unread  (log unread_mark_set / unread_mark_failed)
                   not updated -> log unread_mark_skipped(reason=seen)
  agent send s -> UPDATE SET floor = s WHERE floor IS NULL OR floor < s
                   updated -> gateway.mark_unread; else skipped(reason=not_newer)
  seen up to m -> UPDATE SET floor = GREATEST(COALESCE(floor, 0), m)
                   (read elsewhere: m = max_message_id; mark cleared: m = newest stored message
                    of the chat; manual outgoing: m = its message id)
```

Setting off: `prepare` and `after_send` return before any write (`reason=disabled`); seen
signals still move the floor, so turning the setting back on cannot mark already-seen messages.

## Gateway value objects (in memory, `telegram/gateway.py`)

- `UnreadMarkClearedEvent(user_id: UUID, chat_id: int)`: the user's other session cleared the
  chat's unread mark (`UpdateDialogUnreadMark`, `unread=False`, a plain `DialogPeer`, no
  `saved_peer_id`).
- `MarkTicket(user_id: UUID, work_chat_id: UUID, telegram_chat_id: int, up_to: int)`: returned by
  `UnreadMarker.prepare`, consumed by `commit`. Never stored.

## Logs (structured, no content; FR-012)

| Event | Fields |
|---|---|
| `unread_mark_set` | `user_id`, `work_chat_id`, `cause` (`read` / `send`), `up_to` |
| `unread_mark_skipped` | `user_id`, `work_chat_id`, `cause`, `reason` (`disabled` / `seen` / `not_newer` / `not_work_chat`) |
| `unread_mark_failed` | `user_id`, `work_chat_id`, `cause`, `error` (exception type only) |
| `unread_mark_seen` | `user_id`, `work_chat_id`, `source` (`read_elsewhere` / `mark_cleared` / `manual_reply`), `up_to` |
