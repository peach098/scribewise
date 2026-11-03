# Quickstart: Validate Group Mention-Only Intake

## Prerequisites

- Dev environment per `specs/001-telegram-reply-agent/quickstart.md` (uv, Postgres test
  container for integration tests).
- `uv run alembic upgrade head` applies `0009_drop_group_mode`.

## Automated checks

```bash
make lint typecheck
uv run pytest tests/unit/test_router.py tests/unit/test_user_client.py tests/unit/test_i18n_us34.py tests/unit/test_i18n_lint.py -q
uv run pytest tests/integration/test_us1_pipeline.py tests/integration/test_us4_account.py \
  tests/integration/test_us2_review_fixes.py tests/integration/test_earlier_search_drafts.py \
  tests/integration/test_jira_models.py tests/integration/test_models_and_repos.py -q
make test-fast test-integration
make eval-dry   # no draft-generation change; must stay green
```

Expected: everything passes. `grep -rn "GroupMode\|group_mode" src tests` finds nothing except
the migrations.

## Scenarios (map to spec)

| # | Scenario | Expected | Covered by |
|---|----------|----------|------------|
| 1 | Group message tagging the user by @username | `new_turn`, `reason=tagged`, draft after the delay | US1-1, unit + integration |
| 2 | Name-link tag (user without username) | Same as 1 | US1-2, unit (flag-driven) + live check |
| 3 | Reply to the user's message, no tag | `new_turn`, `reason=reply` | US1-3 |
| 4 | Plain group message | `store_only`, `reason=not_addressed`; later draft in that group sees it as history | US1-4/5, SC-004 |
| 5 | Chat that was `always` before upgrade | After the migration: plain message → no draft; tag → draft | US2-1/2, migration test |
| 6 | Forum group, post in a topic the user created, no reply, no tag | `store_only` | research R3 |
| 7 | Forward containing the user's @username | `store_only` | spec edge case |
| 8 | Old settings card: press 👥 Every message / 👥 Mentions only | Toast "This option is no longer available", nothing changes | US2-4, FR-007 |
| 9 | `/chats` → a group | Line "👥 Drafts in this group: only when you are tagged or replied to", no group-mode buttons (en and ru) | US2-3, SC-005 |
| 10 | Private chat message | Unchanged (`reason=private`) | FR-009 |

## Live check (manual, synthetic content, test accounts only)

On a test account in a test group, run scenarios 1, 2, 3, 4, 6 and 7. In the app logs, check
the `intake` lines: `action` and `reason` must match the table, and no text may appear. For
scenario 6, also record whether the server set `mentioned` (debug run of `message_to_incoming`).
If it did, apply the fallback described in research R3.
