# Feature Specification: Release Notes for Users After an Update

**Feature Branch**: `011-release-notes`

**Created**: 2026-10-23

**Status**: Draft

**Implements**: GitHub issue #20, "Send
release notes to all users when the server version is updated".

**Builds on**:

- `specs/005-beta-subscription-access`: the access gate decides who counts as a user with
  access (beta, subscription or operator), and suspended users are left out. Refused
  senders and people with only a beta application have no account, so they are never
  recipients.
- The operator CLI `python -m scribe.admin` (specs/001, extended in specs/005): the new
  `release-notes` commands live there.
- The control bot and its two UI languages (issue #8): every user has a stored language,
  `en` or `ru`.

**Input**: User description: GitHub issue #20. After every server update, each user gets a
short "what's new" message in their bot chat, in their language. Notes are kept in the
repository per version. They are sent once, automatically on startup after an update, to
users with access, within Telegram's limits. The operator can preview them, send them by
hand and turn automatic sending off.

## Clarifications

The issue lists open questions. They are answered here with defaults, so no interactive
clarification was needed:

- Q: Automatic on startup, or only through an explicit operator command? → A: Both.
  Automatic sending is **on by default** and can be turned off with a deployment setting.
  The operator can always send by hand.
- Q: Also notify users who are still in setup? → A: Yes. Anyone with an account and
  access gets the notes, whatever their setup or pause state. A user in setup is already
  talking to the bot, and new features are relevant to them too.
- Q: One message per version, or a combined digest if several versions were skipped? → A:
  One digest message per update. It covers every version with notes that the user has not
  received yet, oldest first. It is split into several messages only if it is too long for
  one Telegram message.
- Q: Must notes be in both languages, or fall back? → A: A notes file should have both
  English and Russian. If the user's language is missing, the other language is sent
  instead, and the preview warns about it. A version with no text in either language sends
  nothing.
- Q: Where to keep the notes: files in the repo, or a DB table? → A: Files in the
  repository, one per version, reviewed in pull requests like code and shipped with the
  service. The database only records what was announced and to whom.

## Problem

Users do not know when the service changes: new commands, new buttons, changed behaviour
(for example the in-bot beta applications from specs/005). The version is never bumped,
nothing records what changed, and there is no channel to tell users. Updates are deployed by
hand, so the operator would have to message each user personally.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Users learn what is new after an update (Priority: P1)

The operator deploys a new version that has release notes. When the service starts, every
user with access gets one short message in their bot chat, in their language: "What's new in
Scribe 0.2.0" followed by the notes. Nobody gets it twice. Restarting the service, or
deploying again without changing the version, sends nothing.

**Why this priority**: this is the whole point of the issue. Without it users miss new
features.

**Independent Test**: with two test users (one English, one Russian) and a notes file for a
version newer than the last one announced, start the service. Each user gets exactly one
message in their language. Restart the service: nothing more is sent.

**Acceptance Scenarios**:

1. **Given** a deployed version with a notes file, newer than the last announced version,
   **When** the service starts, **Then** every user with access gets exactly one message
   with those notes in their language.
2. **Given** the notes were already announced for the running version, **When** the service
   restarts, **Then** nothing is sent.
3. **Given** a deployed version with no notes file, or an empty one, **When** the service
   starts, **Then** nothing is sent and the version counts as handled.
4. **Given** two versions were deployed since the last announcement and both have notes,
   **When** the service starts, **Then** each user gets one digest with both versions'
   notes, oldest first.
5. **Given** an English user and a notes file that only has Russian text, **When** the
   notes are sent, **Then** the user gets the Russian text instead of nothing.
6. **Given** an older version is deployed again (a rollback), **When** the service starts,
   **Then** nothing is sent.

---

### User Story 2 - Only people with access are notified (Priority: P1)

The notes go only to people who can use the service: beta testers, subscribers and
operators who have an account. Suspended users (access ended), refused senders and people
whose beta application is still pending get nothing.

**Why this priority**: telling someone without access about new features is confusing and
leaks product plans. It also wastes Telegram capacity.

**Independent Test**: with the access gate on, prepare one beta user, one subscriber, one
suspended user, one refused sender and one person with a pending application. Announce a
version. Only the beta user and the subscriber get the message.

**Acceptance Scenarios**:

1. **Given** the access gate is on, **When** notes are sent, **Then** users with beta,
   subscription or operator access get them, and suspended users do not.
2. **Given** a refused sender or a person with only a pending application, **When** notes
   are sent, **Then** they get nothing, because they have no account.
3. **Given** the access gate is off (a self-hosted installation), **When** notes are sent,
   **Then** every user with an account gets them.
4. **Given** a user who lost access after the sending started but before their message
   went out, **When** their turn comes, **Then** they are skipped.

---

### User Story 3 - Sending is safe at any scale (Priority: P2)

Sending respects Telegram's bot limits, so the bot is never throttled for its normal work
(draft cards, notices). If Telegram asks the bot to slow down, it waits and continues. If
the service restarts in the middle of sending, it continues where it stopped: nobody gets a
duplicate, and nobody who was due is skipped. A user who blocked the bot is skipped and
recorded, and the rest continue.

**Why this priority**: with 30 beta testers a naive loop would work. With hundreds of
users it would hit limits and could send duplicates after a crash. This matters as the
service grows, but it is not needed for the first announcement.

**Independent Test**: announce a version to many test users through a fake bot that
answers some sends with "retry after N seconds" and one with "blocked by the user". Stop
the service halfway and start it again. Every due user gets exactly one message. The
blocked user is recorded as blocked. The send rate never exceeds the configured limit.

**Acceptance Scenarios**:

1. **Given** many recipients, **When** notes are sent, **Then** no more than the configured
   number of messages per second is sent overall, and no chat gets more than one message per
   second.
2. **Given** Telegram answers "retry after N seconds", **When** that happens, **Then**
   sending pauses for N seconds and the same message is retried. It does not count as a
   failure.
3. **Given** a user who blocked the bot or deleted their account, **When** their message is
   sent, **Then** they are recorded as unreachable, the log holds no content, and sending
   continues.
4. **Given** the service stops halfway through sending, **When** it starts again, **Then**
   sending resumes with the users who have not received the notes, and those who did are not
   sent again.

---

### User Story 4 - The operator controls the announcement (Priority: P2)

Before deploying, the operator previews the notes for a version in both languages and sees
any problem: a missing language, text that is too long, or a version name that does not
match a release. After deploying, the operator can check progress: how many users received
the notes, how many are left, how many were unreachable. The operator can send a version's
notes by hand, with a dry run that shows who would get what without sending. On an
installation where announcements are not wanted, the operator turns automatic sending off.

**Why this priority**: automatic sending covers the normal case. Operator controls are
needed for checking text before a release, for installations with automatic sending off, and
for sending notes added after a deploy.

**Independent Test**: run the preview for a version with notes. Both languages are shown,
with a warning if one is missing. Run a dry-run send: it shows recipient counts per language
and sends nothing. Run a real send: users get the message. Turn automatic sending off and
deploy a new version: nothing is sent until the operator sends by hand.

**Acceptance Scenarios**:

1. **Given** a notes file, **When** the operator runs preview, **Then** they see the
   message exactly as each language's users would get it, plus any warnings.
2. **Given** a version, **When** the operator runs send with dry run, **Then** they see how
   many users would get it in each language and how many would be skipped, and nothing is
   sent or recorded.
3. **Given** a version, **When** the operator runs send without dry run, **Then** the
   running service sends it to every user with access who has not received it yet.
4. **Given** automatic sending is off, **When** a new version starts, **Then** nothing is
   sent automatically, and a later manual send works as usual.
5. **Given** an announcement in progress or finished, **When** the operator asks for its
   status, **Then** they see the counts of delivered, waiting, unreachable, failed and
   skipped recipients.

---

### User Story 5 - A release has a version and notes (Priority: P3)

Each release bumps the service version and, when it is worth announcing, adds a notes file
for that version. The release steps are written down, so whoever deploys knows what to do.

**Why this priority**: without a version bump nothing is ever announced. It is a process
change with little code, so it comes last.

**Independent Test**: follow the written release steps for a test version. The service
reports the new version, and the notes file is picked up.

**Acceptance Scenarios**:

1. **Given** the written release steps, **When** a release is prepared, **Then** the
   version is bumped, the notes file is added or deliberately left out, and the release can
   be tagged.
2. **Given** a notes file whose name is not a valid version, or which has an empty or
   too-long section, **When** the automated checks run, **Then** they fail and name the
   file.

---

### Edge Cases

- **First deploy of this feature**: nothing was announced before. Only the running
  version's notes are sent, not every older notes file.
- **Version skipped without notes**: a version without notes counts as handled, so the
  next version's digest does not include it.
- **Notes added after the deploy**: the version was already handled, so nothing is sent
  automatically. The operator sends it by hand.
- **Manual send of a version already sent**: only users who have not received it get it.
  If everyone has, the operator is told nothing is left to send.
- **Manual send of a version the digest already covered**: users who got it in a digest do
  not get it again.
- **New user during sending**: someone who gets an account after the announcement started is
  not added to it. They start on the current version anyway.
- **User deletes their data during sending**: their delivery record goes with the rest of
  their data, and nothing is sent to them.
- **Unknown result after a crash**: if the service stopped right after asking Telegram to
  send a message but before recording it, it cannot know whether the message arrived. That
  message is treated as delivered and is not resent, because a duplicate is worse than one
  missed notice. Such cases are counted separately in the status, so the operator can see
  them. At most the messages in flight at the moment of the crash are affected.
- **Telegram unavailable for a long time**: a message that keeps failing for non-limit
  reasons is retried a few times with growing delays, then recorded as failed. The operator
  can resend later; only users who have not received it get it.
- **Several operators, gate on or off**: an operator with an account is a user with access
  and gets the notes like everyone else.
- **Very long notes or a long digest**: a version's text is limited so that it fits in one
  message. A digest of several versions is split into several messages at version
  boundaries, sent at least one second apart.
- **The notes text**: it is written by the operator, never contains user data, and is shown
  as plain text. Nothing in it is interpreted as formatting or commands.

## Requirements *(mandatory)*

### Functional Requirements

**Release notes**

- **FR-001**: Release notes MUST be kept in the repository, one file per version, named
  after the version, with an English section and a Russian section of short, user-facing
  text. They MUST be shipped with the service.
- **FR-002**: A version MUST be a three-part number (major.minor.patch). Versions MUST be
  compared as numbers, so 0.10.0 is newer than 0.9.0.
- **FR-003**: Each language section of a version MUST fit in one Telegram message together
  with the heading. Automated checks MUST fail on a file with a bad name, an unknown
  section, or a section that is too long. A section that is missing or empty is allowed but
  reported as a warning.
- **FR-004**: Every message MUST start with a heading in the user's language that names the
  version: "What's new in Scribe 0.2.0". In a digest, every version has its own heading.

**Automatic announcement**

- **FR-005**: When the service starts with automatic sending on, it MUST check whether the
  running version is newer than the last version announced or handled. If so, it MUST
  announce every version newer than the last announced one, up to and including the running
  one, that has notes. It MUST then record the running version as handled, even if no
  version had notes.
- **FR-006**: The record of the last announced version MUST be kept in the database, so that
  a restart without a version change, or the deploy of an older version, sends nothing.
- **FR-007**: When no version was announced before, only the running version's notes MUST
  be announced.
- **FR-008**: Automatic sending MUST be a deployment setting, on by default. When it is off,
  nothing is sent or recorded at startup.

**Recipients**

- **FR-009**: The recipients MUST be the users with an account who have access when the
  announcement starts. With the access gate on, that means beta, subscription or operator
  access, and not suspended (specs/005). With the gate off, it means every user with an
  account. A user's setup, pause or out-of-office state MUST NOT matter.
- **FR-010**: Right before each message is sent, the system MUST check again that the
  recipient still has an account and access, and skip them otherwise.
- **FR-011**: Each user MUST get the notes in their stored language. If that language's
  section is missing or empty, they MUST get the other language's section. A version with
  no text in either language MUST NOT be sent.
- **FR-012**: A user MUST receive each version's notes at most once, whether sent
  automatically, in a digest or by hand. A digest for a user MUST leave out versions they
  already received.

**Delivery**

- **FR-013**: Sending MUST stay under a configurable overall rate (default well below
  Telegram's limit of about 30 messages per second, leaving room for the bot's normal
  work), and MUST NOT send more than one message per second to the same chat.
- **FR-014**: When Telegram asks the bot to wait, sending MUST pause for the requested time
  and retry the same message, without counting it as a failure.
- **FR-015**: When a user has blocked the bot or their chat no longer exists, the system MUST
  record that user as unreachable for this announcement and continue. Other errors MUST be
  retried a limited number of times with growing delays, then recorded as failed.
- **FR-016**: Progress MUST be stored per recipient, so that after a restart the
  announcement continues with the users who have not received it. A message whose result is
  unknown after a crash MUST NOT be resent, and MUST be counted separately.
- **FR-017**: Release-notes messages MUST be sent without a notification sound, since they
  are informational and may go out at any hour.
- **FR-018**: Sending release notes MUST NOT delay or block the bot's normal work: commands,
  draft cards and notices.

**Operator controls**

- **FR-019**: The operator MUST be able to preview a version's notes in both languages,
  exactly as users would see them, with warnings about missing languages and errors about
  length or format.
- **FR-020**: The operator MUST be able to send a version's notes by hand. The send MUST go
  to every user with access who has not received that version yet, and the running service
  MUST deliver it within about a minute.
- **FR-021**: A manual send MUST offer a dry run that shows the number of recipients per
  language, the number who already have it, and the text, without sending or recording
  anything.
- **FR-022**: The operator MUST be able to see each announcement's status: which versions it
  covers, when it started and finished, how it was started (automatically or by hand), and
  the counts of delivered, waiting, unreachable, failed, unknown and skipped recipients.

**Privacy and logging**

- **FR-023**: Logs MUST hold only ids, versions and counts. They MUST never hold the notes
  text, usernames or chat content (constitution, Principles II and VI).
- **FR-024**: A user's delivery records MUST be shown in `/my_data` and deleted with the
  rest of their data by `/delete_my_data`.

**Release process**

- **FR-025**: The release steps MUST be documented: bump the version, add a notes file if
  the release is worth announcing, check it with the preview, and optionally tag the
  release.

### Key Entities

- **Release notes**: the text for one version, in English and Russian. It is a file in the
  repository, not stored in the database.
- **Announcement**: one decision to send release notes. It has the versions it covers, the
  running version it was made for, a copy of the notes text in both languages, how it was
  started (automatically or by hand), when it started and finished.
- **Delivery**: one recipient of one announcement. It has the user, its state (waiting,
  sending, delivered, unreachable, failed, skipped, unknown), the number of attempts and when
  it was delivered. It holds no message text of its own.
- **Last handled version** (derived): the newest version for which an automatic
  announcement was made or that was handled without notes.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: After a deploy of a version with notes, 100% of users with access receive
  exactly one message with those notes, in their language, within 10 minutes for up to 1,000
  users.
- **SC-002**: In testing, restarts and deploys without a version change send 0 messages.
- **SC-003**: In testing, suspended users, refused senders and applicants receive 0
  messages.
- **SC-004**: In a test that stops the service halfway through sending, 0 users receive a
  duplicate, and every due user except those whose message was in flight at the stop is
  delivered after the restart.
- **SC-005**: Sending never exceeds the configured overall rate or one message per second
  per chat, and the bot answers commands without noticeable delay while an announcement is
  running.
- **SC-006**: The operator can preview a version and see an announcement's progress in
  under 1 minute, without reading logs or the database.

## Assumptions

- Updates are deployed by hand (`git pull`, build, migrate, start), and the service runs as
  a single process, as today.
- The running version is the version of the installed service package. It changes only when
  the operator bumps it in a release.
- The first version that ships this feature is announced with its own notes, so beta
  testers learn about the in-bot beta applications and this feature itself. The operator
  reviews that text before the deploy.
- Release notes are plain text with simple bullet lines. Formatting, images and buttons in
  release notes are out of scope.
- Only the control bot sends release notes. The users' own Telegram accounts are never used.
- Announcements reach users with an account. People without one see the current state of
  the service when they first start, so they need no notes.
- Delivery records are tiny (user, version, time, state). They are kept until the user
  deletes their data; no separate retention period is added.
- The operator is responsible for the notes text. It is reviewed in a pull request like any
  other change, so the system does not moderate it.
