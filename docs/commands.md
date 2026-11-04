# Commands

## Bot commands

Send these in your bot chat. The same list is in the bot's menu and in `/help`.

| Command | What it does |
| --- | --- |
| `/start` | Set up: connect Telegram (QR code or phone), pick work chats or turn on work account mode, set your time zone, learn your style |
| `/chats` | Your work chats: **➕ Add work chats**, turn a chat on or off, per-chat language and reading delay, per-chat **⏱ auto-send** (on after a confirmation, off in one tap, with the share of drafts sent unchanged), exclusions in work account mode |
| `/settings` | Reading delay, working hours and days, time zone, bot language; switches for search over earlier conversations, unread marks, work account mode and the grammatical gender in drafts; auto-send: the **⏱ Cancel window** (1, 2, 5, 10 or 15 minutes), the **chats with auto-send** and **⏹ Turn off auto-send everywhere** |
| `/profile` | Your style profile and its rules: edit rules, per-contact profiles (up to 20), pin, rebuild, undo the last weekly refresh |
| `/commitments` | Promises found in your chats and Jira comments: resolve, delete, reopen |
| `/waiting` | Messages still waiting to be read, with **👀 Read now** |
| `/skipped` | Messages of the last 7 days that got no draft ("thanks!", "ok", forwards, plain announcements, or by one of your rules): **Draft anyway**, turn skipping off for everyone or one chat |
| `/pause` | Stop drafting; messages stay unread |
| `/resume` | Start drafting again |
| `/ooo` | Set or clear an out-of-office note; drafts become out-of-office replies |
| `/tasks` | Connect, reconnect or disconnect Jira or Trello and pick projects or boards (one tracker at a time) |
| `/history` | What the agent did recently, without message content |
| `/my_data` | What is stored about you, with **📦 Export** |
| `/telegram` | Telegram connection status: disconnect or reconnect |
| `/delete_my_data` | Delete everything about you and log the Telegram session out |
| `/help` | The list of commands |
| `/cancel` | Leave the current step (editing, setup, a settings prompt, a connection flow) |

### Draft card

Every draft comes as a card with:

- **✅ Send**: sends the draft from your account as an ordinary message.
- **✏️ Edit**: puts a short draft into the input field (with inline mode on in BotFather) or
  offers it for copying; send your corrected text back to the bot. See `SCRIBE_EDIT_PREFILL`.
- **🔁 Regenerate**: writes the draft again.
- **❌ Reject**: no reply; the message stays read.
- **🧾 Sources**: the tasks, promises and earlier messages the draft relies on.

A draft flagged for possible prompt injection needs one more confirmation before it is sent.

### Auto-send

Auto-send is off everywhere until you turn it on for one chat (`/chats` → the chat → **⏱ Turn on
auto-send**, then confirm). In that chat a draft that is eligible is sent from your account when
its cancel window ends (2 minutes by default; `/settings` → **⏱ Cancel window**). Its card shows
**⏱ Will be sent automatically at HH:MM** and:

- **✅ Send now** sends it at once.
- **⏹ Don't send automatically** cancels the countdown and keeps the draft for you.
- Edit, Regenerate and Reject also cancel the countdown first. If the draft is already being
  sent, the button answers "Already being sent automatically".

Drafts with a new promise, a sensitive topic, missing details, a suspicious message, a date that
differs from the task, a conflict, a weak style profile, a warning or a pause, or outside your
working hours, always wait for your tap; the card says **✋ Needs your approval** and why. Deleting
a card in the bot chat does not cancel its countdown; use ⏹.

When you send at least 20 of a chat's drafts in 30 days and more than 60% of them unchanged,
Scribe offers to turn auto-send on for that chat (at most one offer a day; **Not now** waits a
month, **Don't offer for this chat** stops it, and the chat screen can offer it again). If you
stop or change more than 4 of the last 10 automatic drafts, Scribe suggests turning it off (at
most once a week). Turning it off for a chat, or everywhere, stops running countdowns at once.
Disabling or excluding a chat turns its auto-send off too, and enabling it again does not turn it
back on.

## Operator commands

Run on the server:

```bash
docker compose run --rm app python -m scribe.admin <command>
```

`<who>` is a numeric Telegram id or `@username`. Exit code 0 is success, 1 is "not found or
nothing to change", 2 is a usage error.

| Command | What it does |
| --- | --- |
| `list-users` | One line per account: id, state, model tier, creation date. No personal data |
| `set-tier <user> quality\|economy` | Model tier for a user's drafts (`<user>` is an id from `list-users` or the control chat id) |
| `beta add <who> [--note TEXT]` | Invite someone when the access gate is on |
| `beta remove <who> [--note TEXT]` | Remove them from the beta list |
| `beta list` | Beta entries and active users |
| `beta requests [--all]` | Applications sent from the bot |
| `beta approve <id>` / `beta reject <id>` | Decide an application; the person is told |
| `sub grant <who> (--days N \| --until YYYY-MM-DD) [--note TEXT]` | Grant a subscription |
| `sub extend <who> (--days N \| --until YYYY-MM-DD) [--note TEXT]` | Extend the latest subscription |
| `sub end <who> [--note TEXT]` | End active and scheduled subscriptions |
| `sub list [--all]` | Subscriptions |
| `access show <who>` | Access status and recent access events of one person |
| `access stats [--days N]` | Counts of beta users, subscriptions, applications, refusals |
| `release-notes preview <X.Y.Z>` | Show the "what's new" message for a version, in each language |
| `release-notes check` | Validate every file in `release-notes/` |
| `release-notes send <X.Y.Z> [--dry-run]` | Queue a version's notes for users who have not got them |
| `release-notes status [--all]` | Announcements and their delivery counts |
| `cost [--days N]` | LLM spend in USD per user per UTC day, split by route, for the last N days including today (default 7), with calls and tokens; user ids only |

The access gate is described in [access-gate.md](access-gate.md), release notes in
[release-notes.md](release-notes.md).

Maintenance jobs, normally run by the scheduler:

```bash
docker compose run --rm app python -m scribe.jobs refresh_profiles --user <user id>
docker compose run --rm app python -m scribe.jobs reencrypt_profiles
docker compose run --rm app python -m scribe.jobs reencrypt_all [--check]
```

`reencrypt_profiles` encrypts rows written before their columns were encrypted.
`reencrypt_all` moves every encrypted value to the current key and format, for example after a
key rotation; `--check` only counts what is left. See
[Rotating the master key](configuration.md#rotating-the-master-key).
