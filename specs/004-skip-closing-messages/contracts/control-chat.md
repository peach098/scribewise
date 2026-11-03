# Contract: Control Chat Additions

Extends [001 control chat contract](../../001-telegram-reply-agent/contracts/control-chat.md).
Callback data keeps the 001 format `<action>:<entity_id>[:<extra>]`, at most 64 bytes.
Every text below is shown in the user's language: the English wording is quoted here; each
string is a `scribe.i18n` key with an English and a Russian entry (`src/scribe/i18n/en.py`,
`ru.py`).

## Commands

| Command | Purpose | Spec |
|---|---|---|
| `/skipped` | Messages skipped in the last 7 days, with **Draft anyway** and per-chat toggle; header shows and toggles the global setting | FR-012, FR-013, FR-014 |

`"skipped"` is in `render.COMMAND_NAMES` after `waiting` (catalog key `cmd.skipped`: "Messages
I didn't draft for (thanks, bye…)" / "Сообщения без черновика (спасибо, пока…)"), so `/help`
and the `/` menu list it in both languages. The command answers only when no other flow is
active (the settings router's filter).

## Skipped list (`/skipped`)

```
💤 Skipped in the last 7 days — 3          Skipping: ON
[ Turn skipping off ]                       ← skipcfg:global:off  (or "Turn skipping on" → :on)
[ ⚙️ Per-chat settings ]                     ← skipchats:0

1. Anna Testova · Design team · Tue 14:02
   “thanks, that helps!”
   Reason: thanks
   [ ✍️ Draft anyway ]  [ Always draft in this chat ]
2. …
[ ◀ ]  [ ▶ ]                                ← skippage:<page>:<n>
```

- 10 items per page, newest first. Times are in the user's time zone.
- The preview is the first 80 characters of the turn text, in quotes. It is decrypted only
  for this render, never logged, and never sent anywhere else.
- **✍️ Draft anyway** (`draftany:<turn_id>`) is shown only while the turn is available (see
  [data-model.md](../data-model.md) "Derived values"). Otherwise the item shows "— you
  already replied".
- **Always draft in this chat** (`skipchat:<work_chat_id>:off`) is shown only when the chat
  has no override. When the chat override is `off`, the item shows **Use global setting**
  (`skipchat:<work_chat_id>:inherit`).
- Empty list: "Nothing skipped in the last 7 days. Skipping is ON." The list still shows the
  global toggle and **⚙️ Per-chat settings**.

## Per-chat skip settings (`skipchats:<page>`)

```
⚙️ Skip thanks/bye per chat            Global: ON
[ Turn skipping off ]                   ← skipcfg:global:off (or :on)

Design team        ✓ Global · Always draft · Skip
Anna Testova         Global · ✓ Always draft · Skip
…
[ ◀ ]  [ ▶ ]                            ← skipchats:<page>
[ ‹ Back to skipped ]                   ← skippage:0:10
```

- Lists the caller's enabled work chats, ordered by title, 10 per page, including chats that
  have never had a skip.
- Each row has three buttons: **Global** (`skipchat:<work_chat_id>:inherit`), **Always
  draft** (`skipchat:<work_chat_id>:off`) and **Skip** (`skipchat:<work_chat_id>:on`). The
  current value is marked ✓.
- This is the only way to set a chat to **Skip** while the global setting is off.

## Callbacks

| Callback | Effect | Reply |
|---|---|---|
| `draftany:<turn_id>` | Check ownership and availability again; guarded update `skipped → waiting`, `due_at = now`, `draft_anyway_at = now`, `decision_source = user`; activity `draft_anyway` | toast "Drafting…"; item edited to "✍️ Drafting…"; the draft card arrives as usual |
| `skipcfg:global:on` / `:off` | `users.skip_closing` | the originating view (list header or per-chat view) re-rendered |
| `skipchat:<work_chat_id>:off` / `:on` / `:inherit` | `work_chats.skip_closing` = false / true / null | item or row re-rendered |
| `skippage:<page>:<n>` | paging | list edited in place |
| `skipchats:<page>` | open or page the per-chat settings view | view edited in place |

- A turn that is no longer available (the user replied, or it was already drafted) gets
  the toast "Already handled".
- A foreign or unknown id gets the toast "Not found" and a warning log line
  (`callback_refused_not_owner` from `ownership.OwnershipMiddleware`, which knows `draftany`
  and `skipchat`; the handlers also load every row scoped to the user).
- `skipcfg` / `skipchat` re-render the view they came from: the per-chat view when the pressed
  message starts with its title (in either language), else the list.

## Draft card additions

- A draft whose `system_warnings` contains `drafted_by_habit` shows the line
  "✍️ Drafted: you usually reply to messages like this here".
- A pending draft with `possibly_unnecessary = true` shows the banner
  "💤 {sender}: this may no longer be needed" at the top (`{sender}` is the card's contact).
  All buttons stay. A closing message alone no longer adds the "⚠️ New messages arrived"
  banner (FR-011); see [reply-gate.md](reply-gate.md) "Pending-draft integration".
- A draft created through **Draft anyway** shows "✍️ You asked for this draft".

## `/chats` chat settings

Each chat screen shows the line "💤 Skip thanks/bye: Global (ON)" (or "Always draft" /
"Skip") and a button row **Global (ON) · Always draft · Skip**, the current value marked ✓.
The buttons are `chatskip:<work_chat_id>:inherit|off|on` (same `work_chats.skip_closing` as
`skipchat:`), handled with the other per-chat options: "✅ Saved" and the chat screen is sent
again.

## `/history`

`skipped` → "💤 Skipped — thanks" (the reason from `meta.category`), `draft_anyway` →
"✍️ Drafted on request", each with the contact and time, never message content.

## Notifications

None. A skip produces no message in the control chat (FR-008).
