# Data Model: Work Account Mode (008)

Changes to the schema of `specs/001-telegram-reply-agent/data-model.md`. One Alembic revision
(next free number, research R10). All rows stay scoped by `user_id` and are removed by the
existing `ON DELETE CASCADE` from `users` (data deletion, 001 FR-026).

## New enum

| Type | Values | Notes |
|---|---|---|
| `work_chat_origin` | `picked`, `auto` | How a chat became a work chat. `picked`: chosen by the user (setup picker, spec 007 ➕). `auto`: picked up by work account mode. |

## Extended enums (values added in autocommit mode first, as in 0008)

| Type | New values | Used for |
|---|---|---|
| `activity_kind` | `work_account_on`, `work_account_off`, `chat_added`, `chat_excluded`, `chat_included` | `/history` (spec FR-017). `chat_added` is shared with spec 007 (meta `origin`). |
| `dropped_reason` | `work_account_off` | Turns/drafts dropped because the mode was turned off (spec FR-009). Exclusion keeps `chat_disabled`. |

## `users` (extended)

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `work_account` | boolean | no | `false` | Work account mode (spec FR-001). |
| `work_account_changed_at` | timestamptz | yes | — | Last switch; used by the startup reconcile (research R8) and shown in the data export. |

## `work_chats` (extended)

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `origin` | `work_chat_origin` | no | `'picked'` | Existing rows become `picked`. |
| `excluded` | boolean | no | `false` | The user's own "off" for an `auto` chat. Always `false` for `picked`. |

Constraint: `CHECK (origin = 'auto' OR NOT excluded)` (`work_chats_excluded_auto_only`).

Unchanged: `enabled` remains the **effective** flag read everywhere (research R1); unique
`(user_id, telegram_chat_id)` makes pick-up idempotent.

### Invariant (kept by `scribe/chats/work_account.py`)

```
origin = 'picked':  enabled = the user's choice (as today); excluded = false
origin = 'auto':    enabled = users.work_account AND NOT excluded
```

`reconcile_user` (research R8) re-establishes the `auto` line; tests assert it after every
writer.

### Derived values (not stored)

- **Excluded chats** (shown in `/settings`, `/chats`, spec FR-014/FR-015):
  `kind = 'private' AND ((origin = 'auto' AND excluded) OR (origin = 'picked' AND NOT enabled))`.
- **Paused picked-up chats** (mode off, `/chats` summary line):
  `origin = 'auto' AND NOT excluded AND NOT enabled`.

## State transitions of an `auto` work chat

| From | Event | To | Side effects | Activity |
|---|---|---|---|---|
| (no row) | first new message (in or out), mode on, eligible | active | message handled as in any work chat | `chat_added` |
| (no row) | style import with mode on finds own messages | active | contact gets a profile slot candidate | `chat_added` |
| (no row) | excluded in the exclusion picker | excluded | row holds id, title, kind only | `chat_excluded` |
| active | exclude | excluded | waiting turns + pending drafts dropped (`chat_disabled`), cards updated | `chat_excluded` |
| active | mode off | paused | waiting turns + pending drafts dropped (`work_account_off`), cards updated | `work_account_off` (once per switch) |
| paused | mode on | active | none | `work_account_on` (once per switch) |
| excluded | include again, mode on | active | next new message handled | `chat_included` |
| excluded | include again, mode off | paused | none | `chat_included` |
| excluded | mode on / off | excluded | none | — |

States: **active** `enabled=true, excluded=false`; **paused** `enabled=false, excluded=false`;
**excluded** `enabled=false, excluded=true`.

A `picked` private chat while the mode is on: "Exclude" = disable (`enabled=false`,
`chat_disabled`), "Include again" = enable; the mode switch never touches it (spec FR-008,
FR-009).

## Gateway DTOs (`src/scribe/telegram/gateway.py`, extended)

| Type | New field | Values |
|---|---|---|
| `IncomingMessage` | `peer_kind: PeerKind \| None = None` | `user`, `bot`, `self`, `service`, `deleted`; `None` for groups |
| `OutgoingEvent` | `chat_kind: DialogKind = "private"`, `peer_kind: PeerKind \| None = None`, `chat_title: str = ""` | as above; title for a newly picked-up chat |

Defaults keep existing call sites and fakes valid; defaults are conservative: a missing
`peer_kind` is **not** eligible.

## Settings (`src/scribe/config.py`)

| Setting | Default | Meaning |
|---|---|---|
| `work_account_import_max_chats` | `50` (`ge=0`) | Max private dialogs read by the style import on top of enabled work chats while the mode is on (spec FR-016, research R6). |

## FSM data

- Setup picker (`SetupStates.pick_chats`): existing `{dialogs, selected, page}` plus
  `work_account: bool`, `excluded: list[int]` (Telegram chat ids).
- Exclusion picker (`ChatsStates.exclude`, new): `{dialogs, excluded, page}`; only private
  eligible dialogs; held in the encrypted FSM storage like the setup picker.

## Retention and privacy

- An exclusion created before the chat ever wrote stores only `telegram_chat_id`, `title`,
  `kind`, `origin='auto'`, `excluded=true` — no messages, no contact row.
- Messages of picked-up chats follow the existing retention and encryption at rest.
- Excluding or pausing a chat does not delete stored messages (same as disabling today);
  `/delete_my_data` removes everything.
