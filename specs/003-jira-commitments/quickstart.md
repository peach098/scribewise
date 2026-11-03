# Quickstart: Validate Jira Task Source with Comment Commitments

Validation guide for this feature. Setup of the service itself is in the repository
`README.md`; interfaces are in [contracts/](contracts/), entities in
[data-model.md](data-model.md).

## Prerequisites

- Base branch: feature 001 US3 + issue #6 (Trello comment deadlines) + main — this feature
  reuses the task service, the comment-deadline logic, commitment extraction and `/commitments`.
- Migration `0004_jira_commitments` applied:
  `uv run alembic upgrade head`.
- For live checks: a Jira Cloud test site **and** a self-hosted Jira (Data Center 10.3 or
  11.3, e.g. Atlassian's Docker image with a timebomb licence) with a test user, one project
  `PAY`, and a self-signed certificate on the self-hosted instance.
- Self-hosted Jira on a private address: add it to `SCRIBE_JIRA_ALLOWED_PRIVATE_HOSTS`.

## Automated checks (no Jira, no LLM spend)

```bash
make lint typecheck
uv run pytest tests/unit -k "jira or netguard or every_schedule or reconcile"
uv run pytest tests/contract -k jira          # respx fixtures for cloud + self-hosted, GET-only guard
uv run pytest tests/integration -k jira       # Postgres + fake LLM + respx
make eval-dry                                  # includes --suite jira with canned outputs
```

Key tests (names are the contract):

| Test | Proves |
|---|---|
| `test_jira_client_rejects_non_get` | SC-007: no write can leave the process |
| `test_flavors_same_scenarios[cloud\|self_hosted]` | FR-J00 / SC-008: same results for both |
| `test_address_guard_blocks_private_and_metadata` | J4 SSRF protection |
| `test_pinned_cert_accepts_same_rejects_changed` | FR-J02a |
| `test_validate_error_mapping` | FR-J02: five distinct errors |
| `test_own_promise_creates_commitment_with_link` | US2-1 |
| `test_unaccepted_request_creates_nothing` / `test_accepted_request_uses_request_date` | US2-3/4 |
| `test_comment_edit_updates_not_duplicates` / `test_comment_delete_resolves_restorable` | US2-5/6 |
| `test_user_modified_not_overwritten` | FR-J14 |
| `test_issue_done_auto_resolves` / `test_unreachable_keeps_open` | FR-J12, edge case |
| `test_telegram_and_jira_merge` | FR-J17 |
| `test_to_whom_falls_back_to_reporter` | FR-J08 |
| `test_comment_picked_up_within_two_runs` | FR-J13 / SC-005 |
| `test_telegram_promise_mismatch` | FR-J16 (Telegram-only promise) |
| `test_creopen_rejects_other_users_commitment` | callback ownership |
| `test_date_mismatch_on_card` | US3-1, SC-004 |
| `test_jira_comment_deadline_wins` | FR-J06a (issue #6 logic on Jira) |
| `test_jira_outage_uses_stored_commitments` | FR-J19 |
| `test_no_comment_text_in_logs` | FR-J23, Principle II |

## Quality gate (costs money)

```bash
uv run python -m evals.run --suite jira          # recall/precision/date accuracy/merge per flavor
uv run python -m evals.run                       # existing suites, incl. us3_commitments (prompt v3)
```

Must meet `evals/thresholds.yml`: `jira.recall ≥ 0.85`, `jira.precision ≥ 0.90`,
`jira.date_accuracy ≥ 0.95`, `jira.merge_rate ≥ 0.90`, `jira.flavor_gap ≤ 0.05`.

## Manual end-to-end (run once per deployment type)

1. **Connect (US1)** — `/tasks` → Jira → Cloud *or* Self-hosted → address → token.
   Self-hosted with a self-signed cert: the certificate card appears; compare the SHA-256 with
   `openssl s_client -connect jira.example.com:443 </dev/null | openssl x509 -noout -fingerprint -sha256`;
   trust it. Pick `PAY`. Expect: token message deleted, "Scanning comments…", then status ✅.
   Time it: under 5 minutes (SC-001).
2a. **Comment deadline** — as another Jira user comment "deadline moved to Nov 5" on PAY-142.
   The next draft about it uses 5 Nov and the card names that comment (issue #6 behavior).
2. **Wrong inputs** — repeat with a wrong token, `https://10.0.0.5` (not allowlisted), and an
   unreachable host. Expect the three different messages from contracts/control-chat.md.
3. **Task context** — issue `PAY-142` assigned to you, due Friday. From a second Telegram
   account ask "when will the refund fix be done?". Expect a draft mentioning Friday with
   source `Jira PAY-142 … (due Fri)`.
4. **Comment promise (US2)** — comment on PAY-142 as yourself: "@Anna I'll have the fix on
   staging by Wednesday". Within 15 minutes `/commitments` shows "fix on staging — to Anna —
   Wed" with a link (SC-005). Edit the comment to "by Thursday" → same item, Thursday. Delete
   it → item under "Recently closed by Scribe" with **Restore**.
5. **Accepted request** — as another Jira user comment "@you can you send the logs by
   Friday?"; reply as yourself "sure". Expect a commitment "send the logs — Friday". A request
   you don't answer creates nothing.
6. **Mismatch (US3)** — PAY-142 due Friday, your comment "fix by Wednesday". Ask in Telegram
   "can I count on the fix by Thursday?". Expect `📅 Dates differ: Jira due Fri · you promised
   Wed` on the card and the draft using Wednesday with the comment as source.
7. **Merge** — promise the same thing in Telegram ("I'll send the logs Friday") and in a Jira
   comment. Expect one item with two sources.
8. **Done issue** — move PAY-142 to Done. Next check: its commitments appear under "Recently
   closed by Scribe — issue done" with **Reopen**.
9. **Outage** — stop the self-hosted Jira (or revoke the token). Drafts show "⚠️ drafted
   without task context" and still cite stored Jira promises; after repeated failures `/tasks`
   shows the error and one alert is posted; the next success clears it.
10. **Read-only proof** — check the Jira audit log / access log: only GET requests from the
    Scribe user (SC-007).
11. **Disconnect** — `/tasks` → Disconnect. `/my_data` shows no Jira connection; Jira-sourced
    commitments remain, marked unreachable.

## Known limitations

- Issues you only commented on are found through Jira's watcher list (FR-J07); if you turned off
  autowatch in your Jira profile, comments on issues you neither own nor reported are missed.
- Self-hosted Jira must be reachable from the Scribe server; Scribe does not tunnel into
  company networks.
- Jira Cloud accepts only scoped read-only tokens; self-hosted PATs cannot be limited to
  read-only by Jira, so Scribe enforces read-only itself (GET-only client).
