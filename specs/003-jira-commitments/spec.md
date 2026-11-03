# Feature Specification: Jira Task Source with Comment Commitments

**Feature Branch**: `003-jira-commitments` (renumbered from 002: `specs/002-telegram-qr-login` exists on main)

**Created**: 2026-10-21

**Status**: Draft

**Input**: User description: "Add Jira integration with feature to find commitments not only in task dates but also in comments"

## Clarifications

### Session 2026-10-21

- Q: Which Jira deployments must be supported? → A: Both Jira Cloud and self-hosted Jira
  (Data Center / Server) are supported in this feature, with the same behavior for issues,
  comment commitments and conflicts.

### Revision 2026-10-21 (rebased on issue #6, Trello comment deadlines)

The base branch already resolves a task's *effective deadline* from its due field, its recent
comments and its description (issue #6) for any task source that supplies them, and it already
has a task-source failure counter with a one-time alert and the "drafted without task context"
label. This spec now reuses both:

- FR-J06a added: Jira issues supply comments, description and due-date changes, so deadlines
  agreed in Jira comments (by anyone) count exactly as they do for Trello.
- FR-J19 and the "Jira unavailable" edge case now follow the existing task-source failure
  behavior instead of a separate one-hour rule.

## Context

Feature 001 (Telegram Work Reply Agent) defines a pluggable task source (FR-019): Trello in
the MVP, Jira required for the release version. Drafts use task status and due dates, plus
commitments extracted from the user's own Telegram messages (FR-031), to answer status
questions and flag conflicts.

In Jira, many real promises never reach the due-date field. They live in comments: "I'll
push the fix by Wednesday", "will send the logs after the demo", "OK, I'll take this one
before the release". This feature connects Jira as a task source and also reads issue
comments, so promises the user made in Jira count as commitments, the same as promises
made in chat.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Connect Jira and use its issues as task context (Priority: P1)

The user connects their Jira account from the control chat, picks the projects that matter,
and from then on drafts use their open Jira issues (title, status, due date, assignee,
link) the same way they already use Trello cards: to answer "when will X be ready?"
questions and to flag conflicts with due dates.

**Why this priority**: Jira is a required task source for the release (feature 001,
FR-019), and comment commitments (Story 2) need a working Jira connection first.

**Independent Test**: Connect a test Jira site, select one project with an issue
"PAY-142 Fix refund rounding" assigned to the user and due Friday. Have a colleague ask in
Telegram "when will the refund fix be done?" and check that the draft says Friday and lists
PAY-142 as a source with a link.

**Acceptance Scenarios**:

1. **Given** a user with no task source, **When** they run the task-source setup and choose
   Jira, **Then** they are asked whether their Jira is cloud-hosted or self-hosted, then for
   the site address and the access credentials that type of Jira uses; the connection is
   checked before it is saved, and they see the list of projects they can pick from.
2. **Given** a user whose company runs its own Jira, **When** they enter its address and
   their personal access credentials, **Then** the connection works the same way as for
   cloud Jira, and every later scenario in this feature behaves identically.
3. **Given** a connected Jira with selected projects, **When** a draft is prepared for a
   question about an open issue assigned to the user, **Then** the draft uses the issue's
   status and due date, and the review card lists the issue (key, title, link) as a source.
4. **Given** a Jira issue with no due date, **When** a related question comes in, **Then**
   the draft does not invent a date; it uses a placeholder or a date from a known
   commitment (Story 2).
5. **Given** credentials that are invalid or have been revoked, **When** the agent tries to
   read Jira, **Then** the user is told in the control chat, and drafts are labeled
   "drafted without task context" until the connection is fixed.
6. **Given** a connected Jira, **When** the user disconnects it, **Then** the stored
   credentials are deleted and Jira issues are no longer used as context.

---

### User Story 2 - Find my promises in Jira comments (Priority: P1)

The agent reads comments on the user's Jira issues and picks out promises the user made
there: what they promised, to whom, and by when. These become commitments next to the
ones taken from Telegram messages. They show up in `/commitments` with a link to the
comment and are used when drafting replies and checking for conflicts.

**Why this priority**: This is what makes the feature worth building. Due dates alone miss
most real promises, and a draft that ignores "I said Wednesday in the ticket" contradicts
the user in front of the colleague who is following up.

**Independent Test**: On issue PAY-142 (no due date), post a comment as the user: "@Anna
I'll have the fix on staging by Wednesday". Wait for the next Jira check. Verify that
`/commitments` lists "fix on staging — to Anna — Wed" with a link to the comment. When
Anna asks in Telegram "is the refund fix coming this week?", check that the draft says
Wednesday and cites the Jira comment as its source.

**Acceptance Scenarios**:

1. **Given** a comment by the user on an issue in a selected project that promises a
   deliverable and a date, **When** the agent next checks Jira, **Then** a commitment is
   created with what, to whom (the addressee or, if none, the issue reporter), by when,
   and a link to the source comment.
2. **Given** a comment by the user that promises something without a date ("I'll look into
   it"), **When** it is processed, **Then** a commitment without a due date is created
   only if the promise is clear; vague remarks create nothing.
3. **Given** a comment by someone else asking the user for something ("@me can you do this
   by Friday?") and a reply comment from the user accepting it ("sure"), **When** they are
   processed, **Then** a commitment is created from the user's acceptance, using the date
   from the request.
4. **Given** a comment by someone else that the user has not accepted, **When** it is
   processed, **Then** no commitment is created (other people's requests alone are not the
   user's promises).
5. **Given** the user edits a comment and changes the promised date, **When** the agent
   next checks Jira, **Then** the existing commitment is updated to the new date rather
   than a second one being created.
6. **Given** the user deletes a comment, **When** the agent next checks Jira, **Then** the
   commitment taken from it is marked resolved with the note "source comment removed", and
   the user can restore it.
7. **Given** a Jira-sourced commitment, **When** the user opens `/commitments`, **Then** it
   is shown with its source (issue key and comment link) and can be edited, resolved, or
   deleted like any other commitment.

---

### User Story 3 - Catch mismatches between dates and promises (Priority: P2)

The agent compares the different places a date can come from: the issue's due date, the
user's promises in Jira comments, and the user's promises in Telegram. When they disagree,
or when an incoming request clashes with one of them, the review card says so, and the
draft never picks one date without showing the user.

**Why this priority**: Conflicts are where a wrong draft does the most damage. It builds on
Stories 1 and 2 and adds safety, but the feature is already useful without it.

**Independent Test**: Issue PAY-142 has due date Friday. The user commented "fix by
Wednesday". Anna asks in Telegram "can I count on the fix by Thursday?". Verify that the
review card shows a mismatch warning ("Jira due Fri · you promised Wed in a PAY-142
comment") and that the draft text follows the user's latest promise (Wednesday) and cites
it. When Jira cannot be read, the draft is labeled "drafted without task context" and still
uses the stored promise.

**Acceptance Scenarios**:

1. **Given** an issue whose due date differs from a date the user promised in a comment on
   it, **When** a draft about that issue is prepared, **Then** the card shows both dates and
   their sources, and the draft uses the most recent promise by the user.
2. **Given** a commitment from Telegram and one from a Jira comment that describe the same
   promise, **When** commitments are listed or used for a draft, **Then** they are shown as
   one commitment with both sources, not as two.
3. **Given** an incoming request for a date already taken by a Jira-sourced commitment or
   due date, **When** a draft is prepared, **Then** the card highlights the conflict and
   names the issue.
4. **Given** a commitment whose issue has moved to a done status, **When** the agent next
   checks Jira, **Then** the commitment is marked resolved automatically, with an "issue
   done" note and the option to reopen it.

---

### Edge Cases

- **Jira unavailable or rate-limited**: drafts continue with the stored Jira-sourced
  commitments and are labeled "drafted without task context"; after repeated failures the
  connection shows an error in `/tasks` and the user gets one alert per failure streak (as
  for Trello). Rate-limit waits are honored before the next check.
- **Very long comment threads or many issues**: only comments inside the lookback window
  (see FR-J07) are read; the first scan after connecting covers the last 30 days.
- **Comments in other languages**: commitments are recognized in any language the user
  writes in, the same as for Telegram messages.
- **Relative dates** ("tomorrow", "end of week", "after the release"): resolved against the
  comment's posting time and the user's time zone; dates tied to events with no known date
  ("after the release") are stored without a due date and keep the original wording.
- **Comment quoting someone else**: quoted text inside the user's comment is not treated as
  the user's own promise.
- **Restricted comments** (visible only to some groups): treated like any other comment the
  user can see; they are never quoted back in a Telegram draft, only summarized as "see
  restricted comment on PAY-142".
- **Issue moved to a project that is not selected, or user loses access**: its commitments
  stay in `/commitments`, are marked "source no longer reachable", and are not
  auto-resolved.
- **Comment contains instructions aimed at the agent** (prompt injection): treated as plain
  data; it can never change agent behavior, trigger sending, or create anything except a
  commitment record shown to the user.
- **Self-hosted Jira not reachable from the agent** (for example, only inside the company
  network): the connection check fails with a message saying the server could not be
  reached and that the agent must be able to reach it; nothing is saved.
- **Self-hosted Jira with a company-issued security certificate**: the user can confirm
  the server's certificate once during setup; the connection is refused if the certificate
  later changes, until the user confirms the new one.
- **Self-hosted Jira that does not support the requested access type or is too old**:
  setup explains which Jira versions are supported and does not save the connection.
- **User switches from Trello to Jira**: Trello connection and board selection are removed;
  commitments from Telegram are unaffected.

## Requirements *(mandatory)*

### Functional Requirements

**Connection**

- **FR-J00**: System MUST support both Jira Cloud and self-hosted Jira (Data Center /
  Server). All requirements in this feature apply equally to both, unless a requirement
  names one of them.
- **FR-J01**: Users MUST be able to connect, check the status of, and disconnect a Jira
  account from the control chat, as an alternative to Trello. One task source is active per
  user at a time.
- **FR-J02**: System MUST check the Jira connection before saving it and show a clear,
  user-friendly error that tells apart a wrong address, an unreachable server, an untrusted
  certificate, an unsupported Jira version, and wrong credentials.
- **FR-J02a**: For self-hosted Jira, setup MUST let the user trust a company-issued
  certificate for that server, and MUST refuse to connect if the trusted certificate
  changes until the user confirms the new one.
- **FR-J03**: Users MUST be able to choose which Jira projects the agent reads (up to 20),
  and change that choice later. Only issues in selected projects are read.
- **FR-J04**: Access to Jira MUST be read-only: the agent never creates, edits, comments
  on, transitions, or assigns issues. Where Jira lets the user restrict the access level,
  setup MUST ask for the narrowest read-only access.
- **FR-J05**: Jira credentials MUST be stored encrypted and deleted on disconnect or account
  closure.

**Issues as tasks**

- **FR-J06**: System MUST treat open issues in selected projects that are assigned to the
  user as tasks (key, title, status, due date, assignee, reporter, link), usable as draft
  context and as sources, consistent with feature 001 FR-019 and FR-006.
- **FR-J06a**: Each Jira task MUST carry its recent comments, its description and the time its
  due date was last changed, so the effective deadline of a Jira issue is found in its
  comments and description exactly as for Trello cards (issue #6): a newer deadline agreed in
  a comment wins over the due field, and the card names the comment it came from.

**Comment commitments**

- **FR-J07**: System MUST read comments on issues in selected projects that the user is
  assigned to, reported, or watches, posted or edited in the last 30 days. Commenting on an
  issue makes the user a watcher under Jira's default settings, so this covers issues the
  user has commented on. Issues done for more than 30 days are skipped.
- **FR-J08**: System MUST extract commitments (what, to whom, by when, source comment) from
  the user's own comments, and from the user's comments that accept a request made by
  someone else in the same issue. Requests by others that the user did not accept MUST NOT
  create commitments.
- **FR-J09**: Only commitments the system is confident about MUST be stored, using the same
  confidence threshold as Telegram commitments (feature 001, FR-031).
- **FR-J10**: Relative dates in comments MUST be resolved against the comment's posting
  time in the user's time zone; when no date can be determined, the commitment is stored
  without a due date and keeps the original wording.
- **FR-J11**: Editing a source comment MUST update its commitment; deleting it MUST mark the
  commitment resolved with a restorable "source comment removed" note.
- **FR-J12**: When an issue moves to a done status, System MUST mark commitments sourced
  from that issue as resolved, with an "issue done" note and an option to reopen.
- **FR-J13**: New and changed comments MUST be picked up within 15 minutes of being posted
  while the agent is running and Jira is responding normally (not rate-limiting or
  unavailable; those cases are covered by FR-J19).
- **FR-J14**: Jira-sourced commitments MUST appear in `/commitments` with their source
  (issue key, link to the comment) and support the same edit, resolve and delete actions as
  other commitments. A user-edited or user-deleted commitment MUST NOT be recreated or
  overwritten by a later Jira check.

**Drafting and conflicts**

- **FR-J15**: Drafts MUST use Jira-sourced commitments as context alongside tasks and
  Telegram commitments, and MUST cite the issue and comment when they rely on one.
- **FR-J16**: When an issue's due date and the user's promised date for it differ, the
  review card MUST show both with their sources, and the draft MUST use the user's most
  recent promise.
- **FR-J17**: System MUST merge commitments that describe the same promise from different
  sources (Telegram and Jira) into one commitment with all its sources.
- **FR-J18**: Conflicts between an incoming request and Jira due dates or Jira-sourced
  commitments MUST be highlighted on the review card, naming the issue (feature 001,
  FR-008).
- **FR-J19**: When Jira cannot be read, drafts MUST still use the stored Jira-sourced
  commitments and carry the existing "drafted without task context" label; failed comment
  checks count toward the same task-source failure streak as failed task loads, so the user
  gets the existing one-time alert and the error status in `/tasks`.

**Privacy and safety**

- **FR-J20**: System MUST NOT store full comment threads. It keeps only extracted
  commitments with a short excerpt of the source comment (encrypted), and drops the excerpt
  30 days after the commitment is resolved or deleted, as for Telegram commitments.
- **FR-J21**: Only the comments needed to understand the user's promise (the user's
  comment and the request it answers) MUST be sent for extraction; other people's comments
  are not otherwise processed or retained.
- **FR-J22**: Text from Jira issues and comments MUST be treated as untrusted input and
  MUST NOT be able to change agent instructions, trigger actions, or bypass approval.
- **FR-J23**: Each Jira check MUST be logged (time, number of issues and comments read,
  commitments created/updated/resolved, errors) without comment content; Jira-sourced
  commitments MUST be included in `/my_data` counts and export.

### Key Entities

- **Jira Connection**: The user's link to a Jira site: deployment type (cloud /
  self-hosted), site address, trusted certificate for self-hosted servers if any, encrypted credentials,
  selected projects, status (active / error / disconnected), time of last successful check.
- **Jira Issue (as Task)**: An issue read live from Jira: key, title, status, due date,
  assignee, reporter, link. Not stored beyond short-lived caching.
- **Commitment** (extended from feature 001): a promise the user made, now with one or more
  **Commitment Sources**. Gains a note for automatic state changes ("issue done", "source
  comment removed", "source no longer reachable") and a flag that the user changed it by
  hand.
- **Commitment Source**: Where a commitment was found: a Telegram message or a Jira
  comment (issue key, comment link, comment posting/edit time, short encrypted excerpt).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A user who has Jira credentials at hand connects Jira (cloud or self-hosted)
  and picks projects in under 5 minutes, without help.
- **SC-002**: On the evaluation set of synthetic Jira comment threads, at least 85% of the
  user's clear promises are found as commitments, and at most 10% of stored commitments are
  false (not actual promises by the user).
- **SC-003**: Promised dates are correct (after resolving relative dates) for at least 95%
  of found commitments that have a date.
- **SC-004**: In evaluation threads where a Jira due date and a comment promise disagree,
  100% of review cards show the mismatch, and no draft states a date without citing its
  source.
- **SC-005**: A new or edited promise in a Jira comment is reflected in `/commitments` and
  in new drafts within 15 minutes while Jira is responding normally.
- **SC-006**: The same promise made in both Telegram and Jira shows up as one commitment in
  at least 90% of evaluation cases.
- **SC-007**: Zero writes to Jira in all tests and in production logs.
- **SC-008**: SC-002 to SC-006 hold for both cloud and self-hosted Jira; results on the two
  differ by no more than 5 percentage points.

## Assumptions

- Self-hosted Jira means Data Center and Server versions still supported by Atlassian at
  planning time; older versions are out of scope. The exact minimum version is set in the
  plan.
- A self-hosted Jira must be reachable from where the agent runs. Making a server that is
  only reachable inside a company network (for example, behind a VPN) reachable is up to
  the user's company; the agent does not provide a connector for this.
- Users of self-hosted Jira connect with a personal access token or an equivalent personal
  credential that their Jira allows; shared admin accounts are not used.
- Each user connects with their own Jira account, so the agent sees exactly what the user
  can see and nothing more.
- One task source per user at a time (Trello or Jira), as in feature 001's data model;
  using both at once is out of scope for this feature.
- "Selected projects" plus "assigned / reported / watched by the user" is enough to find
  the user's promises; mentions of the user on unrelated issues are out of scope.
- A user who turned off automatic watching in their Jira profile and comments on an issue
  they neither own nor reported is not covered; setup tells the user about this. Scanning
  every issue in the selected projects instead would read far more of other people's data
  (Principle II).
- The agent checks Jira for new comments on a schedule rather than being notified by Jira,
  because notifications would need Jira admin setup that a typical user cannot do.
- Jira issue descriptions and work-log entries are not scanned for commitments in this
  feature; only comments and the due-date field.
- The Trello task source and Telegram commitment extraction from feature 001 (US3) exist or
  are built first; this feature reuses their commitment list, conflict flags and source
  display.
- Project constitution v1.0.0 applies, in particular Principle II (data minimization),
  Principle III (no fabrication) and the prompt-injection rule.
