# Contract: Control Chat (Jira additions)

Extends `specs/001-telegram-reply-agent/contracts/control-chat.md` as implemented in
`tasks_command.py` / `commitments_command.py`. Callback data stays `<action>:<entity_id>[:<extra>]`,
≤ 64 bytes. Every callback that names an entity loads it scoped to the calling user.

## `/tasks`

Status view (connected):

```
🗂 Task source: Jira (self-hosted · jira.example.com · 11.3.2)
Projects: PAY, CORE, OPS   ·   Last check: 14:05   ·   Status: ✅ OK
[ Change projects ] [ Reconnect ] [ Disconnect ]
```

Error state: the existing `error` status line after repeated failures ("⚠️ Jira is not
answering — reconnect or check the server"); a certificate change shows
`⚠️ Certificate changed — reconnect to review it`.

| Button | callback | Effect |
|---|---|---|
| 🔌 Connect Trello / 🔌 Connect Jira | `tasks:connect` / `jira:connect` | start that provider's flow; when the other source is connected, confirm "Replace Trello with Jira?" (`jira:replace`) first |
| Jira Cloud / Self-hosted | `jdep:cloud` / `jdep:self` | FSM `jira_deployment` → `jira_url` |
| Trust this certificate | `jcert:<sha256[:16]>` | stores pinned cert, continues setup / clears `cert_changed` |
| 📋 Projects / toggle | `tasks:boards`, `tboard:<KEY>`, `tpage:<n>`, `tboard:done` | the existing picker, listing Jira projects; a 21st selection answers "Up to 20 projects"; done requires ≥ 1 and schedules the first comment scan |
| Disconnect | `tasks:disconnect` | existing; also deletes the user's `jira_comment_cursor` rows; Jira-sourced commitments stay (marked `source_unreachable`) |

FSM states (`JiraStates`, in order): `deployment` (buttons `jdep:cloud` / `jdep:self`) →
`site` → (`email` → `token`) | `pat` → [`cert`] → project picker. Messages containing a token are deleted
right after reading. Each validation error maps to one message:

| Error | Message |
|---|---|
| `JiraAddressInvalid` | "That doesn't look like a Jira address. Send it like https://jira.example.com" |
| `JiraUnreachable` (any network-level failure, including a blocked private address; the detail goes to the log only, issue #61) | "Scribe couldn't reach this server. Check the address: the server must answer over HTTPS from the internet (a server in a private network must be allowed by the Scribe operator)." |
| port outside `SCRIBE_JIRA_ALLOWED_PORTS` (default 443, 8443) | "Scribe connects to Jira only on these ports: <ports>. Send the address with one of them (no port means 443) or ask the Scribe operator." |
| more than 5 connection checks in 10 minutes | "Too many connection attempts. Try again in <minutes> min (/tasks)." |
| `JiraCertificateUntrusted` | certificate card (below) |
| `JiraVersionUnsupported` | "Jira <found> is not supported. Supported: <minimum> and newer." |
| `JiraAuthFailed` | "Jira rejected the token." + for `classic_token`: "Please create a scoped token with read:jira-work and read:jira-user." |
| `JiraRateLimited` / other | "Jira is not answering right now. Try again later." |

Certificate card:

```
🔐 This server uses a certificate your company issued itself.
Subject: CN=jira.example.com   Issuer: CN=Example Corp Issuing CA
Valid until: 2027-03-01   SHA-256: 3F:9A:…:C1
Only trust it if this matches what your IT team expects.
[ Trust this certificate ] [ Cancel ]
```

## `/commitments` (changes)

Each item shows its sources and automatic notes:

```
1. Fix on staging — to Anna — Wed 23 Oct
   📎 Jira PAY-142 comment 21 Oct ↗ · Telegram 20 Oct
2. Send logs — to Ivan — no date ("after the demo")
   📎 Jira CORE-77 comment 18 Oct ↗ (restricted)
Recently closed by Scribe:
3. Release notes — issue CORE-80 done   [ Reopen ]
4. Review PR — source comment removed  [ Restore ]
⚠️ OPS-12 is no longer reachable — 1 commitment kept as is
```

Buttons per open item unchanged (`cmtres` / `cmtedit` / `cmtdel`); new `cmtreopen:<commitment_id>`
for **Reopen** / **Restore** (only `closed_by=system`, within 30 days). `cmtreopen` goes
through the same callback ownership check as other entity callbacks: a commitment of another
user answers "Not found" and changes nothing.

## Draft card (changes)

- Sources: `Jira PAY-142 "Fix refund rounding" (due Fri)` (existing task label with the issue
  key as title prefix), deadline notes from comments as for Trello, commitments with
  `your Jira comment on PAY-142, 21 Oct`; restricted rows show `restricted comment on CORE-77`.
- New line for each `date_mismatch:<task ref>` warning:
  `📅 Dates differ: Fri 25 Oct (due date) · Wed 23 Oct (your Jira comment, 21 Oct)`.
- Outages use the existing `⚠️ drafted without task context` line and failure alert.
- Conflicts name the issue: `⚠️ conflicts with "PAY-142 Fix refund rounding" (due Fri)`.

## Notifications

- First comment scan finished: "Found N commitments in your Jira comments. /commitments to
  review." (plus "Only the 1000 most recently updated issues were scanned." when capped).

## `/my_data` (changes, when 001-T070 lands; until then `jira_data_summary()` provides the counts)

Adds lines: `Commitments from Jira: N`, `Jira comment markers: K (no content, kept 30 days)`.
Export includes Jira-sourced commitments and cursor rows (no HMAC) and the connection's
`config_json` without `pinned_cert`; never credentials.
