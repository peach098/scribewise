# Contract: Telegram Control Chat

The control chat is the product's only user interface. This contract fixes the commands,
inline buttons, callback data and message layouts so the bot handlers, tests and the
quickstart agree. Callback data is `<action>:<entity_id>[:<extra>]`, ≤ 64 bytes.

## Commands

| Command | Purpose | Spec |
|---|---|---|
| `/start` | Create account, begin setup FSM (login → chats → tasks → defaults → import) | FR-022, FR-030 |
| `/chats` | Work chats with a per-chat settings button; ➕ Add work chats opens the setup chat picker with the chats not added yet (spec 007) | FR-001, FR-024 |
| `/settings` | Global delay range, working hours/days, time zone | FR-016, FR-017 |
| `/profile` | Show global profile summary; buttons: edit rules, list contact profiles, pin/remove, undo last refresh, rebuild | FR-023, FR-034, FR-037 |
| `/commitments` | List open commitments; buttons: resolve, edit, delete | FR-031 |
| `/waiting` | List waiting messages with planned reading time; button **Read now** per item | FR-018 |
| `/skipped` | *(added by 004, see [004 control chat](../../004-skip-closing-messages/contracts/control-chat.md))* Messages skipped as needing no reply, **Draft anyway**, skip switches | 004 FR-012–FR-014 |
| `/pause` / `/resume` | Pause/resume drafting (waiting messages stay unread) | FR-025 |
| `/ooo [text]` | Set/clear out-of-office; optional suggested reply text | FR-025 |
| `/tasks` | Connect / reconnect / disconnect task source; pick boards | FR-019 |
| `/history` | Last 20 activity records (no message content) | FR-027 |
| `/my_data` | Counts of stored items by type (messages, drafts, profiles, commitments, activity) with retention dates; button **📦 Export** sends a JSON file (decrypted, owner only) | FR-026 |
| `/telegram` | *(superseded by 002: disconnect keeps the user state; reconnect offers QR code or phone)* Show Telegram session status; **Disconnect** (log out session, stop reading/drafting, user → `setting_up`) / **Reconnect** (re-run phone → code → 2FA) | FR-030 |
| `/delete_my_data` | Two-step confirmation → delete everything and log out Telegram session | FR-026, FR-030 |
| `/help` | Command list | |

## Draft card

Sent when a draft is prepared (turn → `pending_review`). One card per draft version; the
card is edited in place on state changes.

```
💬 <Contact or Group title>   ·   <chat kind>   ·   read <HH:MM>
› <incoming message(s), quoted, truncated to 600 chars>

✍️ Draft (<profile used: "Anna" | "global">, tone shift: none | more formal)
<draft text>

⚑ Flags: 🤝 commits you to <what>  ·  ⚠️ conflicts with "<task/commitment>"  ·  🔒 sensitive
❓ Needs info:
1. <placeholder 1 question>
2. <placeholder 2 question>
↩️ Reply with the missing values (one per line), or tap ✏️ Edit to rewrite.
📎 Sources: Trello “Q3 report” (due Fri 16 Oct) · your promise “send the deck” (due Thu 15 Oct)
⏳ Expires <HH:MM tomorrow>
```

Inline keyboard (row 1 / row 2):

| Button | callback | Effect |
|---|---|---|
| ✅ Send | `approve:<draft_id>` | Sets `approved_by_user_at`, sends immediately, card → "✅ Sent HH:MM" | 
| ✏️ Edit | `edit:<draft_id>` | Bot replies to the card with a prompt that carries the draft (see "✏️ Edit prompt"); the next text the user sends becomes `edited_text` and is sent; card → "✅ Sent (edited)" |
| 🔁 Regenerate | `regen:<draft_id>` | New draft version; old card → "superseded", new card posted |
| ❌ Reject | `reject:<draft_id>` | Turn → `rejected`; card → "❌ Rejected" |
| 🧾 Sources | `sources:<draft_id>` | Expands full source list in a reply message |

✏️ Edit prompt (issue #7; the user edits the draft instead of retyping it):
- Draft of ≤ 256 characters (the Bot API limit of both buttons): one button. With inline mode
  on, **✏️ Edit in input field** (`switch_inline_query_current_chat` = the draft) fills the
  input field with `@<bot> <draft>`; otherwise **📋 Copy draft** (`copy_text`) copies the draft.
  `SCRIBE_EDIT_PREFILL` = `auto` (default: inline when `getMe.supports_inline_queries`),
  `inline` or `copy`. Inline queries are answered with no results (`is_personal`, no cache).
- Longer draft: the prompt shows it in a `<code>` block (one tap copies it) with `ForceReply`
  (placeholder "Your version…"); a draft too long for one message gets the plain prompt.
- A leading `@<bot>` (case-insensitive) is removed from the text before it is used, so a
  saved edit never contains it; a message that is only `@<bot>` is ignored. `/cancel` ends the
  edit; any other command is never taken as the edit.

Rules:
- `approve` and `edit` are the **only** callbacks that lead to sending. Both require the
  draft to be `pending`; an expired/superseded draft answers "This draft has expired" and
  does nothing.
- A draft flagged `sensitive` or with `needs_info=true` shows an extra confirmation step
  on Send ("Draft has unresolved placeholders / sensitive content. Send anyway?").
- **Filling placeholders (needs-info cards).** The `❓ Needs info` block lists the questions of
  the `[[...]]` placeholders still in the draft text, numbered: first the model's
  `placeholders[]` in their order (only those whose marker is in the text), then any other
  marker in the text (asked as the marker itself). A marker used several times is one item.
  The hint line reads "↩️ Reply with the missing value, or tap ✏️ Edit to rewrite." for one
  item and "↩️ Reply with the missing values (one per line), or tap ✏️ Edit to rewrite." for
  several; it is left out of finished cards. A needs-info draft without any marker in its
  text keeps "❓ Needs info: see the draft" and no hint.
  - A plain text message (not starting with `/`) that replies to the card, or any plain text
    message while that card is the newest pending card in the chat and no other flow (setup,
    Telegram login, a settings question, ✏️ Edit) is waiting for input, is taken as the
    values: the whole message for one item, otherwise one value per non-blank line in the
    listed order. Values are substituted literally (every occurrence of the marker), with no
    LLM pass.
  - The filled text becomes a **new draft version** (old card → "✍️ Superseded: values filled
    in a new version", buttons removed; new card posted with the usual buttons; the `outdated`
    flag carries over). Nothing is sent: the new card goes through the normal Send / Edit /
    Regenerate / Reject review.
  - A wrong number of values answers, as a reply to the card, "⚠️ This draft needs N values,
    one per line, in this order: <numbered questions> You sent M. Nothing was changed: reply
    again, or tap ✏️ Edit to rewrite." and leaves the draft unchanged. A reply to a card with
    nothing to fill answers "This draft has no missing values to fill in. Tap ✏️ Edit to
    rewrite it."; a reply to an expired, superseded or finished card answers "This draft has
    expired". Plain text with no needs-info card to fill is ignored.
  - The new version has `needs_info=false` and no placeholders, so its Send no longer asks the
    "unresolved placeholders" confirmation (the sensitive-content confirmation is unaffected).
    The filled text is treated as the user's edit: stored encrypted in `text` and
    `edited_text` (never in `output_json`, never logged), and Send records
    `edited_and_approved` and shows "✅ Sent (edited)". ✏️ Edit works as before on either
    version.
- When `outdated=true`, the card gets a banner "⚠️ New messages arrived — this draft may be
  out of date." and the Send button is replaced by **🔁 Regenerate** (`regen:<draft_id>`) and
  **Send anyway** (`approve:<draft_id>:anyway`: the same approval path as ✅ Send, including the
  sensitive / placeholder confirmation, whose button is then `approve:<draft_id>:anyway_confirm`).
  Only `anyway` / `anyway_confirm` may send an outdated draft: a plain
  `approve:<draft_id>[:confirm]` (an old card, a card whose re-render failed) sends nothing and
  answers "⚠️ New messages arrived after this draft. 🔁 Regenerate it, or Send anyway as it is?"
  with those two buttons; `send.py` enforces the same rule under the row lock. ✏️ Edit sends the
  user's own text and is allowed for an outdated draft. Regenerating an outdated draft first
  marks the chat read up to the newest message of the turn (never past the first message of
  another turn still waiting in that chat). A new message from the turn's sender joins the
  pending turn, so the regenerated draft covers it; a message that joins while a draft is being
  composed or regenerated makes that draft outdated. The reply always quotes the message the
  draft was composed for.
- `reject` finalizes only a still-pending draft (checked under its row lock); a draft sent,
  dropped or rejected meanwhile answers "This draft was already handled".

## Reminder and expiry messages

- 2 h after `prepared_at`, if still pending: reply to the card "⏰ Still waiting for your
  decision on this draft." (one time).
- 24 h after `prepared_at`: card → "⌛ Expired — handle manually", buttons removed; the turn
  becomes `handle_manually`. A draft that is already expired when its reminder falls due
  (service downtime) gets no late reminder.
- With `SCRIBE_TEST_FAST_CLOCK=1` the reminder comes after 1 min and expiry after 2 min
  (`SCRIBE_DRAFT_REMIND_AFTER_S` / `SCRIBE_DRAFT_EXPIRE_AFTER_S` otherwise, default 2 h / 24 h).
- Manual reply while a draft is pending: card → "✅ You replied yourself — dropped", buttons
  removed (plus the "Manual reply detected" notification below).

## Waiting list item (`/waiting`)

One message per waiting turn (at most 10, then "…and N more"); times are in the user's time
zone, with weekday and date when not today, and "now" once due. A paused user first sees
"⏸ Scribe is paused: these stay unread until /resume." Read now while paused answers that the
message will be read after /resume.

```
⏳ <Contact> · arrived HH:MM · will be read at HH:MM
› <first 120 chars>
[👀 Read now]  callback: readnow:<turn_id>
```

## Setup FSM states

> Superseded for the Telegram connection steps (`phone` → `code` → `password?`) and for the
> `/telegram` command by `specs/002-telegram-qr-login/contracts/control-chat.md`.

`link` → `phone` → `code` → `password?` → `pick_chats` → `tasks_connect` → `tasks_boards`
→ `timezone` → `hours` → `delay` → `importing` → `done`. Each state has a text prompt and,
where applicable, inline options; `/cancel` aborts setup and keeps the account in
`setting_up`.

> `pick_chats` gains the 💼 work account switch (`pickwa:<on|off>`) in
> `specs/008-work-account-mode/contracts/control-chat.md`.

## Profile view (`/profile`)

```
🧑 Your style (global, v3, built from 412 messages)
<summary_text>
Rules you set:
1. never use emojis
2. reply in English
[✏️ Edit rules] profrules:<profile_id>  [👥 Contact profiles (14/20)] profcontacts:0
[↩️ Undo last refresh] undo_refresh:<batch_id>  [🔄 Rebuild] profrebuild:0
```
- A profile built from fewer than `min_style_samples` (50) messages adds "⚠️ Built from fewer
  than 50 of your messages, so style matching is weak: drafts use a neutral, polite default
  until more of your messages are available." No profile yet: "🧑 No style profile yet…" with
  the contact-profiles and Rebuild buttons.
- ↩️ Undo last refresh is shown only while the latest weekly refresh can still be undone (it
  changed something, was not undone, and no 🔄 Rebuild came after it).
- Contact profiles (`profcontacts:0`): one line per current contact profile (pinned first, then by
  activity; a group chat is one contact, "<title> (group)") and one button row each:
  👁 view (`profview:<profile_id>`: that profile's summary and rules, with ✏️ Edit rules),
  📌 Pin / 📌 Unpin (`pin:<profile_id>` / `unpin:<profile_id>`; a pinned contact always keeps its
  profile; at most 20 pinned) and 🗑 remove (`removeprofile:<profile_id>`: the profile is retired,
  drafts to that contact use the global profile, and the removal is recorded: only the messages
  written after it count, also on 🔄 Rebuild, so the contact gets a profile again only if those
  alone make it one of the most active contacts again. "🗑 Removed the profile for <name>: drafts
  to them use your global profile. It comes back only if your messages to them from now on (not
  the earlier ones, also on 🔄 Rebuild) make them one of your most active contacts again.").
- ✏️ Edit rules (`profrules:<profile_id>`): the numbered rules of that profile; every message
  adds a rule (≤ 200 characters, ≤ 30 rules), `-N` removes rule N, `/done` or `/cancel` ends;
  any other command ends it too (later text is never taken as a rule). Rules go to the profile's
  current version (`user_rules_json`, re-read under the user's profile lock, so a rule saved
  while a rebuild or refresh stores a new version lands on the new version), win over the
  learned rules in drafts, and are copied unchanged by every rebuild and refresh.
- Global rules apply to every draft (issue #42), also for a contact with a profile of its own:
  they are merged in when the draft is made (never copied into the contact's profile), so a
  changed or removed global rule applies to the next draft without a rebuild; on conflict the
  contact's own rule wins. A contact's 👁 view and ✏️ Edit rules list them after the contact's
  numbered rules, read-only: "Global rules (apply here too; edit them in your global
  profile):" with one "• <rule>" line each (a rule the contact also has is listed once, as
  the contact's); `-N` refers to the contact's own rules only. When the view would exceed one
  Telegram message (4096 UTF-16 code units), the global list is cut first ("…and N more
  global rules: see your global profile."), then the contact's own ("…and N more rules of
  yours."; "…and 1 more rule of yours."). The same rule means the same text (case and spacing
  ignored), the same proposal, or an accepted proposal's rule as shown in the user's language
  (e.g. "без эмодзи"), everywhere: adding, listing, drafting and proposing.
- A rule already set globally is not proposed for a contact. Accepting a contact's proposal
  whose rule the global profile got meanwhile adds nothing: "✅ "<rule>" is already one of
  your global rules, so it applies to <name> too. Nothing was added." No "rule added" history
  entry; the proposal is closed as dismissed with its evidence cleared (`rule_json.closed =
  "already_global"`), so the rule can be proposed again as soon as the global one is removed.
  A rule typed in a contact's ✏️ Edit rules that is already a global rule is not stored
  either: ""<rule>" is already one of your global rules, so it applies to <name> too. Nothing
  was added."
- Contact profile builds learn from the contact's messages and own rules only: global rules
  are applied when the draft is made.
- ✅ Add rule on a profile that already has 30 rules answers the limit error ("A profile can
  have at most 30 rules; remove one first.") and leaves the proposal open.
- 🔄 Rebuild (`profrebuild:0`) explains first; **🔄 Rebuild now** (`profrebuild:1`) removes its
  button at once, re-imports the user's own messages of the last 6 months in the enabled work
  chats and rebuilds the global profile and the profiles of the top contacts (≤ 20) with
  synchronous LLM calls (never the batch API: the user is waiting), then reports "✅ Rebuilt:
  global profile from N of your messages, K contact profile(s)." A second tap while a rebuild
  runs answers "⏳ Already rebuilding your profiles; wait for it to finish." A rebuild ends the
  latest refresh's undo. If the account was deleted meanwhile the rebuild ends without a
  message. The global and the contact profiles fail independently (issue #30): a failed global
  build reports "⚠️ I couldn't rebuild your global profile right now, so it stays as it was.
  Rebuilt: K contact profile(s).", failed contact builds add "⚠️ K contact profile(s) couldn't
  be rebuilt …", and only when nothing could be rebuilt the note is "⚠️ I couldn't rebuild your
  profiles right now. Nothing was changed."
- Profile selection for a draft: the contact's current profile (in a group chat: the group's),
  else the global one; a contact needs at least 10 of the user's own messages for a profile of its
  own. The card's "✍️ Draft (<profile>, …)" names the profile used ("global", the contact or the
  group title).

## Style rule proposal

```
💡 I noticed you removed emojis in 5 of your last 6 edits for Anna.
Add rule "no emojis" to Anna's profile?
[✅ Add rule]  callback: rule_accept:<proposal_id>   [🚫 Dismiss]  callback: rule_dismiss:<proposal_id>
```
- Posted after an ✏️ edited send when the same kind of change shows in at least 5 of the last 6
  edited drafts of one profile (across its versions): emojis removed / added, greeting removed,
  sign-off removed, much shorter / longer, more formal / casual, switched to English / Russian.
  For the global profile: "… edits with your global profile. Add rule "…" to your global
  profile?".
- ✅ Add rule adds the rule to the current version of that profile and edits the message to
  "✅ Added rule "…" to …'s profile. /profile shows it." (`activity_records(rule_accepted)`);
  🚫 Dismiss → "🚫 Dismissed. Your profile is unchanged." The profile never changes otherwise. A
  closed proposal answers "This suggestion is no longer open". The same rule is not proposed again
  while a proposal for it is open or accepted, nor from the same edits after a dismissal.

## Weekly refresh note

```
🔄 Profiles refreshed (global + 3 contacts). Changes: shorter replies with Ivan; more formal
with "Finance" group. Your own rules are unchanged. [↩️ Undo]  callback: undo_refresh:<batch_id>
```
- Weekly (Sunday 02:00 UTC, job `refresh_profiles`) for every user past setup, or now with
  `python -m scribe.jobs refresh_profiles --user <id>`. Changes name formality, typical length,
  emoji use and main language per profile ("new profile with …" for a contact that got one; "no
  notable changes" otherwise); retired contact profiles are mentioned. No note when nothing
  changed (fewer than 10 new own messages for every profile and no slot change); the run is
  still recorded (`profile_refreshed`, `meta_json.noop`), so its messages are not counted again
  by the next run. The weekly job uses the batch API; a run interrupted by a restart resumes at
  startup with the users not refreshed yet.
- ↩️ Undo (`undo_refresh:<batch_id>`, the `profile_refreshed` activity record) restores the
  previous version of every profile in that refresh, retires the profiles new in it and gives
  contacts retired by it their profile back while a slot is free (never more than 20 contact
  profiles); rules set after the refresh are kept. Only the latest refresh can be undone, once,
  and only while every profile it created is still current: after a 🔄 Rebuild, a later
  refresh or 🗑 Remove of one of them nothing changes and the answer is "Nothing to undo: only
  the latest refresh can be undone, once, and not after a rebuild or a later change of those
  profiles". Success: "↩️ Restored your previous profiles (N restored). Rules you set are
  unchanged."

## Notifications (no buttons)
- Media-only message in an enabled chat: "📎 <Contact> sent a voice/photo/file — I can't
  draft for that; please handle it manually."
- Manual reply detected: "✅ You replied to <Contact> yourself — dropped the waiting
  message/draft."
- Telegram session revoked / Trello error: one-line alert with `/start` or `/tasks` hint.

## Settings (`/settings`, `/chats`, `/pause`, `/resume`)

> Extended by `specs/008-work-account-mode/contracts/control-chat.md`: the work account line and
> button in `/settings` (`settings:work_account`, `wacct:<on|off|cancel>`), and the `/chats`
> header, marks, paging (`chatspage:<n>`), exclusion picker (`wxopen:0`) and Exclude / Include
> again on private chats.

```
⚙️ Settings
⏱ Reading delay: 2 min–10 min, random for every message      (or "5 min (fixed)")
🕘 Working hours: 09:00–19:00
📅 Working days: Mon, Tue, Wed, Thu, Fri
🌍 Time zone: UTC
🌐 Language: English
▶️ Drafting: active (/pause to pause)
[⏱ Delay] settings:delay  [🕘 Hours] settings:hours  [📅 Days] settings:days  [🌍 Time zone] settings:tz
[🌐 Language] settings:lang
```
- Language (issue #8): every control-chat text, button, alert and background notice is in
  `users.language` (`en` / `ru`, catalogs in `scribe/i18n/`). `settings:lang` shows
  [English] [Русский] (`setlang:en|ru`, the current one ticked); a tap saves it, answers in the
  new language at once and sets this chat's `/` menu in it (`BotCommandScopeChat`). The default
  menu is English, with a Russian variant for `language_code=ru` clients. Dates use the
  catalogs' month names ("5 ноя", "завтра в 10:05"), never the system locale. Delay input
  also accepts Russian units (`2-10 мин`, `90 с`, `1-2 ч`).
- Delay, hours and time zone are asked as a text reply; `/cancel` keeps the old value.
  Delay: `2-10 min`, `90-300 s`, `1-2 h`, a single value for a fixed delay; numbers without a
  unit are minutes; `0 ≤ min ≤ max ≤ 86400` s. Hours: `09:00-19:00`, start < end. Invalid
  input is explained and the question stays open.
- Time zone (issue #5): stored as an IANA name known to `zoneinfo`; input is human —
  `Europe/Moscow` / `europe/moscow`, a city (`moscow`, `new york`), an alias (`Москва`, `МСК`,
  `CET`, `PST`, …) or an offset (`UTC+3`, `GMT+3`, `+3`, `+03:00`, `МСК+1`). Whole-hour offsets
  are stored as `Etc/GMT-3` style zones (POSIX sign: `Etc/GMT-3` = UTC+3, shown as
  "Etc/GMT-3 (UTC+3)"); non-whole offsets map to the city zone at that offset now
  (`UTC+5:30` → `Asia/Kolkata`); both add a hint that a city follows DST. The prompt has quick
  buttons `settz:<zone>` (Telegram `language_code` picks them, e.g. `ru` → Moscow first);
  input matching several different zones offers them as `settz:<zone>` buttons. Reply:
  "✅ Time zone set to Europe/Moscow (UTC+3, now 14:05 there)." The setup `timezone` step
  works the same with `setuptz:<zone>` and **Keep UTC** (`setuptz:keep`).
- Days: ✅/⬜ toggles `setday:<0-6>` edited in place, `setday:done`; the last working day
  cannot be removed.
- Changing hours, days or time zone re-plans waiting messages:
  `due_at = next_allowed(max(arrived_at + delay, now))` (a wait cut short by Read now or by
  reading elsewhere is kept).
- `/chats`: the user's work chats, one button each (`chatcfg:<work_chat_id>`; ⚙️ enabled, ⏸
  disabled, enabled first) → chat settings with the reading delay ("global (…)" or the override),
  language ("the language of the incoming message" or the code), formality ("from your style
  profile" or `N/5`), for group chats the fixed drafting rule, and whether the chat is enabled.
  Under the list (also under "No work chats are enabled yet"), set-up users get **➕ Add work
  chats** (`chatadd:0`): the setup chat picker with only the chats that are not work chats yet;
  Done adds them enabled and learns the user's own messages in them like 🔄 Rebuild. See
  [specs/007 contracts/control-chat.md](../../007-add-work-chats/contracts/control-chat.md).
  Chat settings:
  - **⏱ Set delay** (`chatdelay:<work_chat_id>`, same input format) / **↩️ Use global**
    (`chatdelay:<work_chat_id>:clear`);
  - **🌐 Language** (`chatlang:<work_chat_id>`): a BCP-47 tag (`en`, `ru`, `pt-BR`, `zh-Hant`,
    `es-419`; normalised) or a language name (English, Russian, German, …) as a text reply,
    `/cancel` keeps it; **↩️ Auto** (`chatlang:<work_chat_id>:clear`);
  - **🎩1 … 🎩5** (`chatformal:<work_chat_id>:<1-5>`), **↩️** (`chatformal:<work_chat_id>:clear`);
  - group chats have no mode buttons; the screen shows the fixed line "👥 Drafts in this group:
    only when you are tagged or replied to" (FR-003, specs/006-group-mention-only). A stale
    `chatgroup:<work_chat_id>:<mentions_only|always>` button from an older card changes nothing
    and answers "This option is no longer available";
  - **⏸ Disable chat** / **▶️ Enable chat** (`chatenable:<work_chat_id>:<off|on>`): a disabled chat
    is not read or stored; its waiting messages are dropped (`chat_disabled`), and so are its
    pending drafts with their turns (each card is re-rendered with "⏸ Chat disabled — dropped",
    no buttons). A message being read and drafted at that moment ends without a draft.
  Each change re-posts the chat settings. Overrides go into every draft for that chat as
  `chat_overrides` and win over the selected style profile (FR-024).
- `/pause` → "⏸ Paused. Incoming work messages stay unread and no drafts are prepared until
  /resume." (`users.state = paused`; messages are still received and stored). `/resume` →
  state `active`, waiting messages re-planned as above, "▶️ Resumed. Waiting messages: N; any
  that are already due are read now." Both refuse during setup.
- These commands answer when no other flow is active (no FSM state, a settings question, or
  a draft edit); during setup or the Telegram login those flows handle commands. While a
  draft is being edited, a message starting with `/` is never taken as the reply text.

## Task source (`/tasks`)

```
🗂 Task source
✅ Trello: connected (read-only)            (or "⚠️ Trello: error — … Reconnect below." / "Not connected.")
📋 Boards: 2 selected
[📋 Boards] tasks:boards  [🔁 Reconnect] tasks:connect  [❌ Disconnect] tasks:disconnect
```
- Not connected: one button **🔌 Connect Trello** (`tasks:connect`). Jira comes with the release
  version.
- Connect: "1/2 Send your Trello API key" → "2/2 … send me the token" with the authorize link
  (`scope=read`, `expiration=never`; it has to carry the key); both messages are deleted from
  the chat (best effort), and so is the "2/2" prompt when the token step ends. The key waits in
  the encrypted FSM data until the token arrives, an implausible value is asked again. The step
  ends (key dropped, prompt deleted) on the token, `/cancel`, any other command, or a token sent
  more than 15 minutes after the key ("⌛ That took too long…"). The pair is checked with
  `TaskSource.validate`; rejected or unreachable → "Trello did not accept this key and token, or
  did not answer. Nothing was saved." Accepted → stored encrypted; the previous board selection
  is kept only when the same Trello account reconnects (another account starts with none), and
  the board picker follows. `tasks:*` buttons answer only when no setup or login flow is active
  (a stale button never replaces that flow's state).
- Boards: the account's open boards, 8 per page, `tboard:<board_id>` toggles ✅/⬜ (edited in
  place; only ids Trello returned for these credentials are accepted), `tpage:<n>` pages,
  `tboard:done` shows the status. Changes invalidate the 60 s task cache.
- Disconnect removes the connection: drafts no longer use tasks.
- After 3 failures in a row the status shows `error`, and the control chat gets one alert per
  failure streak (a success resets it): "⚠️ Trello is not answering or the token was revoked, so
  drafts are prepared without task context. Reconnect it in /tasks." Each draft prepared
  meanwhile shows "⚠️ drafted without task context"; after a failure Trello is not asked again
  for 30 s. A selected board that cannot be read is skipped; the draft uses the other boards and
  shows "⚠️ some task boards could not be read".
- The guided-setup steps `tasks_connect` / `tasks_boards` are not in this release's setup flow
  yet (T073, after `specs/002-telegram-qr-login`); `/tasks` is the way to connect.

## Commitments (`/commitments`)

```
🤝 Open commitments (3):
🤝 send the deck · Anna · due Thu 15 Oct
› “Отправлю презентацию в четверг”
[✅ Resolve] cmtres:<id>  [✏️ Edit] cmtedit:<id>  [🗑 Delete] cmtdel:<id>
```
- One message per open commitment (earliest due first, undated last; at most 20, then "…and N
  more"); none → "🤝 No open commitments…".
- Resolve / Delete: the message becomes "✅ Resolved: …" / "🗑 Deleted: …"; the row keeps
  `closed_at` and is removed by the retention job 30 days later.
- Edit asks for the new text, a new date (`22.10`, `2026-10-22`, `Thursday`), both as
  `text; date`, or `no date`; `/cancel` keeps it. The input is a date-only edit only when all
  of it is one date expression (`пятница`, `в четверг`); "send today" is new text. A message
  starting with `/` is never the new text. **✏️ Edit** answers only when no setup or login flow
  is active.
- Callbacks act only on the caller's own open commitments ("This commitment is no longer open"
  otherwise). `/tasks` and `/commitments` follow the same rule as the settings commands: they
  answer when no setup or login flow is active.

## Draft card: US3 parts

- 📎 Sources: `task:<id>` → `Trello “<title>” (due <Ddd D Mon>)`, the due date in the user's
  time zone (🧾 Sources adds `· <status>`;
  titles come from the 60 s task cache, refreshed for 🧾 Sources when needed — nothing from Trello
  is stored); `commit:<uuid>` → `your promise “<what>” (due …)` (🧾 Sources adds when it was
  said and a quote).
- ⚑ Flags: `⚠️ conflicts with "<task title or commitment> (due …)"`; 🧾 Sources lists each
  conflict with its description. `🤝 commits you to <what> (by <date>)` when the draft promises
  something new.

## Out of office and history (`/ooo`, `/history`)

- `/ooo <text>` → `users.state = out_of_office`, `ooo_message = <text>` (≤ 500 characters):
  "🏖 Out of office is on. Drafts are prepared as out-of-office replies based on: “…”. /ooo turns
  it off." Messages are still read and drafted after their delay; each draft is an out-of-office
  style reply based on the text (`user_state.ooo`) and the card shows "⚠️ out-of-office reply
  (/ooo to turn it off)". `/ooo` alone turns it off ("✅ Out of office is off…", state `active`,
  `ooo_message = NULL`), or, when it is off, explains how to turn it on.
- While paused, `/ooo <text>` only changes the text (state stays `paused`; /resume restores out of
  office) and `/ooo` clears it (the pause stays). `ooo_message` is non-NULL exactly while out of
  office, including a pause taken from it (data-model.md). Refused during setup.
- `/history`: "🧾 Recent activity (last N, newest first; no message content):" then one line per
  record, `<Ddd DD Mon HH:MM> · <what> · <contact>`: ✍️ Draft prepared, 👍 Approved by you,
  ✏️ Edited and approved by you, ❌ Rejected by you, 🔁 Regenerated, ✅ Sent, ⌛ Expired,
  🗑 Dropped, 👀 Read now, 🔄 Profiles refreshed, 🔄 Profiles checked (nothing new to learn),
  ↩️ Profile refresh undone, 🗑 Contact profile removed, 🔄 Profiles rebuilt, 💡 Style rule
  added, 📦 Data exported, ➕ Work chats added (with up to 3 chat titles, spec 007) (last 20;
  times in the user's time zone). Never message or draft text.

## Your data (`/my_data`, `/delete_my_data`)

```
📦 What I store about you
💬 Messages: 120 (oldest 14 Sep 2026; each is deleted 30 days after it was sent; next removal 14 Oct 2026)
✍️ Drafts: 34 (oldest …; the text is deleted 30 days after your decision; the record stays; next removal …)
🧑 Style profiles: 5 (3 current, 1 previous, 1 retired; retired and previous versions are deleted 30 days after they were replaced, except the one ↩️ Undo of your last refresh would restore; next removal …)
💡 Rule suggestions: 2
🤝 Commitments: 4 open, 1 closed (closed ones deleted 30 days after closing; next removal …)
🧾 Activity records: 210 (oldest …; kept until you delete your data)
👥 Contacts: 12 · 💼 Work chats: 4
🔌 Telegram session: active · 🗂 Task source: trello (active)
[📦 Export]  callback: mydata:export
```
- 📦 Export sends `scribe-data-<YYYY-MM-DD>.json` as a document in the control chat: every row of
  the caller grouped by table (`users`, `telegram_sessions` without the session string,
  `task_source_connections` without credentials, `work_chats`, `contacts`, `messages`,
  `conversation_turns`, `drafts` with the decrypted draft content, `style_profiles`,
  `style_rule_proposals`, `commitments`, `activity_records`, `bot_fsm_state` without its
  `data`, which may hold pending task-source credentials, issue #56), decrypted, nothing
  of other users; `activity_records(data_exported)` with row counts only. Neither the summary nor
  the file is logged.
- `/delete_my_data` → "⚠️ This deletes everything I store about you … Type DELETE to confirm, or
  /cancel." Only `DELETE` (within 10 minutes) confirms; any other reply, /cancel or another
  command → "Not deleted. Nothing was changed." On confirmation: the Telegram session is logged
  out, the user's client is stopped, the user row is deleted (every per-user table cascades, the
  FSM row by trigger), the task-source cache entry is dropped, and the chat gets "🗑 Done. All your
  data was deleted and the Telegram session was logged out. Send /start if you want to set Scribe
  up again." (`data_deleted` is written to the logs only). If Telegram cannot be reached to log
  out: "⚠️ I couldn't reach Telegram to end the session, so nothing was deleted yet. Try
  /delete_my_data again in a minute." If the deletion itself fails after the log-out, the
  session is marked `revoked` and the chat gets "⚠️ The Telegram session I used is logged out
  already, but I couldn't delete your data just now. Nothing else was changed and Scribe no
  longer reads your chats. Send /delete_my_data again in a minute to delete the rest." The
  log-out uses `MessagingGateway.log_out` from
  `specs/002-telegram-qr-login` (wired explicitly after that merge; until then a warning is
  logged when the gateway has none).

## Callback ownership (T072)

Every callback that names a stored row — `approve|edit|regen|reject|sources:<draft_id>`,
`readnow:<turn_id>`, `pin|unpin|removeprofile|profrules|profview:<profile_id>`,
`rule_accept|rule_dismiss:<proposal_id>`, `undo_refresh:<batch_id>`,
`cmtres|cmtedit|cmtdel:<commitment_id>`, `chatcfg|chatdelay|chatlang|chatformal|chatgroup|
chatenable:<work_chat_id>` (`chatgroup` is legacy since 006) — is checked before its handler: a row that does not exist or belongs
to another user answers "Draft not found" / "Profile not found" / "Chat not found" / … and nothing
happens (`control_bot/ownership.py`; the handlers load every row scoped to the caller as well).
Malformed data for one of these actions (not `<action>:<uuid>[:<extra>]`) is refused the same
way (the check fails closed). The test derives the action list from the callback filters
registered on the dispatcher, so a new entity button without a check fails it.
The US4 commands and buttons answer only when no setup or login flow is active, like the other
settings commands.
