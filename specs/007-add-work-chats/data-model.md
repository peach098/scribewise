# Data Model: Add Work Chats After Setup

This feature adds no new tables or columns. It adds one enum value and one repo function, and
it adds a *purpose* to the picker's FSM data.

## `activity_kind` enum (changed)

| Value | New | Meaning | `meta_json` |
|---|---|---|---|
| `chats_added` | yes | The user added work chats from `/chats` (➕). One record per Done that added ≥ 1 chat. | `{"work_chat_ids": ["<uuid>", …]}`. Ids only, never titles or content. |

- The record is written in the same transaction as the inserted `work_chats` rows, with
  `draft_id` and `conversation_turn_id` NULL and `at` set to the service clock.
- `/history` renders it with the current titles of those chats. Ids of chats that no longer
  exist are skipped. See [contracts/control-chat.md](contracts/control-chat.md).
- The record is covered by the existing `/my_data` activity count and export, and by
  `/delete_my_data` (cascade on `user_id`). Activity retention is unchanged.
- The setup picker writes no `chats_added` record. Setup is recorded as setup, and the
  behaviour there is unchanged.

Migration `0012_chats_added` (revises `0008`) runs `ALTER TYPE activity_kind ADD VALUE IF NOT
EXISTS 'chats_added'` inside `autocommit_block()`. The downgrade does nothing.

## `work_chats` (unchanged schema)

These existing columns and constraints are relied on:

| Column / constraint | Use in this feature |
|---|---|
| `UNIQUE (user_id, telegram_chat_id)` | Conflict target of the insert. At most one row per Telegram chat per user. |
| `enabled` | `true` for added chats. A row that already exists keeps its value: a disabled chat is never re-enabled by ➕. |
| `title`, `kind` | From the picker snapshot (`Dialog.title`, `Dialog.kind` → `ChatKind` via the setup mapping `_KIND`). |
| `group_mode` | Model default (`mentions_only`). Spec 006 makes it the only behaviour. |
| overrides, `skip_closing` | NULL (use the global settings), the same as setup. |

### New repo function

`chats_repo.add_work_chats(session, user_id, chats: Sequence[NewWorkChat]) -> list[WorkChat]`

- `NewWorkChat` holds `telegram_chat_id`, `title` and `kind`.
- It inserts every chat with `ON CONFLICT (user_id, telegram_chat_id) DO NOTHING RETURNING`
  and returns only the rows it created, so a double Done adds 0 the second time.
- It never updates existing rows. That is the difference from `upsert_work_chat`, which setup
  keeps using.
- Every query is scoped by `user_id`, as for all other repo functions.

## Chat picker FSM data (session state)

The picker's data is stored in the aiogram FSM storage (`bot_fsm_state`, one row per control
chat, deleted with the user). The setup and add pickers share one shape:

| Key | Type | Meaning |
|---|---|---|
| `purpose` | `"setup"` \| `"add"` | Which finisher Done calls, and which prompt is shown. |
| `dialogs` | `list[[chat_id: int, title: str, kind: str]]` | Snapshot from `list_dialogs`. In `add`, existing work chats are already removed. |
| `selected` | `list[int]` | The ticked Telegram chat ids. They must be in `dialogs`. |
| `page` | `int` | Current page, 8 per page. |
| `message_id` | `int` | The picker message. Callbacks from another message are stale. |

### FSM states

| State | Group | Entered by | Left by |
|---|---|---|---|
| `SetupStates.pick_chats` | setup (existing) | `show_chat_picker(purpose="setup")` after the Telegram connect | Done with ≥ 1 chat → `SetupStates.timezone`. `/cancel` → cleared (setup cancelled). |
| `AddChatsStates.picking` | new, `add_chats.py` | ➕ (`chatadd:0`) → `show_chat_picker(purpose="add")` | Done (any selection) → cleared. `/cancel` → cleared. Any other command → cleared by `AbandonedPickerMiddleware`, then handled. Data deletion → row gone. |

Validation:
- A toggle for a chat id that is not in `dialogs` is ignored.
- A page number is clamped to the valid range.
- A callback whose message id is not `message_id` answers "list closed".
- In `add` mode, Done with an empty selection is valid and adds nothing.

## Style data (unchanged schema)

The profile update after adding reuses the Rebuild path:
- `ImportResult`: in memory only.
- `contacts.own_message_count_6m`: set from the import.
- New `style_profiles` versions.
- The latest `profile_refreshed` record is superseded.

No new fields. Contacts for the new chats are created by `_contact_for_chat` during the build,
as for any enabled work chat.
