# Contract: Operator CLI `release-notes` commands

This extends `python -m scribe.admin` (contracts of specs/005). It runs inside the app
container with the service's `SCRIBE_*` environment:

```
docker compose run --rm app python -m scribe.admin release-notes <command> ...
```

Exit codes follow the existing CLI: `0` success, `1` not found / nothing to do / invalid
file (message on stderr), `2` usage error. The output never shows names, usernames, chat ids
or chat content. The notes text is shown only by `preview` and `send --dry-run`.

`<version>` is `X.Y.Z`. Anything else is a usage error (exit 2).

| Command | Effect | Output |
|---|---|---|
| `release-notes preview <version>` | Reads `release-notes/<version>.md` and validates it. Writes nothing. | For `en` then `ru`: a `--- en ---` line, then the exact message a user of that language would get, including the heading and any fallback. Then the warnings, one per line, prefixed `warning:`. If the version is newer than the running one: `note: announced automatically when <version> runs`. Exit 1 with the error when the file is missing or invalid. |
| `release-notes check` | Validates every file in the directory | One line per file: `<version> ok` or `<version> warning: ...`; errors go to stderr. Exit 1 if any file has an error. |
| `release-notes send <version> [--dry-run]` | Creates a `manual` announcement for exactly `<version>` with `pending` deliveries for every user with access now (research R4) who has not received `<version>` yet. The running service sends them within about 15 s. | `queued <version>: <N> recipients (en <a>, ru <b>), <k> already have it`, then `the running service sends this within about a minute`. Exit 1 with `nothing to send` when N = 0, or with the error when the file is missing, invalid or has no text. |
| `... --dry-run` | Same computation, rolled back. Nothing is written or sent. | `dry run <version>: <N> recipients (en <a>, ru <b>), <k> already have it`, then the preview of both languages. |
| `release-notes status [--all]` | The latest announcement, or all of them, newest first, with `--all` | Per announcement: `<created YYYY-MM-DD HH:MM> <auto\|manual> release=<X.Y.Z> versions=<a,b or -> recipients=<n> sent=<n> pending=<n> blocked=<n> failed=<n> skipped=<n> unknown=<n> finished=<YYYY-MM-DD HH:MM or ->`. Prints `no announcements` when there are none. |

Also shown: `release-notes status` prints, as the first line, `running=<X.Y.Z> last_handled=<X.Y.Z or -> auto=<on|off>`.
The running version is the CLI's package version, which matches the service when both run
from the same image.

## Settings

| Env | Default | Meaning |
|---|---|---|
| `SCRIBE_RELEASE_NOTES_AUTO` | `true` | Announce automatically at startup (FR-008) |
| `SCRIBE_RELEASE_NOTES_RATE_PER_S` | `10` | Overall send rate for announcements, 1-25 |
| `SCRIBE_RELEASE_NOTES_MAX_ATTEMPTS` | `5` | Attempts for retryable errors before `failed` |
| `SCRIBE_RELEASE_NOTES_DIR` | `release-notes` | Where the notes files are (relative to `/app` in the image) |
