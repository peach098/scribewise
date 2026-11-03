# Quickstart: Release Notes for Users After an Update

How to check the feature end to end. Formats are in
[contracts/release-notes-file.md](contracts/release-notes-file.md) and
[contracts/operator-cli.md](contracts/operator-cli.md), and the tables in
[data-model.md](data-model.md).

## Automated checks

```bash
uv run pytest tests/unit/test_release_notes_files.py   # every file in release-notes/ is valid
uv run pytest tests/unit -k release_notes                # parser, versions, digest, pacing
uv run pytest tests/integration -k release_notes         # Postgres: announce, deliver, resume, CLI
uv run ruff check . && uv run mypy
```

The integration tests cover the acceptance criteria of issue #20 with the recording bot
from `tests/integration/bot_support.py`, `FrozenClock` and a fake `sleep`:

| Scenario | Test expectation |
|---|---|
| New version with notes, two users (en, ru) | One message each, in their language, silent, plain text |
| Restart with the same version | 0 messages, no new announcement |
| Version without notes | 0 messages, an `auto` row with empty `versions`, finished |
| Two versions with notes since the last one | One digest, oldest first |
| Rollback to an older version | 0 messages |
| Gate on: beta, subscriber, operator, suspended, refused, applicant | Only beta, subscriber and operator (with accounts) |
| Gate off | Every user with an account |
| Access lost between snapshot and send | `skipped` (`no_access`) |
| `RetryAfter(3)` on one send | Pause of 3+1 s (fake sleep), same part retried, `attempts` unchanged |
| Forbidden on one send | `blocked`, others delivered, log without text |
| Crash: a row left in `sending` | After `recover_interrupted`: `unknown`, not resent |
| Crash after part 1 of 2 | Part 2 sent after restart, part 1 not resent |
| Pace | 50 sends need at least 49 / rate seconds of fake sleep |
| Manual send of a version the digest covered | `nothing to send` |
| `/delete_my_data` | The user's deliveries are gone |

## Manual check on a local instance

Prerequisites: a working local setup (README "Quick setup") with two test accounts in the
bot, one with English and one with Russian UI.

1. Note the running version:
   `uv run python -m scribe.admin release-notes status` → `running=0.2.0 last_handled=- auto=on`.
2. Create `release-notes/0.2.0.md` like the example in the file contract, then preview it:
   `uv run python -m scribe.admin release-notes preview 0.2.0`. Both languages show, with no
   warnings.
3. Start the service: `uv run python -m scribe`. Within about 15 s each test account gets
   "What's new in Scribe 0.2.0" / "Что нового в Scribe 0.2.0", without a notification sound.
4. `release-notes status` shows `auto release=0.2.0 versions=0.2.0 recipients=2 sent=2`.
5. Restart the service. Nothing is sent, and `status --all` still shows one announcement.
6. Dry run: `release-notes send 0.2.0 --dry-run` → `0 recipients ..., 2 already have it`.
   Send by hand: exit 1, `nothing to send`.
7. Delete `release-notes/0.2.0.md`'s Russian section and run preview: a warning, and the
   Russian block shows the English text.
8. Set `SCRIBE_RELEASE_NOTES_AUTO=false`, bump the version to `0.2.1` (`pyproject.toml`,
   `uv lock`, `uv sync`), add `release-notes/0.2.1.md` and restart. Nothing is sent. Then
   `release-notes send 0.2.1`: both accounts get it within a minute.
9. With `SCRIBE_ACCESS_GATE=true`, remove one test account from the beta list
   (`beta remove`), wait for `access_reconcile`, then send a new version by hand. Only the
   other account gets it.

## Release steps (US5, FR-025)

To be copied into `docs/release-notes.md`:

1. Bump `version` in `pyproject.toml` (patch for fixes, minor for features).
2. Run `uv lock`. The Docker build uses `uv sync --locked` and fails on a stale lock.
3. If the release is worth announcing, add `release-notes/<version>.md` in English and
   Russian, then run `release-notes preview <version>`. Leave it out otherwise.
4. Merge, then optionally tag: `git tag v<version> && git push origin v<version>`.
5. Deploy as usual (`git pull`, `docker compose build`, `alembic upgrade head`, `up -d`).
   Check `release-notes status` a few minutes later.
