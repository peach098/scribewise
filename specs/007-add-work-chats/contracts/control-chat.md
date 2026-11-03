# Contract: Control Chat Changes for Adding Work Chats

This contract extends [the 001 control chat contract](../../001-telegram-reply-agent/contracts/control-chat.md).
It changes the `/chats` section and the setup picker, and adds `/history` text. Callback data
follows `<action>:<entity_id>[:<extra>]`, ≤ 64 bytes. The new `chatadd` action is not an
entity action, so `OwnershipMiddleware` lets it through: it acts on the caller's own rows only.

## `/chats` (changed)

```
💼 Your work chats. Tap one for its settings:
[⚙️ Anna]                 chatcfg:<work_chat_id>
[⚙️ Project X]            chatcfg:<work_chat_id>
[⏸ Old project]           chatcfg:<work_chat_id>
[➕ Add work chats]        chatadd:0
```

- An empty list is shown as `chats.none` with the same **➕ Add work chats** row.
- The ➕ row is shown only when the user has finished setup (`users.state != setting_up`).
  Otherwise `/chats` is unchanged. A `chatadd:` tap from such a user answers
  `common.not_set_up` ("Send /start first.").

## ➕ Add work chats (`chatadd:0`)

The checks run in order. The first one that fails answers and stops, with no state set and no
write.

| Condition | Answer |
|---|---|
| Telegram session missing or not `active` | `telegram.disconnected_start` + reconnect keyboard (the same as `/start` for a set-up user) |
| `list_dialogs` fails | `common.send_to_continue(problem = connect.telegram_unavailable, command = /chats)`: "I couldn't reach Telegram right now. Please try again in a minute. Send /chats to continue." |
| No dialog left after removing existing work chats | `chats.add_none_left` |

Otherwise the picker opens in a new message (state `AddChatsStates.picking`):

```
➕ Choose chats to add. I'll read only new messages in them, from now on.
Chats already in /chats aren't listed; turn a disabled (⏸) one back on from its settings there.
[⬜ Design team]          pickchat:<telegram_chat_id>
[✅ New project]          pickchat:<telegram_chat_id>
… (8 per page)
[◀️] pickpage:<n>   [▶️] pickpage:<n>
[Done (1 selected)]       pickdone:0
```

The rows, paging, truncation (40 characters) and the Done label are those of the setup picker
(`chat_picker.picker_keyboard`). Only the prompt differs (`chats.add_prompt`).

### Done (`pickdone:0`, purpose `add`)

- The selected chats that are not work chats yet are inserted as enabled work chats (no
  overrides). Existing rows are not touched, so a disabled chat stays disabled.
- If ≥ 1 chat was inserted, one `chats_added` activity record is written.
- The picker message is edited to `chats.added` (no buttons): "✅ Added {n} chat(s). I'm
  learning from your own messages in them in the background and will tell you when it's done."
- The `/chats` list follows as a new message.
- If nothing new was inserted (empty selection, or everything was added meanwhile), the picker
  message is edited to `chats.add_nothing` ("Nothing was added."). The state is cleared and no
  record is written.

### Profile update (after a Done that added ≥ 1 chat)

This is the same work as 🔄 Rebuild now (`profrebuild:1`):
- it re-imports the user's own messages of the last `style_import_months` in all enabled work
  chats;
- it rebuilds the global profile and the top contact profiles with synchronous LLM calls;
- the user's rules are kept, the removed-contact rule applies, and the latest refresh's ↩️ Undo
  ends.

Differences from the button:

- If a rebuild is running, it waits for it instead of answering "Already rebuilding".
- There is no "⏳ Reading your own messages…" message, because the confirmation already says it.
- The ending message is exactly one of:
  - **Built**: the Rebuild result note (`profile.rebuilt`; after PR #31, `rebuild_result_text`),
    with the weak-profile line when it applies.
  - **Nothing to learn**: the added chats have 0 own messages in the import window. The user
    gets `chats.add_nothing_to_learn`: "ℹ️ None of your own messages from the last 6 months are
    in the new chats, so your style profiles stay as they are." No profile is built.
  - **Failed**: `chats.add_learn_failed`: "⚠️ The chats are added, but I couldn't learn from your
    messages in them right now. Your profiles are unchanged; try 🔄 Rebuild in /profile later."
  - **Account deleted meanwhile**: no message.

### Leaving the picker

- `/cancel` → `chats.add_cancelled` ("Nothing was added."). The picker's buttons are removed
  (best effort) and the state is cleared.
- Any other command (`/settings`, `/help`, `/start`, …) → the picker is closed silently and the
  command is answered as usual.
- Free text → `chats.add_hint` ("Tap chats to select them, then Done. /cancel to stop.").
- A `pickchat:` / `pickpage:` / `pickdone:` tap on a message that is not the open picker, or
  when no picker is open → callback alert `alert.picker_closed` ("This list is closed. Open
  /chats again."). Nothing changes. This also applies to stale setup picker buttons.

## Setup picker (unchanged behaviour)

The setup picker uses the same `chat_picker` module with purpose `setup`:
- the prompt is `setup.pick_prompt`;
- all dialogs are listed;
- Done with nothing selected answers `setup.pick_none`;
- Done upserts the chats, shows `setup.chats_saved` and moves to the time zone step;
- `/cancel` keeps the setup text.

## `/history` (added line)

```
Fri 23 Oct 14:05 · ➕ Work chats added · Project X, Anna
Fri 23 Oct 14:10 · ➕ Work chats added · Design, Ops, Finance, +2 more
```

The titles are the current titles of the added chats, at most 3 of them, followed by
`history.more` ("+{n} more"). Chats deleted since then are left out. The line never shows
message content.

## New i18n keys (en and ru)

- `btn.chat_add`
- `chats.add_prompt`
- `chats.add_none_left`
- `chats.added` (plural forms one/other)
- `chats.add_nothing`
- `chats.add_cancelled`
- `chats.add_hint`
- `chats.add_nothing_to_learn`
- `chats.add_learn_failed`
- `alert.picker_closed`
- `history.kind.chats_added`
- `history.more`

The Russian texts follow the tone of the existing `chats.*` keys. Every key must exist in both
languages: the `test_i18n_lint.py` parity check enforces it.

## Changes to the 001 documents

- 001 `contracts/control-chat.md`:
  - Commands table: `/chats` gets the ➕ button and the add flow.
  - "Settings" section: the `/chats` bullet links here.
  - `/history` list: gains "➕ Work chats added".
- 001 `spec.md`:
  - FR-024 gains one sentence: users can add work chats after setup from `/chats`, through the
    same picker as setup, and the new chats' own messages are learned as on Rebuild (spec 007).
  - FR-022 gains a note that chats are chosen at setup and can be added later.
