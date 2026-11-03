# Feature Specification: Group Chats — Draft Only When Addressed

**Feature Branch**: `006-group-mention-only`

**Created**: 2026-10-23

**Status**: Draft

**Input**: User description: "issue #25 — In group chats answer messages where user were tagged
only. In group work chats the agent should prepare a draft only for messages where the user is
explicitly tagged (an @mention of the user)."

**Builds on**: `specs/001-telegram-reply-agent` (FR-003 group intake, per-chat settings in
`/chats`, Edge Cases "message in a group not addressed to the user").

**Changes**: 001 FR-003, the matching 001 edge case, and the group-mode setting shown in chat
settings and setup.

## Clarifications

### Session 2026-10-23

- Q: Should a reply to the user's message that does not tag them still count? → A: Yes. In a
  group, a draft is prepared when the message tags the user or replies to one of the user's
  messages.
- Q: Should the per-chat "every message" group mode stay as an explicit opt-in, or be removed?
  → A: Removed. Chats currently set to it move to the standard rule (tag or reply).

## Problem

In a group work chat the agent currently opens a conversation turn, and so prepares a draft,
when the user is tagged, when the message is a reply to one of the user's messages, or when
the chat is set to draft for every message.

The "every message" mode turns a busy group into a stream of drafts the user did not ask for,
and it makes group behaviour depend on a per-chat switch that is easy to forget. The user wants
one simple, predictable rule for every group: a draft appears only when someone addresses the
user directly, by tagging them or by replying to their message. Everything else in the group
is context.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A draft appears only when I am addressed in a group (Priority: P1)

The user has a group work chat. When a colleague's message tags the user, or replies to one of
the user's messages, the agent prepares a draft as usual. Any other message in that group
produces no draft; it is still kept as context for later drafts in that chat.

**Why this priority**: This is the core rule of the feature: group drafts only for messages
directly addressed to the user.

**Independent Test**: In a group work chat, send a message that tags the user, a reply to the
user's message without a tag, and a plain message that does neither. Verify exactly two drafts
appear (for the tag and the reply), and that the plain message is used as context.

**Acceptance Scenarios**:

1. **Given** a group work chat, **When** a colleague sends a message that tags the user by
   username, **Then** a conversation turn is opened and a draft is prepared after the usual
   reading delay.
2. **Given** a group work chat, **When** a colleague tags the user by a name link (the user
   has no username, or the sender picked the user from the member list), **Then** a draft is
   prepared exactly as in scenario 1.
3. **Given** a group work chat, **When** a colleague replies to one of the user's messages
   without tagging the user, **Then** a draft is prepared exactly as in scenario 1.
4. **Given** a group work chat, **When** a colleague sends a message that neither tags the
   user nor replies to the user's message, **Then** no turn is opened, no draft is prepared,
   and the message is stored as context only.
5. **Given** an unaddressed group message stored as context, **When** a later message in the
   same group addresses the user, **Then** the draft for that message can use the earlier
   message as conversation context.
6. **Given** a private work chat, **When** a message arrives, **Then** a draft is prepared as
   today; this feature does not change private chats.

---

### User Story 2 - No more "every message" mode for groups (Priority: P1)

The per-chat option to draft for every message in a group goes away. Groups that had it
switched on follow the same rule as all other groups from now on, and the user can see that in
the chat settings.

**Why this priority**: This is the behaviour change the issue asks for; without it, groups set
to "every message" keep producing drafts for unaddressed messages.

**Independent Test**: Take a group that was set to "every message" before the change. After
the change, send an unaddressed message there and verify no draft appears; send a tagged one
and verify a draft appears. Open the chat's settings and verify there is no group-mode switch.

**Acceptance Scenarios**:

1. **Given** a group work chat that was set to "every message" before the change, **When** an
   unaddressed message arrives after the change, **Then** no draft is prepared and the message
   is stored as context only.
2. **Given** the same group, **When** a message tags the user or replies to their message,
   **Then** a draft is prepared.
3. **Given** any group work chat, **When** the user opens its settings or goes through setup,
   **Then** no option to draft for every message is offered, and the drafting rule for groups
   is described in words that match it (e.g., "only when you are tagged or replied to").
4. **Given** an old settings button for the removed mode is pressed (e.g., on a card sent
   before the change), **When** the agent receives it, **Then** nothing changes and the user
   sees a short note that the option is no longer available.

---

### Edge Cases

- A message tags several people including the user: addressed; a draft is prepared.
- A message tags someone else with a similar name or username: not addressed (unless it is
  also a reply to the user's message); no draft.
- A message mentions the user's name as plain text without a tag (e.g., "ask Anna"): not
  addressed; no draft.
- A reply to someone else's message that quotes or forwards the user's message: not a reply to
  the user's message; no draft unless it tags the user.
- A tag appears only inside a forwarded message: not counted as addressing the user, since the
  sender did not address the user.
- Several unaddressed messages followed by an addressed one from the same sender within the
  grouping window: the addressed message opens the turn; the earlier ones are context only.
- After an addressed message opens a turn, the same sender sends unaddressed follow-ups within
  the grouping window: they join that open turn as today (001 FR-002), because they continue
  the same address to the user.
- A draft from an addressed message is pending review and a new unaddressed message arrives
  in the group: it does not mark the draft outdated, unless it is a follow-up from the turn's
  sender that joins the turn under the existing rules.
- The user's own messages in the group never open a turn (unchanged).
- Turns opened before this change (for example, in "every message" mode) are finished as they
  are; the new rule applies only to messages that arrive after it ships.
- A media-only message that addresses the user: handled like any other addressed media-only
  message (handled manually, user notified), as today.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: In group work chats, System MUST open a conversation turn (and so prepare a
  draft) only for a message that addresses the user: it tags the user (FR-002) or it is a
  reply to one of the user's messages. This replaces 001 FR-003.
- **FR-002**: A message tags the user when its own text or caption contains a tag that
  resolves to the user's Telegram account: an @username tag of the user's current username,
  or a name-link tag pointing to the user's account.
- **FR-003**: Group messages that do not address the user MUST NOT open a turn; they MUST be
  stored as context for that chat and remain available as conversation context for later
  drafts, as today.
- **FR-004**: The per-chat "every message" group mode MUST be removed. There MUST be no way to
  make a group draft for unaddressed messages.
- **FR-005**: Group chats set to "every message" before the change MUST follow the FR-001 rule
  from the moment the change ships, without any action by the user.
- **FR-006**: Chat settings and setup MUST no longer offer a group-mode choice, and MUST
  describe the group rule ("only when you are tagged or replied to") in every supported
  interface language.
- **FR-007**: A settings action for the removed mode that still arrives (e.g., from a message
  sent before the change) MUST change nothing and MUST tell the user the option is no longer
  available.
- **FR-008**: Follow-up messages from the same sender within the grouping window MUST keep
  joining a turn that an addressed message already opened (001 FR-002 is unchanged).
- **FR-009**: Private work chats MUST be unaffected by this feature.
- **FR-010**: The 001 specification (FR-003, the group edge case, and the per-chat settings
  that list group drafting behaviour) MUST be updated to match this feature, so the two
  documents do not contradict each other.
- **FR-011**: The intake decision for each group message (opened a turn / stored as context,
  and the reason: tagged, reply to the user, not addressed) MUST be observable in structured
  logs without message content (constitution, Principles II and VI).

### Key Entities

- **Group work chat**: A group the user marked as a work chat. It no longer has a group mode;
  every group follows the same drafting rule.
- **Incoming group message**: A message in a group work chat, with its sender, whether it tags
  the user (FR-002), and whether it replies to one of the user's messages.
- **Conversation turn**: Unchanged; opened only by a message that passes the intake rule.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In group work chats, 100% of drafts come from messages that tag the user or
  reply to the user's message (checked against a test set of tagged, reply, and unaddressed
  messages, including groups formerly set to "every message").
- **SC-002**: 0 drafts are prepared for unaddressed group messages, in any group.
- **SC-003**: 100% of tagged messages and replies to the user's messages in group work chats
  still produce a draft (no regression for real requests), including name-link tags for users
  without a username.
- **SC-004**: Unaddressed group messages remain available as context: a draft for an addressed
  message can reference a fact stated only in an earlier unaddressed message in the same group.
- **SC-005**: Users can tell how groups behave from the chat settings alone: the settings text
  states the rule, and no removed option is shown.

## Assumptions

- "Reply to the user's message" means a message sent as a direct reply to a message the user
  wrote in that group; replies to other people's messages do not count, even if they quote the
  user.
- Plain-text mentions of the user's name are not tags; recognising them would require guessing
  and would bring back the noise this feature avoids.
- Users who had "every message" on are not notified separately; the settings text shows the
  new rule. Existing open turns and pending drafts are not re-evaluated when the change ships.
- No new settings are added.
- Tests and the evaluation set are synthetic (constitution, Principle II); deterministic
  intake rules get unit tests (Principle V).
