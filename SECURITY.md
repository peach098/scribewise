# Security policy

Scribe signs in to Telegram as you and reads your work chats, so we treat security reports as
the top priority.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Report it privately through
GitHub: **Security → Report a vulnerability** on this repository
([direct link](https://github.com/peach098/scribewise/security/advisories/new)).

Include what you found, how to reproduce it, and what an attacker could do with it. You will
get a reply within 7 days. Once a fix is released we credit you in the advisory, unless you
prefer to stay anonymous.

## Supported versions

Only the latest release on `main` gets security fixes. Self-hosted installs should update
regularly (see [Upgrading](docs/self-hosting.md#upgrading)).

## In scope

- Access to another user's Telegram account, drafts, messages or settings through the control
  bot (for example, tampered callback data or a missing ownership check)
- Sending a message without the user's approval
- Prompt injection that bypasses the injection warning and makes a draft act on attacker
  instructions without the card saying so
- Leaks of secrets or message content: logs, error messages, the health endpoint, exports
- Weaknesses in at-rest encryption (`src/scribe/crypto.py`)
- SSRF through the Jira connection (`src/scribe/context/tasks/jira/netguard.py`)
- Write access to Jira or Trello, which Scribe must only read

## Out of scope

- Problems that need an attacker to already control the server, the `.env` file or the
  database together with `SCRIBE_MASTER_KEY`
- What an LLM provider does with data you chose to send to it
  (see [Privacy and data](docs/privacy.md))
- Missing hardening on the landing page (`landing/`), which is static HTML

## How Scribe protects your data

A short summary; details are in [docs/privacy.md](docs/privacy.md).

- Message texts, drafts, style profiles, commitments, chat titles and contact names,
  Telegram session strings and integration tokens are encrypted at rest with AES-256-GCM under
  your own `SCRIBE_MASTER_KEY`. Each value is bound to its table and column, and a Telegram
  session to its user, so a value copied elsewhere in the database does not decrypt.
- The master key can be rotated without losing data: old keys keep decrypting while
  `python -m scribe.jobs reencrypt_all` moves every value to the new key (see
  [Rotating the master key](docs/configuration.md#rotating-the-master-key)). Rotate it if you
  suspect it leaked.
- Stored messages and draft texts are deleted after 30 days, chats and contacts no longer in
  use after 90 days, and a Jira or Trello token from an unfinished connection after 15
  minutes; `/delete_my_data` removes everything and logs the Telegram session out.
- The bot refuses to start open to everyone unless the operator says so
  (`SCRIBE_OPEN_BOT=true`); the default `.env.example` turns the access gate on.
- Nothing is sent from your account without your explicit approval of that message; a
  database constraint backs this up.
- Messages from contacts reach the model only as untrusted data.
- Jira and Trello are read-only; Jira requests go through an SSRF guard, allowed ports only.
- Logs contain no message content or tokens at any `SCRIBE_LOG_LEVEL`; only the opt-in
  `SCRIBE_LOG_LIBRARIES_DEBUG` lets libraries log content, for troubleshooting.
