# Feature Specification: Mark Chats Unread After Agent Activity

**Feature Branch**: `009-mark-chats-unread`

**Created**: 2026-10-23

**Status**: Draft

**Input**: User description: "issue #16 — Mark chats 'unread' when they have changes the user
has not seen. The agent reads incoming messages on the user's behalf (marks them read) and may
reply. As a result, the user's Telegram chat list does not show where something happened: the
chat looks read although the person has not seen the new messages or the agent's reply. When a
work chat has changes the person has not seen yet (the agent read incoming messages, sent a
reply, prepared a draft), mark the chat 'unread'. When the person opens the chat themselves (a
read from their own device) or sends a message by hand, the mark clears as usual and is not set
again until there are new changes. An on/off setting (default: on?)."

**Implements**: GitHub issue #16 (peach098/scribewise).

**Builds on**: `specs/001-telegram-reply-agent`: the reading delay (US2, FR-016 to FR-018;
the agent marks messages read only when their delay has passed), "the user reads the chat
themselves on another device" (001 edge case and the read-elsewhere handling), the manual-reply
detection (FR-015), sending approved replies (FR-012 to FR-014) and `/settings`.
Also `specs/004-skip-closing-messages` (a message the agent reads but skips) and
`specs/005-beta-subscription-access` (no agent work while access is suspended).

**Related in-flight work**: `specs/006-group-mention-only` (which group messages are read and
drafted) and issue #15 / spec 008 (work-account mode, more chats handled). This feature follows
whatever chats and messages the agent reads; it does not change which ones those are.

## Problem

When the agent reads a work message (after the reading delay), Telegram marks it read on all
of the user's devices: the unread counter disappears from the chat list. When the user approves
a reply, it is sent from their account. In both cases nothing in the Telegram chat list tells
the user that this chat changed. The only trace is the card in the control bot. A user who
works from the Telegram chat list misses chats where a colleague wrote and the agent answered.

Telegram lets a person mark a chat as unread by hand (a dot in the chat list, cleared when the
chat is opened). This feature makes the agent set that mark for the user whenever it reads or
answers in a work chat, so the chat list again shows where something happened.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Chats the agent read stay visible in the chat list (Priority: P1)

A colleague writes in a work chat. After the reading delay the agent reads the message and
prepares a draft (or skips a closing message, 004). Right after reading, the chat is marked
unread in the user's Telegram, so on their phone and desktop it still stands out in the chat
list, although its messages are read.

**Why this priority**: this is the core of the issue. Without it the agent's reading hides
new work messages from the user's chat list.

**Independent Test**: with the setting on, send a message from a second account to a test work
chat with a short delay. After the delay, check in the user's Telegram app that the message is
read (the second account sees the read ticks) and the chat shows the unread mark.

**Acceptance Scenarios**:

1. **Given** the setting is on and a work chat with a message waiting out its delay, **When**
   the agent reads the message, **Then** within 10 seconds of the read the chat shows as unread
   in the user's Telegram.
2. **Given** the setting is on, **When** the agent reads a message and the reply gate skips it
   as a closing message (004), **Then** the chat is still marked unread, because the user has
   not seen that message.
3. **Given** several messages from different turns in one chat are read in one go, **When**
   the agent reads them, **Then** the chat is marked unread once.
4. **Given** the setting is on, **When** a message is never read by the agent (media only and
   left for the user, a group message not addressed to the user, the user paused the agent or
   lost access), **Then** the agent does not mark the chat: Telegram's own unread counter
   already shows it.

---

### User Story 2 - Chats the agent answered stay visible (Priority: P1)

The user approves a draft in the control bot and the reply is sent from their account. The
chat is marked unread afterwards, so the chat list shows that the conversation moved on.

**Why this priority**: listed in the issue's acceptance criteria ("after the agent reads or
replies"). Sending a message can clear the mark the agent set when reading, so without this
story the mark could disappear after a reply.

**Independent Test**: with the setting on, approve a draft in a test work chat. After the reply
is sent, check that the chat shows the unread mark in the user's Telegram.

**Acceptance Scenarios**:

1. **Given** the setting is on and a draft is approved, **When** the reply is sent, **Then**
   the chat is marked unread within 10 seconds of sending.
2. **Given** marking the chat fails (Telegram is unavailable or asks to wait), **When** the
   reply is sent, **Then** the reply still counts as sent and the user is told nothing extra.
   Only the mark is missing.

---

### User Story 3 - The mark goes away when the user looks, and stays away (Priority: P1)

The user opens the marked chat on any device. Telegram removes the mark as it always does.
The agent does not put it back until something new happens in that chat: a new message the
agent reads, or a new reply sent through the agent. The same holds when the user reads the
chat or replies in it by hand.

**Why this priority**: without it the mark is noise. A mark that comes back after the user has
looked is worse than no mark (the issue's "avoid a loop" question).

**Independent Test**: let the agent read a message so the chat is marked. Open the chat on the
phone (the mark disappears). Wait at least 10 minutes with no new messages. The chat stays
unmarked. Then send a new message from the second account: after its delay the agent reads it
and the mark comes back once.

**Acceptance Scenarios**:

1. **Given** a chat the agent marked, **When** the user opens it on any device, **Then** the
   mark clears (Telegram's normal behavior) and the agent does not set it again without a new
   message read or a new reply sent through the agent.
2. **Given** the user read a chat on their own device before the agent read it (the message was
   still waiting out its delay), **When** the agent then processes that message, **Then** the
   chat is not marked, because the user has already seen everything up to that point.
3. **Given** the user replied by hand in a work chat, **When** the agent processes the chat
   again for messages up to that reply (for example after a retry or a restart), **Then** the
   chat is not marked.
4. **Given** the agent reads the same message again (a retry after a failure, a restart, or
   "Regenerate" on an outdated draft), **When** nothing newer than what was already marked or
   seen arrived, **Then** the chat is not marked again.
5. **Given** the agent's own reading of a chat, **When** Telegram reports that read back to the
   agent, **Then** the agent does not mistake it for the user opening the chat.

---

### User Story 4 - The user can turn it off (Priority: P2)

In `/settings` the user sees "Mark chats unread after the agent: on" with a button to turn it
off (and later on again). When it is off, the agent reads and sends exactly as before and never
marks a chat.

**Why this priority**: some people find the extra dots noisy or use the control bot as their
only inbox. The default (on) already covers the main need, so the switch comes second.

**Independent Test**: turn the setting off in `/settings`, let the agent read a message and
send a reply in a test chat, and check that the chat is not marked. Turn it on again and repeat:
the chat is marked.

**Acceptance Scenarios**:

1. **Given** a new or existing user, **When** they open `/settings`, **Then** the setting is
   shown, on by default, in their language (English or Russian).
2. **Given** the setting is on, **When** the user presses the button, **Then** it turns off,
   the `/settings` message shows the new value, and from that moment no chat is marked.
3. **Given** the setting is turned off, **When** chats are still marked from earlier, **Then**
   the agent leaves them as they are (it never removes a mark, which may be the user's own).
4. **Given** the setting is turned on again, **When** nothing new happens in a chat, **Then**
   the chat is not marked retroactively; only later reads and replies mark chats.
5. **Given** the user asks for their data (`/my_data`), **Then** the setting's value is listed
   with their other settings.

---

### Edge Cases

- **The user is looking at the chat when the agent acts**: the agent cannot tell that a chat
  is open on screen. The mark may appear and the user's app clears it when they next open the
  chat. This is accepted.
- **The user marked a chat unread by hand**: the agent never removes a mark, so the user's own
  mark is kept. If the agent sets a mark on a chat that is already marked, nothing changes.
- **The user marks a chat read from the chat list without opening it** (for example "Mark as
  read" on a phone): this counts as the user having seen the chat, like opening it.
- **The user clears the mark while the agent is still drafting**: the draft is not affected.
  The chat is marked again only for a new message read or a reply sent.
- **Several devices and Telegram Web**: the mark is account-wide in Telegram, so it shows and
  clears on every device of the user.
- **Group chats**: the same rules apply to group work chats the agent reads (messages that
  mention or reply to the user, or every message in a chat set to "always").
- **Telegram refuses or delays the mark** (flood wait, network error, the session was revoked):
  reading, drafting and sending carry on unaffected; the missing mark is logged and not
  retried later (a late mark could appear after the user has already looked).
- **Agent paused, out of office, or access suspended**: no reading and no sending happen, so no
  marking either. Out of office still reads and drafts, so marking applies as usual.
- **A message arrives right after the user opened the chat**: it is newer than what the user saw,
  so when the agent reads it the chat is marked.
- **The chat is disabled as a work chat or the Telegram account is disconnected**: the agent no
  longer reads or sends there, so it no longer marks it.
- **Forum groups with topics and archived chats**: the mark applies to the whole chat as
  Telegram defines it. Archived chats stay archived.

## Requirements *(mandatory)*

### Functional Requirements

**Setting the mark**

- **FR-001**: When the setting is on and the agent marks messages read in a work chat (after
  the reading delay, after "Read now", or when reading before "Regenerate"), the system MUST
  mark that chat unread in the user's Telegram right after the read, unless FR-005 says the
  user has already seen those messages.
- **FR-002**: When the setting is on and a reply approved by the user is sent in a work chat,
  the system MUST mark that chat unread right after sending, unless FR-005 applies.
- **FR-003**: The system MUST NOT mark a chat for any other reason: not for messages it did
  not read, not for chats that are not enabled work chats, not on its own schedule, and not
  retroactively when the setting is turned on.
- **FR-004**: Marking MUST be best effort. A failure to mark MUST NOT delay, retry or fail
  the read, the draft, the reply gate or the send, MUST NOT notify the user, and MUST NOT be
  retried later.

**Not marking what the user has seen (loop avoidance)**

- **FR-005**: The system MUST keep, per work chat, the newest message the chat was last marked
  for or the user is known to have seen. It MUST mark the chat only for a read or a sent reply
  whose newest message is newer than that point, and then move the point to that message.
- **FR-006**: The user MUST count as having seen a chat up to a message when, from their own
  device or app, they read the chat up to it, clear the chat's unread mark, or send a message
  in the chat by hand. These signals MUST come from the user's other sessions and MUST NOT be
  confused with the agent's own reads, marks or sends.
- **FR-007**: The system MUST NOT remove an unread mark, whether the agent or the user set it.
  Telegram clears it when the user opens the chat.
- **FR-008**: Marking a chat MUST NOT cause the agent to read, draft or send anything. The
  agent's reading stays driven only by incoming messages and their reading delay (001 FR-016).

**Setting**

- **FR-009**: Each user MUST have an on/off setting "Mark chats unread after the agent",
  on by default for new and existing users, shown and toggled in `/settings` in the user's
  language.
- **FR-010**: Turning the setting off MUST stop all marking from the next read or send. Turning
  it on MUST NOT mark any chat until the next read or send.
- **FR-011**: The setting MUST be included in the user's data export and removed with their
  data (constitution, Principle II).

**Observability & privacy**

- **FR-012**: Every mark the agent sets, skips (and why: setting off, already seen, not newer)
  or fails to set MUST be logged as a structured event with the user and chat identifiers and
  no message content (constitution, Principles I, II and VI).
- **FR-013**: The feature MUST NOT store any new message content. It stores only the setting
  and, per work chat, a Telegram message number (FR-005).

### Key Entities

- **Unread-mark setting**: a per-user on/off value (default on), shown in `/settings`.
- **Mark point** (per work chat): the newest Telegram message number the chat was marked for by
  the agent or seen by the user. Empty until the first mark or the first seen signal. Used only
  to decide whether a new read or reply deserves a mark.
- **Seen signal**: an event from the user's own device: the chat was read up to a message, its
  unread mark was cleared, or the user sent a message by hand.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With the setting on, in 100% of test cases where the agent reads new messages or
  sends a reply in a work chat that Telegram accepts, the chat shows as unread on the user's
  devices within 10 seconds.
- **SC-002**: After the user opens a marked chat, the mark does not come back without a new
  read message or a new reply in 100% of test cases over at least 10 minutes, including retries,
  restarts and "Regenerate".
- **SC-003**: With the setting off, no chat is ever marked by the agent.
- **SC-004**: When marking fails, reading, drafting and sending show no difference in outcome
  or timing (no extra delay beyond the failed call's own time limit) in 100% of test cases.
- **SC-005**: A user can find and toggle the setting in `/settings` in under 30 seconds.

## Assumptions

- **When the mark is set** (open question in the issue): right after the agent reads new
  messages, and again right after an approved reply is sent. Reading is the moment the unread
  counter disappears, so waiting for the reply would leave the chat looking quiet while the
  user reviews the draft. Re-marking after a send covers Telegram clearing the mark when a
  message is sent. "Prepared a draft" needs no separate mark: a draft is always prepared right
  after a read that already marked the chat.
- **Telling the user's read from the agent's read** (open question in the issue): the agent
  decides whether to mark before it reads, from what it already knows, so a read it causes
  itself cannot hide its own mark. Reads, mark clearing and manual messages reported from the
  user's other sessions count as "seen" (FR-006). The existing read-elsewhere handling (001)
  keeps its current effect of making waiting messages due now.
- **Loop avoidance** (open question in the issue): the mark never triggers reading (FR-008),
  and the per-chat mark point (FR-005) means a chat is marked at most once per new message or
  reply, so open → clear → mark again cannot repeat without new activity.
- **Skipped closing messages are marked**: the agent read them and the user has not seen them.
  A user who finds this noisy can turn the setting off; a finer rule is out of scope.
- **Default on** (open question in the issue): the issue suggests it, and the mark only restores
  what reading took away.
- **One global switch**: per-chat overrides, quiet hours for marking, and removing marks are out
  of scope (YAGNI).
- **No backfill**: chats read before this feature ships, or while the setting was off, are not
  marked.
- Telegram's "mark as unread" for a chat is available to user accounts and is account-wide.
  If a chat type does not support it, that chat is simply not marked (FR-004).
