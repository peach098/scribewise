# Quickstart: validating Telegram QR-Code Login

Proves the acceptance scenarios in [spec.md](spec.md). Screens, buttons and callbacks are
in [contracts/control-chat.md](contracts/control-chat.md); stored fields in
[data-model.md](data-model.md).

## Prerequisites

- Everything from `specs/001-telegram-reply-agent/quickstart.md` (bot token, API id/hash,
  Postgres, LLM key).
- On my.telegram.org, the app title for `SCRIBE_TG_API_ID` is set to **Scribe Agent**.
- Test account **A** with Telegram signed in on two devices (a phone to scan, plus the
  device showing the control chat). Test account **A2** with two-step verification on.
  Test account **B** (a different account) for the wrong-account checks.
- Optional settings (defaults shown):

```
SCRIBE_TG_QR_MAX_CODES=5
SCRIBE_TG_LOGIN_PASSWORD_TIMEOUT_S=600
SCRIBE_TG_QR_SAME_DEVICE_LINK=false
SCRIBE_TG_DEVICE_MODEL="Scribe Agent"
```

## Automated checks

```
make lint typecheck
make test-fast          # unit: QR loop with a fake client and clock, qr_image, masking
make test-integration   # connect flow with FakeLoginClient + real Postgres + migration 0002
```

Expected: all green; the integration tests cover every scenario below except the ones
marked *manual*.

## Scenarios

| # | Steps | Expected | Spec |
|---|---|---|---|
| 1 | New user: `/start` | Method choice; QR first, marked recommended | US1-1, FR-001 |
| 2 | Tap **Scan a QR code**; scan from account A's phone (Settings → Devices → Link Desktop Device) | ≤ 10 s: "Connected as … (@…)" + **This is not my account**, then chat picker. No phone or code asked. | US1-2/3, FR-004/006 |
| 3 | Start QR, do not scan for ~3 min | Image swaps 5 times ("Code n of 5"), then text "The code expired" + **Show a new code**; no image left in chat | US1-4, FR-005 |
| 4 | Start QR, tap **Use phone number instead**, then within 30 s scan the old code from a screenshot | Phone prompt shown; the late scan yields "That code was no longer active"; account A's Active sessions has no new Scribe entry | US1-5, FR-002/009, research Q4 (*manual*) |
| 5 | Account A2: scan, send a wrong password, then the right one | Password prompt; "Wrong password"; then connected; both password messages deleted | US2-1/2/3, FR-007/008 |
| 6 | Account A2: scan, wait 10 min | "The connection attempt timed out" + method choice | US2-4 |
| 7 | After 2, tap **This is not my account** | Logged out; Active sessions on A no longer lists Scribe; method choice shown | FR-012 |
| 8 | Connected user: terminate the "Scribe Agent" session from A's Active sessions | Control chat: disconnected notice with **Reconnect** | US3-1, FR-017 |
| 9 | Tap **Reconnect** → QR → scan with account A | "Reconnected as …"; `/chats`, `/settings`, `/profile` unchanged; a test message gets drafted | US3-2/3, SC-006 |
| 10 | Reconnect, scan with account B | "That's a different Telegram account…"; B's Active sessions has no Scribe entry | US3-4, FR-011, SC-005 |
| 11 | Second bot user tries to connect account A | "already connected to another Scribe account"; no session left on A | FR-013 |
| 12 | `/telegram` | Shows account, "via QR code", connection time, how to terminate | US4-1, FR-015 |
| 13 | Check Active sessions on A | Entry named "Scribe Agent" | US4-2, FR-016 (*manual*) |
| 14 | Start QR, restart the service before scanning | After start-up: "The service restarted…" + method choice; no `telegram_sessions` row was created | Edge case, research Q10 |
| 15 | Phone-and-code flow end to end | Works as in 001 | FR-003 |
| 16 | With `SCRIBE_TG_QR_SAME_DEVICE_LINK=true`: tap **Approve on this device** on iOS, Android, Desktop | Record per platform whether the login is approved; keep the flag off unless iOS and Android both work | FR-010, research Q5 (*manual*) |
| 17 | Connected user (paused): `/telegram` → **Disconnect** → confirm; then `/start`; then **Reconnect** by QR | After disconnect: Scribe gone from Active sessions, state still paused; `/start` answers "Use /telegram to reconnect" (no setup steps); after reconnect: still paused, chats/profiles unchanged | US3, research Q9 |
| 18 | While disconnected, colleague sends 3 messages; reconnect | "Reconnected as …" followed by "📥 3 messages arrived while you were disconnected…"; drafts follow | US3-3, FR-017 |

## Privacy check (SC-004)

After running scenarios 2–11:

```
docker compose logs scribe | grep -E 'tg://login|token=|\+[0-9]{7,}' ; echo "exit=$?"
```
Expected: no matches (`exit=1`). In Postgres, `activity_records` rows of kind
`telegram_login` contain only the keys listed in data-model.md.

## Timing (FR-006, plan Performance Goals)

During scenarios 2, 5 and 9, compare `login_step` log timestamps: `step=scanned` →
`step=finished` must be ≤ 10 s; consecutive `step=qr_refreshed` events must follow the
previous code's expiry by ≤ 2 s (scenario 3). Record the observed maximums.

## Metrics (SC-001 – SC-003)

Per-method success rate and median duration come from `activity_records` where
`kind = 'telegram_login'`, grouped by `meta_json->>'method'` and `meta_json->>'outcome'`.
