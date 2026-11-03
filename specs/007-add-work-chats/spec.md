# Feature Specification: Add Work Chats After Setup

**Feature Branch**: `007-add-work-chats`

**Created**: 2026-10-23

**Status**: Draft

**Input**: GitHub issue #32, "Add work chats after setup: ➕ button in /chats". Work chats can be
chosen only once, during setup. After setup, `/chats` lists the chosen chats (they can be
configured or disabled) but no new chat can be added without redoing setup.

**Implements**: issue #32.

**Builds on**: `specs/001-telegram-reply-agent` (FR-001 monitoring only enabled work chats and
nothing from before, FR-022 setup, FR-024 per-chat settings in `/chats`, FR-027 activity
history, FR-034/FR-035 own-message import and style profiles, the 🔄 Rebuild in `/profile`),
`specs/002-telegram-qr-login` (the Telegram connection and its "unavailable" states),
`specs/005-beta-subscription-access` (the access gate).

**Related work**:

- Issue #29 (setup builds per-contact profiles) and issue #30 / PR #31 (profile sampling caps and
  a retry on truncated builds) make the profile update in this feature better, but are not
  required for it: the update reuses the 🔄 Rebuild path, which already builds contact profiles
  and will get the caps and the retry from #30.
- `specs/006-group-mention-only` (branch `006-group-mention-only`): a group chat added here
  follows the group rule of 006 (a draft only when the user is tagged or replied to). This
  feature adds no group-mode choice.
- Issue #15 / spec 008 ("Work account": every private chat is a work chat). When that mode is
  on, private chats no longer need to be added one by one; the picker of this feature stays the
  way to add group chats and to see what is not yet a work chat. 008 decides what the picker
  lists in that mode; this feature does not design it.

## Clarifications

### Session 2026-10-23

No questions were asked; the issue fixes the behaviour. Decisions taken as informed defaults
(see Assumptions): the profiles are updated automatically in the background after adding (the
issue left the choice to the plan); disabled chats are not listed in the picker; Done with
nothing selected closes the picker without changes; there is no limit on the number of work
chats in this feature.

## Problem

A user who joins a new project chat, or starts a new private work conversation after setup,
cannot make the agent watch it. The only path is to redo setup, which means reconnecting the
Telegram account and re-entering settings. In early testing users had messages from chats
outside their list go unhandled, and it was not clear why or how to fix it.

This feature adds a **➕ Add work chats** button to `/chats`. It opens the same chat picker
as setup. The chats the user picks become enabled work chats at once, and the user's own
messages in them are learned for the style profiles.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Add a new work chat from /chats (Priority: P1)

The user finished setup a week ago with three work chats. Today they joined a new project
group. They send `/chats`, tap **➕ Add work chats**, see their Telegram chats (without the
three already added), tick the new group and tap **Done**. The bot confirms "Added 1 chat"
and shows the updated `/chats` list. From then on, a message in that group that tags the user
gets a draft as in any other work chat.

**Why this priority**: this is the whole point of the issue: drafts for a new chat without
redoing setup.

**Independent Test**: with a set-up user and a fake Telegram account that has five dialogs,
two of them already work chats: open `/chats`, tap ➕, check that only the three other dialogs
are listed, pick one, tap Done. Check the confirmation, the updated list, a new enabled work
chat, and that a new incoming message in that chat produces a draft.

**Acceptance Scenarios**:

1. **Given** a set-up user with work chats, **When** they send `/chats`, **Then** the list of
   their work chats is shown with a **➕ Add work chats** button under it.
2. **Given** a set-up user whose work chat list is empty, **When** they send `/chats`, **Then** the "no work chats" text is shown with the
   **➕ Add work chats** button.
3. **Given** the user taps ➕, **When** their Telegram chats load, **Then** they see the same
   paged picker as in setup (toggles, ◀️ ▶️ pages, **Done (N selected)**), listing only chats
   that are not work chats yet.
4. **Given** the user ticked two chats, **When** they tap **Done**, **Then** both become
   enabled work chats, the picker message says "Added 2 chats", and the updated `/chats` list
   follows.
5. **Given** a chat added a minute ago, **When** a new message arrives in it, **Then** it is
   handled like in any other work chat (reading delay, draft, approval). Messages that arrived
   before the chat was added get no draft.
6. **Given** a group chat added this way, **When** a message in it neither tags the user nor
   replies to them, **Then** no draft is prepared (spec 006 group rule).

---

### User Story 2 - New chats count for my style (Priority: P1)

After adding chats, the user's own messages in them (the last 6 months, as at setup) are
learned, so the global style profile and the choice of contacts with their own profile take
the new chats into account. The user does not need to remember to press 🔄 Rebuild.

**Why this priority**: without it, drafts in a new chat use a profile that never saw how the
user writes there, and a busy new contact can never get a profile of its own until the weekly
refresh slowly catches up (001 FR-034/FR-035).

**Independent Test**: add a chat where the fake account has 30 own messages in the last 6
months. Check that the user is told the profiles are being updated, then gets a "profiles
updated" note, that the global profile is a new version built with the new messages, and that
the new contact has its own profile if it ranks in the top 20.

**Acceptance Scenarios**:

1. **Given** the user added chats, **When** the confirmation is shown, **Then** it says that
   their own messages in the new chats are being learned in the background and that they will
   be told when it is done.
2. **Given** the update finished, **Then** the user gets one note with the same content as
   after 🔄 Rebuild (global profile from N messages, K contact profiles).
3. **Given** the added chats contain none of the user's own messages in the import period,
   **Then** no profile is rebuilt and the user is told there was nothing new to learn.
4. **Given** the update fails (Telegram or the model is unavailable), **Then** the added chats
   stay added and enabled, the existing profiles are unchanged, and the user is told they can
   retry with 🔄 Rebuild in `/profile`.
5. **Given** a profile rebuild is already running for the user, **When** they add chats,
   **Then** the update for the new chats starts after it finishes, so the new chats are never
   silently left out.

---

### User Story 3 - Cancel, already-added chats and unavailable Telegram (Priority: P2)

The user can leave the picker at any time. Chats already in the list cannot be added twice. If
the Telegram account is not connected or the chat list cannot be loaded, the user is told so
and nothing changes.

**Why this priority**: these are the failure and safety paths of Story 1; Story 1 is usable
without them only in the happy case.

**Independent Test**: open the picker and send `/cancel`; open it again and send `/settings`;
make the fake gateway fail on listing chats and tap ➕; disconnect the Telegram account and
tap ➕. Each time check the message and that no work chat was added or changed.

**Acceptance Scenarios**:

1. **Given** the picker is open, **When** the user sends `/cancel`, **Then** the picker closes
   with "Nothing was added" and no work chat changes.
2. **Given** the picker is open, **When** the user sends another command (for example
   `/settings`), **Then** the picker closes without adding anything and the command is
   answered as usual.
3. **Given** the picker was closed, **When** the user taps a button on the old picker
   message, **Then** they are told the list is closed and to open `/chats` again; nothing
   changes.
4. **Given** the user ticked nothing, **When** they tap **Done**, **Then** the picker closes
   with "Nothing was added".
5. **Given** a chat that is already a work chat (enabled or disabled), **Then** it is never
   listed in the picker; a disabled one is turned back on from its own settings screen in
   `/chats`, as today. The picker says so in one line.
6. **Given** the same chat was added meanwhile (for example by a double tap on Done), **When**
   Done is processed again, **Then** no second work chat is created for it and it is not
   counted again in the confirmation or the history.
7. **Given** the Telegram account is disconnected or its session was revoked, **When** the
   user taps ➕, **Then** they get the same "can't reach Telegram" text as in setup, with the
   way to reconnect, and nothing changes.
8. **Given** the chat list fails to load, **When** the user taps ➕, **Then** they get the
   same "couldn't reach Telegram, try again in a minute" text as in setup, and nothing changes.
9. **Given** every Telegram chat of the user is already a work chat, **When** they tap ➕,
   **Then** they are told there are no other chats to add.

---

### User Story 4 - Adding is recorded in /history (Priority: P3)

Adding chats shows in `/history` as one line, for example "➕ Work chats added: Project X,
Anna". No message content is shown.

**Why this priority**: the audit trail (001 FR-027) must cover changes to what the agent
reads; it is not needed for the feature to work.

**Independent Test**: add two chats, send `/history`, check the line with both titles and no
message content.

**Acceptance Scenarios**:

1. **Given** the user added two chats, **When** they send `/history`, **Then** one line shows
   the time, "➕ Work chats added" and the two chat titles.
2. **Given** Done added nothing new (all selected chats were added meanwhile), **Then** no
   history line is written.

---

### Edge Cases

- **Setup not finished**: a user still in setup (or who cancelled setup before its end) does
  not see the ➕ button; `/chats` behaves as today and setup is the way to pick chats.
- **Paused or out of office**: adding works; the new chats follow the current state (messages
  wait while paused, as in any other work chat).
- **Access lost**: the access gate (005) applies to `/chats`, the ➕ button and every picker
  button. A person without access gets the gate's answer and nothing is added; a picker that
  was open when access ended cannot add anything.
- **Chat renamed in Telegram**: the picker shows the current title; the added work chat stores
  it, as setup does.
- **Chat left or deleted in Telegram while the picker is open**: it can still be added; it
  simply receives no messages. Removing it is done from `/chats` as for any other chat.
- **Many chats**: the picker is paged the same way as in setup, however many chats there are.
- **Group chats**: added groups get no group-mode question; they follow the 006 rule.
- **Two pickers**: only the latest picker is live; buttons of an older picker message answer
  that the list is closed.
- **Data deletion meanwhile**: if the user deletes their data while the picker is open or the
  profile update runs, nothing is added and the update ends silently.
- **Work account mode (spec 008)**: when it is on, private chats are already work chats; how
  the picker presents them is defined by 008.

## Requirements *(mandatory)*

### Functional Requirements

**Entry point**

- **FR-001**: `/chats` MUST show a **➕ Add work chats** button under the list of work chats.
  When the user has no work chats, it MUST show the "no work chats" text together with the
  button.
- **FR-002**: The button MUST be shown only to users who have finished setup. Users still in
  setup see `/chats` as today.

**Picker**

- **FR-003**: The button MUST open the same chat picker as setup: the user's Telegram chats,
  paged, with a toggle per chat and a **Done (N selected)** button. Setup and `/chats` MUST
  use one shared picker, not two copies.
- **FR-004**: The picker opened from `/chats` MUST list only chats that are not work chats of
  the user yet, enabled or disabled. It MUST say in one line that chats already in `/chats`
  are not listed and that a disabled chat is turned back on from its settings.
- **FR-005**: When every chat of the user is already a work chat, the button MUST answer that
  there are no other chats to add, and open no picker.
- **FR-006**: If the Telegram account is not connected, or the chat list cannot be loaded,
  the user MUST get the same text as in setup for that case, including how to reconnect when
  the account is disconnected, and nothing MUST change.

**Adding**

- **FR-007**: On **Done**, every selected chat MUST become an enabled work chat of the user,
  with default settings. A chat that is already a work chat MUST NOT get a second record and
  MUST NOT be counted as added.
- **FR-008**: After Done, the picker message MUST say how many chats were added ("Added N
  chats"), and the updated `/chats` list MUST follow. With nothing selected, or nothing new,
  it MUST say "Nothing was added".
- **FR-009**: A newly added chat MUST be monitored from the moment it is added. Messages that
  arrived before MUST NOT get drafts (001 FR-001).
- **FR-010**: Group chats added this way MUST follow the group intake rule in force (spec 006:
  a draft only when the user is tagged or replied to).

**Leaving the picker**

- **FR-011**: `/cancel` MUST close the picker without changes. Any other command MUST close
  the picker without changes and then be answered as usual.
- **FR-012**: Buttons of a closed or replaced picker MUST NOT change anything; the bot MUST
  answer that the list is closed and point to `/chats`.

**Style**

- **FR-013**: After chats were added, the system MUST learn the user's own messages in the
  enabled work chats over the import period (the last 6 months by default, as at setup) and
  update the global profile and the
  contact profiles the same way as 🔄 Rebuild (001 FR-034/FR-035), so the new chats count for
  the global profile and the contact ranking. This MUST start automatically, in the
  background, without a further tap.
- **FR-014**: The confirmation MUST say that the user's messages in the new chats are being
  learned. When the update ends, the user MUST get one note: the same result note as after 🔄
  Rebuild; or "nothing new to learn" when the new chats contain none of the user's own
  messages in that period (then no profile is rebuilt); or, on failure, that the profiles are
  unchanged and 🔄 Rebuild in `/profile` can retry it.
- **FR-015**: The update MUST NOT run twice at the same time for a user. If a rebuild is
  running when chats are added, the update MUST run after it.
- **FR-016**: The update MUST keep the rules of a rebuild: the user's own rules are kept,
  removed contact profiles stay removed under the same conditions, and the latest weekly
  refresh can no longer be undone afterwards (as after 🔄 Rebuild).

**Access, audit, languages**

- **FR-017**: The access gate (005) MUST apply to the button, the picker and Done. Only users
  with access can add chats.
- **FR-018**: Each Done that added at least one chat MUST be recorded in the activity history,
  shown in `/history` as "➕ Work chats added" with the chat titles. The record MUST hold no
  message content.
- **FR-019**: Every new text and button MUST exist in English and Russian.
- **FR-020**: The 001 control-chat contract (FR-024, `/chats`) and the 001 spec MUST describe
  the new button and the add flow.

### Key Entities

- **Work chat** (existing): a Telegram chat the agent reads for the user. This feature adds
  new ones after setup; at most one per Telegram chat per user.
- **Chat picker** (session state, not stored long-term): the snapshot of the user's chats
  offered, the current selection and page, and whether it was opened by setup or by `/chats`.
- **Activity record** (existing): gains a "work chats added" kind that refers to the added
  chats, without message content.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A set-up user can add a new work chat in under 30 seconds and at most 4 taps from
  `/chats`, without redoing setup.
- **SC-002**: In tests, 100% of messages that arrive in a chat after it was added are handled
  like messages in other work chats, and 0 messages that arrived before get a draft.
- **SC-003**: In tests, adding a chat that is already a work chat (by double tap or a stale
  picker) never creates a second work chat record.
- **SC-004**: After adding a chat with at least 10 of the user's own messages in the import
  period, the user's profiles include those messages without any further action, and the user
  is told when it is done.
- **SC-005**: When Telegram is unavailable, cancelled or abandoned, 0 work chats are changed.
- **SC-006**: Every addition is visible in `/history` with the chat titles and no message
  content.

## Assumptions

- **Profile update choice** (left to the plan by the issue): automatic background update, the
  same work as 🔄 Rebuild, rather than asking "Rebuild profiles now?". Reason: the user added
  the chats to get drafts in them, and the drafts there are only as good as the profile; a
  question would usually be skipped. The cost is bounded: it runs only when chats were
  actually added and only when they contain own messages.
- The update reuses the 🔄 Rebuild import of all enabled work chats, not an incremental
  import of the new chats only, because the global profile is built from the newest messages
  across all chats and only one place builds profiles.
- Disabled chats are not "new": they are left out of the picker and re-enabled from their
  settings, as the issue says.
- Done with nothing selected closes the picker ("Nothing was added") instead of asking to pick
  at least one, because in `/chats` adding nothing is a valid outcome. Setup keeps its rule of
  at least one chat.
- There is no maximum number of work chats per user today, and 005 defines none. This feature
  adds none; if a limit is introduced later, the picker is the place to enforce it.
- New work chats get the defaults that setup gives (enabled, no overrides, the default group
  behaviour). Per-chat settings are changed from `/chats` afterwards.
- The picker lists the chats the Telegram account returns at the moment ➕ is tapped; chats
  created after that need a new tap.
- Removing a work chat completely (not just disabling it) is out of scope.
