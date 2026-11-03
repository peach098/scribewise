# Release notes and releases

After a deploy of a new version, every user with access gets one short "what's new" message
from the control bot, in their language and without a notification sound. A restart or a
redeploy of the same version sends nothing. Design: [`specs/011-release-notes/`](../specs/011-release-notes/).

## Writing notes

One file per version: `release-notes/<X.Y.Z>.md`, where `X.Y.Z` is the version in
`pyproject.toml`. Format ([contract](../specs/011-release-notes/contracts/release-notes-file.md)):

```markdown
<!-- Notes for reviewers: never sent. -->

## en

- You can now do X: press "Y".

## ru

- Теперь можно сделать X: нажмите «Y».
```

- **Audience: users.** Say what they can do now, in short sentences. No internal details,
  issue numbers, migrations or file names.
- **Plain text.** The section is sent exactly as written: no bold, links in brackets or
  headings. Use `-` or `•` for bullets.
- **Both languages.** A missing section is allowed (users of that language get the other one),
  but the repository test requires both.
- **At most 3,500 characters** per section. Before the first section only blank lines and
  `<!-- comments -->` are allowed.
- A release with nothing worth announcing has no file. It is still recorded as handled.

The message is `What's new in Scribe X.Y.Z` / `Что нового в Scribe X.Y.Z`, a blank line and
the section. When several versions are deployed at once, one message covers all of them,
oldest first.

## Release steps

1. Bump `version` in `pyproject.toml` (patch for fixes, minor for features).
2. Run `uv lock`. The Docker build uses `uv sync --locked` and fails on a stale lock (the unit
   test `tests/unit/test_release_notes_files.py` checks this too).
3. If the release is worth announcing, add `release-notes/<version>.md` in English and
   Russian, then check it: `python -m scribe.admin release-notes preview <version>`.
4. Merge, then optionally tag: `git tag v<version> && git push origin v<version>`.
5. Deploy as usual (`git pull`, `docker compose build`, `alembic upgrade head`, `up -d`).
   A few minutes later check `release-notes status`.

To deploy without announcing, set `SCRIBE_RELEASE_NOTES_AUTO=false` for that deploy. The
version still counts as handled only when it is announced, so after turning the setting back
on, the next start announces it. Send by hand instead when you are ready.

## Operator commands

Run inside the app container (`docker compose run --rm app python -m scribe.admin ...`):

| Command | What it does |
|---|---|
| `release-notes preview <version>` | The exact message of each language, plus warnings. Writes nothing. |
| `release-notes check` | Validates every file in `release-notes/`; exit 1 on an error. |
| `release-notes send <version> [--dry-run]` | Queues the version for every user with access who has not received it. The running service sends it within about a minute. `--dry-run` shows the counts and the text and writes nothing. |
| `release-notes status [--all]` | `running=`, `last_handled=`, `auto=`, then the latest (or every) announcement with counts per state. |

The CLI never contacts Telegram. Only the running service sends, so there is one sender and
one rate limit.

## Settings

| Env | Default | Meaning |
|---|---|---|
| `SCRIBE_RELEASE_NOTES_AUTO` | `true` | Announce a new version automatically at startup |
| `SCRIBE_RELEASE_NOTES_RATE_PER_S` | `10` | Messages a second for announcements, 1–25 |
| `SCRIBE_RELEASE_NOTES_MAX_ATTEMPTS` | `5` | Network or server errors before a delivery is `failed` |
| `SCRIBE_RELEASE_NOTES_DIR` | `release-notes` | Where the files are (relative to `/app` in the image) |

## Who gets it

- With `SCRIBE_ACCESS_GATE=true`: accounts with access (beta, subscription, operator) that are
  not suspended. Access is checked again right before each message.
- With the gate off: every account.
- Paused, out-of-office and still-setting-up users get it too. People without an account
  (refused senders, applicants) never do. Accounts created after the announcement do not.

## What happens when

- **Restart or redeploy of the same version:** nothing is sent. The newest announced version
  is stored in `release_announcements`.
- **First deploy with this feature:** only the running version's notes are sent, never older
  files.
- **Several versions at once:** one message covers every version with notes since the last
  announced one.
- **Rollback to an older version:** nothing is sent.
- **Telegram asks to wait ("retry after"):** the sender pauses and retries the same message.
- **User blocked the bot:** the delivery is `blocked`; the others go on.
- **Network or server error:** retried after 30 s, 2 min, 10 min, 30 min, then `failed`.
- **Crash while sending:** at most one message per crash has an unknown result. It counts as
  delivered (`unknown` in `status`) and is never resent, so nobody gets the same message twice.
- **Data rights:** `/my_data` shows how many release notes the user received, the export lists
  them, and `/delete_my_data` removes them. Logs never contain the text, chat ids or usernames.
