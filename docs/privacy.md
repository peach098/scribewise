# Privacy and data

Scribe works with confidential messages, so this page describes exactly what it reads, what it
keeps, for how long, and what leaves your server. It describes a self-hosted install: you are
the operator, and the data stays in your database.

## What Scribe reads

- **Only your work chats.** A message in any other chat is dropped before anything is stored.
  In work account mode every private chat with a person is a work chat, except the ones you
  exclude; bots, channels, Saved Messages and groups you did not pick are still ignored.
- **Your own messages from the last 6 months** in those chats, once, to learn your style
  (`SCRIBE_STYLE_IMPORT_MONTHS`). Only a sample is sent to the model.
- **Earlier conversations with the sender**, on demand, when a message refers to something
  older. See [earlier-search.md](earlier-search.md).
- **Jira or Trello**, read-only, if you connect them: your open issues or cards and, for Jira,
  your own comments.

## What is stored, and for how long

| Data | Stored | Removed |
| --- | --- | --- |
| Messages in work chats | encrypted | after 30 days (`SCRIBE_MESSAGE_RETENTION_DAYS`) |
| Draft texts, your edits, cited excerpts | encrypted | 30 days after you sent, rejected or let the draft expire |
| Style profiles: rules, example messages, summary | encrypted | old versions 30 days after they were replaced |
| Commitments (what, to whom, when) | encrypted | 30 days after they were resolved or deleted |
| The message a commitment quotes | encrypted | after 30 days (`SCRIBE_MESSAGE_RETENTION_DAYS`), even while the commitment is still open |
| Telegram session, Jira and Trello tokens | encrypted | when you disconnect or delete your data |
| A Jira or Trello token you are in the middle of connecting | encrypted | when you finish or cancel, press another button, or after 15 minutes at the latest |
| Chat titles, contact names, your out-of-office note, Jira site and account | encrypted | when you delete your data. A chat you never picked or configured, and a contact nothing refers to any more, also 90 days after Scribe last saw a message there |
| Activity log for `/history` (no message content) | plain text | when you delete your data |
| What you decided on each draft, for the auto-send offer (`draft_decisions`: ids, the kind of decision, a time; no message content) | plain text | 31 days after the decision, or when you delete your data |
| Daily LLM usage and cost per user (no message content; for the operator, not in `/my_data`) | plain text | after 90 days (`SCRIBE_LLM_SPEND_RETENTION_DAYS`) or when you delete your data |

Encryption is AES-256-GCM in the application, with a fresh random nonce for every value, under
your `SCRIBE_MASTER_KEY`. Each value is bound to its table and column, so a value copied into
another place in the database does not decrypt. A database dump without the key does not reveal
message texts, drafts, tokens, or the names of chats and contacts; it does show ids, times,
settings and the activity log. If the key may have leaked, replace it: see
[Rotating the master key](configuration.md#rotating-the-master-key).

Not stored at all: your phone number, login codes, your 2FA password and the QR login token.
The messages in which you typed them are deleted from the bot chat right away.

## What goes to the LLM provider

To write a draft, Scribe sends the provider the new messages, a selection of recent history in
that chat, your style profile, the relevant tasks and commitments, and any earlier messages it
found. To learn your style it sends a sample of your own messages. Nothing else is sent.

Scribe refuses to start with a provider unless the operator has set `data_use_confirmed: true`
for it, confirming that the provider does not train on this data. Check where the provider
stores data, too: DeepSeek, for example, stores it in China and trains on API input unless you
opt out ([details](self-hosting.md#deepseek)). For the strongest privacy,
point every route at a model you host yourself; see
[Choosing models](self-hosting.md#choosing-models).

Messages from contacts are passed to the model as untrusted data, never as instructions, and a
message that looks like instructions to the assistant is flagged on the draft card.

## Logs

Logs record what happened (a draft was created, a message was read, a job ran) with ids and
counts, not message texts, tokens or phone numbers. This holds at every `SCRIBE_LOG_LEVEL`,
`DEBUG` included: the LLM, Telegram and HTTP libraries are kept at `INFO` or higher (HTTP clients
at `WARNING`) whatever the level, because their own debug output includes message content. Only
`SCRIBE_LOG_LIBRARIES_DEBUG=true` lets them log at `DEBUG`; use it just to troubleshoot, and
turn it off again.

## Your controls

- `/my_data` shows what is stored about you; **📦 Export** sends it as a JSON file. The file
  never holds tokens, including one you are in the middle of connecting.
- `/delete_my_data` deletes everything about you and logs the Telegram session out.
- `/history` shows what the agent did, without message content.
- `/telegram` → **Disconnect** stops reading without deleting your settings. You can also end
  the session from Telegram: Settings → Devices.
- `/tasks` disconnects Jira or Trello; revoke the token on their side too.

## Running Scribe for other people

If you run Scribe for other people, you are responsible for their data under the laws that
apply to you. The access gate ([access-gate.md](access-gate.md)) asks applicants to agree to the
processing of their personal data before they send an application; you need your own privacy
notice for the service.
