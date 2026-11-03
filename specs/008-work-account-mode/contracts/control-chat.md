# Contract: Control Chat — Work Account Mode (008)

Extends `specs/001-telegram-reply-agent/contracts/control-chat.md` (setup FSM, `/settings`,
`/chats`, `/history`, `/my_data`). Plain text, no parse mode, in the user's language (en, ru;
every key below exists in both `i18n/en.py` and `i18n/ru.py`, checked by the i18n lint test).
Callbacks act only on the caller's own rows (`OwnershipMiddleware` + scoped loads). Texts here
are English reference wording; Russian is translated, not literal.

## Setup: `pick_chats` step

```
Pick your work chats. I will only read new messages in them.

💼 Work account: off — tap to treat every private chat as a work chat       [pickwa:on]
⬜ Anna Petrova            [pickchat:<id>]
⬜ Backend team (group)    [pickchat:<id>]
…
◀️ ▶️                       [pickpage:<n>]
Done (2)                    [pickdone:0]
```

With the mode on (`pickwa:on` pressed):

```
💼 Work account: on. Every private chat with a person is a work chat, including people who
write for the first time later. Tap a chat to exclude it. Groups: tick the ones you want.

💼 Work account: on — tap to turn off                     [pickwa:off]
✅ Anna Petrova                                            [pickchat:<id>]   tap → 🚫
🚫 Mum                                                     [pickchat:<id>]   tap → ✅
⬜ Backend team (group)                                    [pickchat:<id>]
Done (all private chats, 1 excluded, 0 groups)             [pickdone:0]
```

Rules:
- `pickchat` on a private dialog toggles `excluded` while the mode is on, `selected` while off.
  Groups always toggle `selected`.
- Done with the mode on is allowed with nothing selected. With the mode off the existing
  "pick at least one chat" alert stays.
- On Done: `users.work_account` is saved (with `work_account_changed_at`); selected dialogs are
  upserted as `picked` enabled chats; excluded private dialogs are upserted as `auto`,
  `excluded=true`, `enabled=false`. Activity: `work_account_on` (if on), `chat_excluded` per
  exclusion.
- Confirmation (`setup.chats_saved_work_account`): "💼 Work account mode is on: every private
  chat is a work chat. Excluded: {n}. Groups: {g}."

## `/settings`

New line after "Search earlier conversations":

```
💼 Work account: on — every private chat is a work chat, 3 excluded
💼 Work account: off — only the chats you chose
```

New button row: `💼 Work account: turn on` / `💼 Work account: turn off`
(`settings:work_account`).

### Turn-on confirmation (`settings.work_account_confirm_on`)

```
Turn on work account mode?

• New messages from every private chat with a person will be read and stored as work
  messages, including people who write to you for the first time.
• Groups, channels and bots are not included. Add groups in /chats.
• You can exclude any chat in /chats, and turn this off any time.
• Old messages are not drafted. Your own recent messages in up to {n} recent chats are used to
  learn your style at the next profile rebuild.

[✅ Turn on  wacct:on]  [Cancel  wacct:cancel]
```

On `wacct:on`: mode on, `auto` non-excluded chats re-enabled, `work_account_on` recorded,
answer "💼 Work account mode is on." and the refreshed `/settings` text.

### Turn-off confirmation (`settings.work_account_confirm_off`)

```
Turn off work account mode?

Only the chats you chose yourself will be handled. {m} chats picked up by this mode will be
paused; {d} pending drafts and {w} waiting messages in them will be dropped. Your exclusions are
kept if you turn it on again.

[⏸ Turn off  wacct:off]  [Cancel  wacct:cancel]
```

On `wacct:off`: mode off, sweep (research R4), each dropped draft card gets status
`status.work_account_off` ("⏸ Work account mode off — dropped"); answer "Work account mode is
off. {m} chats paused." `wacct:*` when the mode is already in the requested state: show the
current state, change nothing (spec edge case "old buttons").

## `/chats`

```
💼 Work account: on — every private chat is a work chat. Excluded: 3.
Your work chats:
⚙️ Anna Petrova · 💼            [chatcfg:<work_chat_id>]
⚙️ Backend team                [chatcfg:<work_chat_id>]
🚫 Mum                          [chatcfg:<work_chat_id>]
◀️ 1/3 ▶️                      [chatspage:<n>]
🚫 Exclude chats…               [wxopen:0]          (only while the mode is on)
```

- Marks: ⚙️ enabled, ⏸ disabled (picked), 🚫 excluded (mode on), `· 💼` suffix = picked up by
  the mode. Order: enabled first, then by title. 8 per page.
- Mode off: no header line; `auto` chats hidden; if any are paused, a footer line
  "💼 {m} chats picked up by work account mode are paused. Turn it on in /settings."
- Empty list with the mode on: the header line and the exclude button are shown instead of
  `chats.none`.
- Spec 007's ➕ button, when present, sits under the list; with the mode on its picker shows
  only groups (private chats are covered).

### Exclusion picker (`wxopen:0` → `ChatsStates.exclude`)

```
Tap a chat to exclude it. Excluded chats are never read.
✅ Anna Petrova     [wxpick:<telegram_chat_id>]
🚫 Mum              [wxpick:<telegram_chat_id>]
◀️ ▶️               [wxpage:<n>]
Done                [wxdone:0]
```

- Lists eligible private dialogs from `gateway.list_dialogs` (bots, Saved Messages, service and
  deleted accounts already left out), current state from `work_chats` rows.
- On Done: newly excluded → `exclude_chat` (creates an `auto` excluded row when none exists);
  newly included → `include_chat`. One summary answer: "Excluded {a}, included again {b}."
- Telegram unavailable: same text as setup (`connect.telegram_unavailable`), nothing changes.
- `/cancel` closes the picker without changes.

### Chat settings screen (private chat, mode on)

The enable/disable row reads `🚫 Exclude` (`chatenable:<id>:off`) or `↩️ Include again`
(`chatenable:<id>:on`). The status line reads "Included by work account mode",
"Chosen by you", or "Excluded". Other rows unchanged.

## `/history`

| Kind | en label | Contact column |
|---|---|---|
| `work_account_on` | 💼 Work account mode on | — |
| `work_account_off` | 💼 Work account mode off | — |
| `chat_added` (origin auto) | ➕ New work chat (work account) | chat title |
| `chat_excluded` | 🚫 Chat excluded | chat title |
| `chat_included` | ↩️ Chat included again | chat title |

The chat title is looked up by `meta.work_chat_id` at display time.

## `/my_data`

Summary gains: "Work account mode: on/off (since {date}) · picked-up chats: {n} · excluded:
{x}". Export gains `work_account`, `work_account_changed_at`, and per work chat `origin`,
`excluded`.
