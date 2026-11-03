# Data Model: Release Notes for Users After an Update

Migration `0011_release_notes.py` (`down_revision = "0008"`) adds two tables and two enums.
Nothing existing changes. Models go in `src/scribe/db/models.py` under a
`# --- release notes (specs/011-release-notes)` section.

## Files (not in the database)

### Release notes file

`release-notes/<X.Y.Z>.md`, format in
[contracts/release-notes-file.md](contracts/release-notes-file.md). Parsed by
`scribe.release_notes.notes.load_notes(dir)` into:

| Field | Type | Rule |
|---|---|---|
| `version` | `Version` (`tuple[int, int, int]`) | From the file name; must match `^\d+\.\d+\.\d+$` |
| `texts` | `dict[Lang, str]` | Keys `en`, `ru`; stripped; a missing or empty section is absent |
| `warnings` | `list[str]` | e.g. `missing ru section` |

Errors (`NotesError`, which names the file): bad file name, text before the first section,
unknown or duplicate section, a section over `MAX_SECTION_CHARS = 3500`.

## Enums

- `release_trigger`: `auto`, `manual`.
- `release_delivery_state`: `pending`, `sending`, `sent`, `blocked`, `failed`, `skipped`,
  `unknown`.

## Table `release_announcements`

One decision to send notes: an automatic one at startup, or a manual send from the CLI.
Holds operator-written text only, no personal data.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `release_version` | text not null | `X.Y.Z`. For `auto`: the running version. For `manual`: the sent version. The max over all rows is the **last handled version** (research R3). |
| `versions` | text[] not null | Covered versions, ascending; may be empty (an `auto` row for a release without notes) |
| `notes_json` | JSONB not null | `{"0.2.0": {"en": "...", "ru": "..."}}`: a snapshot of the covered versions' texts |
| `trigger` | `release_trigger` not null | |
| `created_at` | timestamptz not null | |
| `finished_at` | timestamptz null | Set when no delivery is `pending` or `sending`; set at once when `versions` is empty or nobody is due |
| `recipients` | int not null | Deliveries created (snapshot size) |

Index: `uq_release_announcements_auto_version`, unique `(release_version)` WHERE
`trigger = 'auto'`. Two starts of the same version can never both announce it.

## Table `release_deliveries`

One recipient of one announcement. Holds no message text.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `announcement_id` | UUID FK → `release_announcements.id` ON DELETE CASCADE, not null | |
| `user_id` | UUID FK → `users.id` ON DELETE CASCADE, not null | Removed with `/delete_my_data` (FR-024) |
| `state` | `release_delivery_state` not null default `pending` | |
| `parts_total` | smallint null | Number of messages of this user's digest, set when the row is claimed (research R5) |
| `parts_sent` | smallint not null default 0 | Messages of this digest already accepted by Telegram (research R5) |
| `attempts` | smallint not null default 0 | Failed attempts for retryable errors (not counting "retry after") |
| `next_attempt_at` | timestamptz not null | `created_at` at first; moved by backoff |
| `delivered_at` | timestamptz null | Set with `sent` |
| `skip_reason` | text null | `no_access`, `no_account`, `already_received`, `no_text` |
| `error` | text null | Exception class name only, e.g. `TelegramForbiddenError` |
| `created_at`, `updated_at` | timestamptz | `_Timestamps` mixin |

Indexes:

- unique `(announcement_id, user_id)`: a user appears once per announcement.
- `ix_release_deliveries_due` on `(next_attempt_at)` WHERE `state = 'pending'`: the sender's
  claim query.
- `ix_release_deliveries_user` on `(user_id)`: "what did this user already receive" (R5),
  and the data export.

## State machine of a delivery

```text
            claim (service only)             last part accepted
 pending ───────────────────────► sending ─────────────────────► sent
    ▲                               │ │ │
    │ retryable error,              │ │ └─ Forbidden / chat not found ─► blocked
    │ attempts < max (backoff)      │ └─── BadRequest, or attempts = max ─► failed
    └───────────────────────────────┘
                                    │  pre-send check: no account / no access /
 pending ──────────────────────────────── already received / no text ──► skipped

 sending found at startup (crash): the part in doubt (index parts_sent) counts as sent;
                                   it was the last one ─► unknown
                                   more parts left     ─► pending (parts_sent + 1)
```

- "Retry after" keeps the row in `sending` while the sender waits, then retries the same
  part.
- `sent`, `blocked`, `failed`, `skipped` and `unknown` are final. A later manual send
  creates a new announcement. Its delivery rows cover only users who have not received the
  version, where `sent` and `unknown` count as received.
- When a delivery reaches a final state and the announcement has no `pending` or `sending`
  rows left, `finished_at` is set in the same transaction.

## Derived values

- **Last handled version**: `max(parse(release_version))` over `release_announcements`.
  None when the table is empty.
- **Received versions of a user**: the union of `versions` of announcements where the
  user's delivery is `sent` or `unknown`.
- **Personal versions** for a delivery: the announcement's `versions` minus the user's
  received versions from other announcements, ascending.
- **Announcement status** (CLI `status`): counts of deliveries by state, plus `recipients`,
  `created_at`, `finished_at`, `trigger` and `versions`.

## Settings (`scribe.config.Settings`)

| Setting | Env | Default | Rule |
|---|---|---|---|
| `release_notes_dir` | `SCRIBE_RELEASE_NOTES_DIR` | `release-notes` | Path; missing directory = no notes (warning at startup) |
| `release_notes_auto` | `SCRIBE_RELEASE_NOTES_AUTO` | `true` | FR-008 |
| `release_notes_rate_per_s` | `SCRIBE_RELEASE_NOTES_RATE_PER_S` | `10` | `1 <= x <= 25` (FR-013) |
| `release_notes_max_attempts` | `SCRIBE_RELEASE_NOTES_MAX_ATTEMPTS` | `5` | `>= 1` |

## i18n keys (`scribe/i18n/en.py`, `ru.py`)

| Key | en | ru |
|---|---|---|
| `release_notes.heading` | `What's new in Scribe {version}` | `Что нового в Scribe {version}` |
| `my_data.release_notes` | `Release notes received: {n}` | `Получено обновлений: {n}` |

## Data export (`retention/user_data.py`)

- `export_user_data(...)["release_notes"]`: a list of
  `{"versions": [...], "state": "...", "delivered_at": "..."}`, one item per delivery.
- `data_summary(...).release_notes`: an int (deliveries in `sent` or `unknown`).
