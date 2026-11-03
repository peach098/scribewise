# Security Review: Jira Task Source with Comment Commitments

**Date**: 2026-10-21 · **Scope**: `src/scribe/context/tasks/jira/`,
`src/scribe/context/comment_commitments.py`, `src/scribe/context/task_dates.py`,
`src/scribe/telegram/control_bot/jira_connect.py`, the `/tasks` and `/commitments` changes,
migration `0004`. **Method**: code reading plus the automated tests named below.

## Findings

| # | Area | Result | Evidence |
|---|---|---|---|
| 1 | GET only (SC-007, J3) | **Pass.** Every Jira request goes through `client.build_http_client`, whose request hook refuses anything but GET/HEAD before I/O. The only other client is the inner transport inside `netguard.GuardedTransport`, which receives requests already checked by that hook. No write verb appears in the package. | `test_jira_client_rejects_non_get`, `test_every_client_method_only_gets`, `test_jira_package_never_writes` |
| 2 | Redirects and proxies | **Pass.** `follow_redirects=False` (a 3xx is an error) and `trust_env=False`: a proxy taken from the environment would bypass the address guard. | `test_redirects_are_not_followed`, `test_build_http_client_ignores_proxy_environment` |
| 3 | SSRF (J4) | **Pass.** Before every request the host is resolved and every address checked: loopback, link-local (incl. metadata), private, CGNAT, ULA, multicast, unspecified and reserved addresses are refused unless the operator allowlists them. The connection then goes to the checked IP with the original name for SNI and Host, so DNS rebinding between check and connect is not possible. | `test_address_guard_blocks_private_and_metadata`, the netguard unit tests |
| 4 | Certificate pinning (J5) | **Pass.** A company-issued certificate is shown with its SHA-256 and trusted only on confirmation. The pinned context trusts that one certificate, not the system CAs, keeps hostname checking on, and refuses a changed certificate. Reading the certificate sends no application bytes. An unreadable stored PEM is a typed error, not a crash. | `test_pinned_cert_accepts_same_rejects_changed`, `test_pinned_cert_still_checks_hostname`, `test_self_hosted_certificate_confirmation` |
| 5 | Credentials | **Pass.** Stored only in `task_source_connections.credentials` (encrypted). The token message is deleted right after reading. A token waiting behind the certificate card lives only in the encrypted FSM data and is dropped by /cancel, any other command, or after 15 minutes. Disconnect deletes the connection and the comment cursors. Log redaction drops `api_token`, `pat`, `email`, `pem`, `pinned_cert`. Exception messages never contain tokens or URLs. | `test_connect_pick_projects_and_disconnect`, `test_other_command_drops_a_pending_token`, `test_jira_secrets_are_redacted`, `test_validate_error_mapping` |
| 6 | Token sent to a user-supplied host (fixed in review) | **Fixed.** The classic-token check, which retries `/myself` on the site address with the token, ran for any Cloud address. It now runs only on Atlassian hosts (`*.atlassian.net`, `*.jira.com`); other addresses never receive the token. | `test_classic_token_probe_only_on_atlassian_hosts` |
| 7 | Other people's text (Principle II, FR-J20/J21) | **Pass.** Only the user's own comment, with quotes removed, and at most one earlier comment by someone else are sent to the LLM. Display names only, no ids or emails. Stored: at most 280 characters of the evidence quoted from the user's own comment, encrypted; nothing for restricted comments. Cursors store an HMAC, not text. Task comments live only in the existing 60-second cache. | `test_other_peoples_comments_never_sent`, `test_restricted_comment_has_no_excerpt`, `test_no_comment_text_in_logs` |
| 8 | Prompt injection | **Pass.** All Jira text reaches the model inside delimited untrusted data blocks, with no tools, through the existing `extract.commitments` route. The model can only propose an item, which must quote the user's own words (`ground_item`) and can only become a commitment row shown to the user. It cannot send, change settings or call Jira. The date-mismatch warning is computed deterministically. | grounding tests in `test_jira_commitment_merge.py`; injection cases in `evals/threads/jira_comments.jsonl` |
| 9 | Ownership | **Pass.** `cmtreopen` loads the row scoped to the caller and only reopens rows Scribe closed. The Jira connect buttons act only on the caller's own FSM data and connection. | `test_cmtreopen_rejects_other_users_commitment`, `test_jira_connect_flow.py` |
| 10 | Stored warnings | **Pass.** `date_mismatch:` warnings are stored in the clear like other system warnings, so they hold dates, issue keys and source kinds only, with no names, titles or comment text. | `test_trello_comment_deadline_vs_due_field` |

## Accepted risks

- **Self-hosted PAT scope** (plan.md Complexity Tracking): Jira Data Center personal access
  tokens carry the user's full permissions. This is mitigated by the GET-only client (finding 1),
  encryption at rest, token deletion from the chat, disconnect, and the revoke instructions in
  setup and in the README.
- **No outbound proxy**: an operator who needs an HTTP proxy for Jira cannot use one today,
  because proxies are ignored on purpose (finding 2). It could be added later as an explicit
  setting whose proxy host is itself address-checked.
- **Key rotation re-extraction**: comment fingerprints are keyed from the current master key,
  so after a rotation each recent comment is checked once more. That is a cost, not a leak.
- **Watcher coverage**: comments on issues the user neither owns, reported nor watches are
  not read (spec Assumptions). That is a coverage limit, not a security one.
