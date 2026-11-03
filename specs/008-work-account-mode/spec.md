# Feature Specification: Work Account Mode — Every Private Chat Is a Work Chat

**Feature Branch**: `008-work-account-mode`

**Created**: 2026-10-23

**Status**: Draft

**Input**: User description: "issue #15 — 'Work account' option: enable all chats at once. During
setup (and in settings) the user picks work chats by hand, one by one. For a dedicated work
Telegram account, where every contact is a work contact, this is tedious. Contacts who write for
the first time later are not in the list either, so the agent silently ignores their messages."

**Implements**: GitHub issue #15.

**Builds on**: `specs/001-telegram-reply-agent` (FR-001 intake only in work chats, FR-024 per-chat
settings and `/chats`, FR-026 view/delete data, FR-034/FR-035 style import and contact profiles,
setup flow), `specs/005-beta-subscription-access` (access gate: nothing is read for a user
without access).

**Changes**: 001 FR-001 (a work chat can now also be "every eligible private chat" instead of
only hand-picked ones), the setup chat-picker step, `/settings`, `/chats`.

**Related in-flight work**:

- `specs/006-group-mention-only` (issue #25): group chats draft only when the user is tagged or
  replied to; the per-chat "every message" group mode is removed. This feature does not change
  groups (see FR-010).
- Spec 007 (issue #32): ➕ add work chats after setup from `/chats`, reusing the setup picker.
  With work account mode on, that picker is how the user adds **groups**; private chats are
  already covered (FR-011).
- Issues #29 / #30 (style profiles): work account mode widens the set of chats whose own
  messages feed the global profile and compete for the 20 contact-profile slots (FR-016).

## Problem

Work chats are picked by hand, once, in the setup picker. On a Telegram account used only for
work this means ticking every chat one by one, and anyone who writes to the user for the first
time later is not a work chat, so their message is silently ignored. In early testing a user had
12 chats enabled out of many; messages from the rest were not handled and it was not clear why.

This feature adds a per-user **work account mode**: "every contact is a work contact". With it
on, every private chat with a real person — existing or new — is a work chat, minus the chats
the user excludes by hand. Turning it off returns to the hand-picked list.

Because this mode widens what the agent reads and stores (constitution, Principle II), it is
off by default, explained before it is turned on, limited to private chats with people, and
fully visible and reversible.

## Clarifications

### Session 2026-10-23 (informed defaults, no open questions)

- Q: Are groups part of "all chats"? → A: No. Groups stay hand-picked (setup picker, spec 007's
  ➕ button) and follow spec 006's rule (draft only when tagged or replied to). Group traffic is
  much larger and more often non-work; auto-including it would widen reading far beyond the
  issue's need. The issue itself suggested "probably not by default".
- Q: Which private chats count? → A: Private chats with real people only. Bots (including the
  Scribe control bot), Saved Messages, Telegram's service notifications and deleted accounts
  never count.
- Q: Does "every existing chat counts" mean old messages get drafts? → A: No. As everywhere
  (001 FR-001), only messages that arrive after the chat became a work chat are handled. Old
  history is used only for the one-time style import (FR-016).
- Q: What happens to chats the user had disabled by hand before turning the mode on? → A: They
  stay off and appear as excluded. Turning the mode on never re-enables a chat the user turned
  off.
- Q: When the mode is turned off, what happens to chats it picked up? → A: They stop being
  handled at once (their waiting messages and pending drafts are dropped as for a disabled
  chat); hand-picked chats keep working unchanged. Their per-chat settings and exclusions are
  kept, so turning the mode on again restores the same state.
- Q: Is a chat picked up only when the contact writes first? → A: A chat becomes a work chat on
  its first new message in either direction (the contact's or the user's), so that the user's
  opening message is context for the draft of the contact's answer.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Turn on work account mode in setup and get drafts from everyone (Priority: P1)

A user connects a Telegram account used only for work. At the chat-picker step they turn on
"💼 Work account — every private chat is a work chat". The picker then shows their private chats
as included and lets them exclude any by tapping it; groups can still be ticked by hand. They
finish setup without ticking chats one by one. From then on, a message from any private
contact — including someone who never wrote before — gets a draft as usual.

**Why this priority**: this is the core of the issue: no one-by-one picking, and no silently
ignored first-time contacts.

**Independent Test**: during setup turn the mode on and finish without ticking anything. Then
send a message to the account from (a) an existing private contact who was never picked and
(b) a brand-new contact. Both get a draft after the usual reading delay.

**Acceptance Scenarios**:

1. **Given** the setup chat picker, **When** the user turns work account mode on, **Then** the
   picker explains in one line what it means, shows every eligible private chat as included, and
   tapping a private chat toggles it between included and excluded.
2. **Given** work account mode on in the picker, **When** the user taps Done without ticking any
   chat, **Then** setup continues (the "pick at least one chat" rule does not apply) and the
   confirmation says the mode is on and how many chats are excluded.
3. **Given** work account mode on, **When** a private contact who was not picked writes to the
   user, **Then** the chat becomes a work chat and a draft is prepared after the usual delay.
4. **Given** work account mode on, **When** a person who has never written to the user before
   writes for the first time, **Then** a draft is prepared exactly as in scenario 3, with nothing
   to add by hand.
5. **Given** work account mode on, **When** a bot, Telegram's service account, or the Scribe
   control bot sends the user a message, or the user writes to Saved Messages, **Then** nothing
   is stored and no draft is prepared.
6. **Given** work account mode on, **When** a message arrives in a group the user did not pick,
   **Then** nothing is stored and no draft is prepared.
7. **Given** work account mode on, **When** the user writes first to a contact and the contact
   answers, **Then** the draft for the answer has the user's opening message as context.
8. **Given** the mode is off in the picker (the default), **When** the user picks chats, **Then**
   setup behaves exactly as today.

---

### User Story 2 - Exclude individual chats (Priority: P1)

A user with work account mode on has a few chats that are not work (a relative, a personal
contact). They exclude those chats. Messages from excluded chats are never read, stored or
drafted, even though "all chats" is on.

**Why this priority**: without a deny list, the mode would force the agent to read personal
chats the user does not want read; it is required by the privacy principle and by the issue's
acceptance criteria.

**Independent Test**: with the mode on, exclude one chat that has written before and one that
never has. Send a message from each: no draft, nothing stored. Include one of them again: its
next message gets a draft.

**Acceptance Scenarios**:

1. **Given** work account mode on, **When** the user excludes a chat (in the setup picker, from
   `/chats`, or from that chat's settings screen), **Then** its new messages are not stored and
   produce no draft.
2. **Given** a chat that never wrote to the user since the mode was turned on, **When** the user
   opens the exclusion picker, **Then** that chat is offered too (the user can exclude a chat
   before it writes).
3. **Given** a chat with waiting messages or a pending draft, **When** the user excludes it,
   **Then** those are dropped exactly as when a chat is disabled today, and the draft card says
   so.
4. **Given** an excluded chat, **When** the user includes it again, **Then** its next new message
   is handled; messages that arrived while it was excluded are not.
5. **Given** a private chat the user had disabled by hand before turning the mode on, **When**
   the mode is turned on, **Then** that chat stays off and is counted as excluded.

---

### User Story 3 - See and switch the mode in /settings (Priority: P1)

The user opens `/settings` and sees whether work account mode is on and how many chats are
excluded. They can turn it on or off there. Turning it on asks for confirmation with a short
explanation of what will be read. Turning it off returns to the hand-picked list.

**Why this priority**: an acceptance criterion of the issue; and the confusion seen in testing ("why are
these messages not handled?") is solved only if the state is visible.

**Independent Test**: turn the mode on from `/settings` after a normal setup with a hand-picked
list; check that an unpicked contact now gets a draft and `/settings` shows "on, N excluded".
Turn it off; check that the same contact gets no draft while the hand-picked chats still do.

**Acceptance Scenarios**:

1. **Given** `/settings`, **When** it is shown, **Then** it has a line for work account mode
   ("on — every private chat, N excluded" or "off — only chosen chats") and a button to switch
   it.
2. **Given** the mode is off, **When** the user presses the button, **Then** a confirmation
   explains that new messages from every private chat with a person will be read and stored as
   work messages (groups and bots not included), that chats can be excluded, and that it can be
   turned off at any time; only after the user confirms does the mode turn on.
3. **Given** the mode is on, **When** the user turns it off and confirms, **Then** only chats the
   user picked by hand are handled again; chats picked up by the mode stop being handled at
   once, and their waiting messages and pending drafts are dropped as for a disabled chat. The
   confirmation says how many pending drafts will be dropped.
4. **Given** the mode was turned off, **When** the user turns it on again, **Then** the previous
   exclusions and per-chat settings of chats picked up earlier apply again.
5. **Given** the mode is on, **When** the user opens `/chats`, **Then** the list starts with a line
   saying the mode is on and how many chats are excluded, and offers a button to exclude chats.

---

### User Story 4 - Work chats picked up by the mode are visible and configurable (Priority: P2)

Chats picked up by the mode appear in `/chats` like any work chat, with the same per-chat
settings (delay, language, formality, skip thanks/bye). The list stays usable even when the
account has many chats.

**Why this priority**: transparency and control over what the agent reads; per-chat overrides
(001 FR-024) must keep working. Not needed for the first draft to appear, hence P2.

**Independent Test**: with the mode on, let three new contacts write. Open `/chats`: the three
chats are listed (marked as picked up by work account mode), each opens its settings screen,
and the list pages when longer than one screen.

**Acceptance Scenarios**:

1. **Given** chats picked up by the mode, **When** the user opens `/chats`, **Then** each is listed
   and marked as included by work account mode; hand-picked chats are marked as before; excluded
   chats are listed as excluded.
2. **Given** a chat picked up by the mode, **When** the user opens its settings screen, **Then**
   the usual per-chat settings are offered, and the enable/disable button reads "Exclude" /
   "Include again".
3. **Given** more work chats than fit on one screen, **When** the user opens `/chats`, **Then** the
   list is paged.
4. **Given** a new chat was picked up by the mode, **When** the user opens `/history`, **Then** an
   entry says which chat was added as a work chat (chat name only, no message content).

---

### User Story 5 - Style learning includes the work account's chats (Priority: P3)

When the mode is on at setup (or the user presses 🔄 Rebuild in `/profile`), the one-time style
import reads the user's own recent messages in the private chats covered by the mode (minus
exclusions), so the global profile and contact profiles reflect the whole work account, not
only a few picked chats.

**Why this priority**: improves draft quality, but drafts work without it (global profile from
whatever is imported).

**Independent Test**: set up with the mode on and no hand-picked chats on an account with
several private chats where the user wrote recently. Check that the "learned from N messages"
note counts messages from those chats and that excluded chats contributed nothing.

**Acceptance Scenarios**:

1. **Given** setup with the mode on, **When** the style import runs, **Then** it reads only the
   user's own messages from the last 6 months in the most recently active eligible private
   chats (up to a fixed limit, FR-016) plus the hand-picked chats, never from excluded chats,
   groups that were not picked, or other people's messages.
2. **Given** the mode on and 🔄 Rebuild, **When** the import runs, **Then** it follows the same
   rule.
3. **Given** the mode off, **When** the import runs, **Then** it reads only the hand-picked chats,
   as today.

---

### Edge Cases

- A contact writes for the first time while the user is paused, out of office, or outside
  working hours: the chat is picked up and the message is handled by the usual paused/OOO/
  working-hours rules.
- A user without access (005 gate, suspended): nothing is read or picked up, as today.
- A contact the user blocked: Telegram delivers nothing; nothing happens.
- A message from a contact in an archived or muted private chat: counts like any private chat
  (the user can exclude it).
- A first message from a stranger that is spam: it gets a draft like any message; the user can
  reject it and exclude the chat. Drafts are never sent without approval (Principle I).
- A media-only first message from a new contact: the chat is picked up and the message is
  handled manually (user notified), as today.
- A redelivered or duplicate first message: the chat is picked up once; no duplicate chat or
  turn.
- Two messages from a new contact arrive at the same moment: one chat, one turn (grouping rules
  apply as today).
- The user changes the mode while a draft in a picked-up chat is being prepared: if the mode is
  turned off, the draft is not stored (as when a chat is disabled mid-draft).
- A hand-picked private chat while the mode is on: it is handled; turning the mode off keeps it.
- The user excludes a hand-picked private chat while the mode is on: it is off; turning the mode
  off keeps it off (exclusion and disabling are the same user choice).
- The contact's display name changes: the chat's title follows, as today.
- The dialog list cannot be loaded when the user opens the exclusion picker (Telegram not
  connected or unavailable): the user is told so with the same text as setup, and nothing
  changes; exclusions from `/chats` and chat settings still work.
- An account with hundreds of private chats: the mode does not create chat records for all of
  them up front; a chat is recorded only when it has a new message or the user excludes it (or
  it is imported for style, FR-016).
- Old settings buttons (cards sent before a change) for the mode are pressed: the current state
  is shown; nothing is toggled twice.
- The user deletes all their data (001 FR-026): the mode setting, the picked-up chats and the
  exclusions are deleted with everything else.

## Requirements *(mandatory)*

### Functional Requirements

**Mode**

- **FR-001**: Each user MUST have a work account mode setting, **off by default** for new and
  existing users.
- **FR-002**: While the mode is on, a chat MUST count as a work chat (001 FR-001) when it is an
  eligible private chat (FR-003) and the user has not excluded it, in addition to the chats the
  user picked by hand. While the mode is off, only hand-picked, enabled chats count, as today.
- **FR-003**: An eligible private chat is a one-to-one chat with a real Telegram user. Chats with
  bots (including the Scribe control bot), Saved Messages, Telegram's service notifications
  account, and deleted accounts MUST never be eligible. Groups and channels MUST never be
  eligible (FR-010).
- **FR-004**: When a new message (incoming or outgoing) arrives in an eligible private chat that
  is not yet a work chat while the mode is on, System MUST make it a work chat (marked as
  picked up by the mode) and handle the message as a new message in a work chat; messages from
  before that moment MUST NOT be drafted. Making the chat a work chat and storing the message
  MUST happen once, even for redelivered or concurrent messages.
- **FR-005**: System MUST NOT store anything from a chat that does not count as a work chat
  under FR-002 (unchanged 001 FR-001), including chats excluded by the user.

**Exclusions**

- **FR-006**: While the mode is on, the user MUST be able to exclude any eligible private chat —
  in the setup picker, from `/chats` (an exclusion picker listing the account's private chats,
  including ones that never wrote), and from the chat's settings screen — and to include an
  excluded chat again from the same places.
- **FR-007**: Excluding a chat MUST have the same effect as disabling a chat today: its waiting
  messages and pending drafts are dropped (draft cards say so), a draft being prepared is not
  stored, and nothing more is stored from it.
- **FR-008**: Private chats that were disabled by hand before the mode was turned on MUST stay
  off and count as excluded. Turning the mode on MUST NOT re-enable any chat.

**Turning off and on**

- **FR-009**: Turning the mode off MUST return to the hand-picked list: hand-picked chats keep
  their current state; chats picked up by the mode stop counting as work chats at once, and
  their waiting messages and pending drafts are dropped as in FR-007. Exclusions and per-chat
  settings of picked-up chats MUST be kept, so turning the mode on again restores them.

**Groups and other features**

- **FR-010**: Groups MUST NOT be included by the mode. Groups become work chats only by hand
  (setup picker, spec 007), and follow the group drafting rule of spec 006.
- **FR-011**: The ➕ add-chats picker of spec 007, while the mode is on, MUST still allow adding
  groups; private chats MUST be shown as already covered by the mode (or left out), not offered
  as new.
- **FR-012**: Every other place that today works on "the enabled work chats" (drafting, manual
  reply detection, earlier-conversation search, skipped-message list, style refresh, commitment
  extraction) MUST use the same rule as FR-002, so a picked-up chat behaves exactly like a
  hand-picked one and an excluded or deactivated chat like a disabled one.

**User interface**

- **FR-013**: The setup chat picker MUST offer a work account mode switch. With it on, private
  chats are shown as included and tapping one toggles it excluded/included; groups are still
  picked by hand; Done is allowed with no chat ticked. With it off, the picker behaves as today.
- **FR-014**: `/settings` MUST show the mode state and, when on, the number of excluded chats,
  and MUST offer a button to switch it. Turning it on MUST require a confirmation that explains
  in plain words what will be read (new messages from every private chat with a person; not
  groups, not bots), that chats can be excluded, and that the mode can be turned off. Turning it
  off MUST require a confirmation that states how many pending drafts will be dropped.
- **FR-015**: `/chats` MUST show, when the mode is on, a header line with the mode and the number
  of excluded chats and a button to exclude chats; mark each chat as hand-picked, picked up by
  the mode, or excluded; and be paged when the list is longer than one screen. The chat
  settings screen of a private chat MUST label its enable/disable button "Exclude" / "Include
  again" while the mode is on.

**Style learning**

- **FR-016**: When the style import runs (setup, 🔄 Rebuild) with the mode on, it MUST read the
  user's own messages from the last `style_import_months` in the hand-picked enabled chats plus
  the most recently active eligible, non-excluded private chats, up to a configurable limit
  (default 50 private chats picked up by the mode). It MUST NOT read excluded chats, unpicked
  groups, or other people's messages (001 FR-035 otherwise unchanged). Chats imported this way
  become picked-up work chats so their contacts can get contact profiles (001 FR-034) and rank
  by own-message count (issue #29).

**Transparency, privacy and logging**

- **FR-017**: System MUST record in `/history` when the mode is turned on or off, when a chat is
  picked up by the mode, and when a chat is excluded or included again (chat name only, never
  message content).
- **FR-018**: The user's data overview and export (001 FR-026) MUST include the mode setting and
  the lists of picked-up and excluded chats; deleting all data MUST delete them.
- **FR-019**: Structured logs MUST record mode changes, chats picked up, exclusions and the
  eligibility decision outcome (counts and ids only), never message content or chat titles.
- **FR-020**: All new texts MUST exist in every supported interface language (en, ru).

### Key Entities

- **Work account mode** (per user): on/off; when it was last changed.
- **Work chat** (existing, extended): now also records **how it became a work chat** —
  hand-picked by the user, or picked up by work account mode. Its existing enabled/disabled
  state is the user's include/exclude choice; for a chat picked up by the mode it counts only
  while the mode is on.
- **Exclusion**: an eligible private chat the user turned off while the mode applies; it is a
  work-chat record in the disabled state (possibly created by the exclusion itself, holding only
  the chat's Telegram id, title and kind — no messages).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A user with a dedicated work account completes the chat step of setup in at most
  2 taps (mode on, Done), regardless of how many chats the account has.
- **SC-002**: With the mode on, 100% of new messages from eligible, non-excluded private chats —
  including first-time contacts — open a turn, in an acceptance test with at least 3 existing
  unpicked contacts and 2 new contacts.
- **SC-003**: With the mode on, 0 messages from excluded chats, bots, Saved Messages, the service
  account, and unpicked groups are stored (verified by test and by the data overview counts).
- **SC-004**: After turning the mode off, 0 new drafts appear in chats picked up by the mode,
  while hand-picked chats keep producing drafts.
- **SC-005**: A user can tell from `/settings` alone whether all private chats are handled and
  how many are excluded (the confusion seen in testing cannot recur unnoticed).
- **SC-006**: Turning the mode off and on again restores the same exclusions in 100% of cases.

## Assumptions

- Telegram reports for a private chat whether the other side is a bot, a deleted account, the
  service account, or the user themselves; this is how eligibility (FR-003) is decided. Secret
  chats are not visible to the agent and are out of scope.
- A chat becomes a work-chat record lazily (on its first new message, on exclusion, or at style
  import), not for every dialog up front; this keeps stored data minimal on large accounts.
- Per-user limits on the number of work chats (e.g., per subscription plan, 005) do not exist
  yet; if they are introduced, chats picked up by the mode count towards them and a later spec
  decides what happens at the limit.
- Retention of stored messages, the access gate, pause/OOO and working-hours rules apply to
  picked-up chats unchanged.
- The mode does not change how drafts are approved (Principle I): every draft still needs the
  user's approval.
- The style import limit (FR-016) keeps setup time and Telegram rate-limit exposure close to
  today's; exact default can be tuned in the plan.
- Spec 006 lands before or with this feature; if it does not, groups keep their current rule —
  this feature does not touch groups either way.
