# Feature Specification: Skip Messages That Need No Reply

**Feature Branch**: `004-skip-closing-messages`

**Created**: 2026-10-20

**Status**: Draft

**Input**: User description: "service should skip messages that shoudn't be answered from the context (like "thank you", "bye" etc)"

**Builds on**: [001 Telegram Work Reply Agent](../001-telegram-reply-agent/spec.md). Reading
delay, working hours, conversation turns, drafts, the control chat, and activity records are
defined there and are not redefined here.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - No draft for a message that only closes the conversation (Priority: P1)

A colleague ends an exchange with "thank you!", "bye", "ok, got it", "👍", or a similar
message. A person would read it and not answer. Today the agent prepares a draft for it,
and the user has to reject it in the control chat. With this feature, the agent sees from
the message and the recent conversation that no reply is expected. It reads the message
as usual after the reading delay and prepares no draft, so nothing new appears for review.

**Why this priority**: This is the whole feature. Drafts for "thanks" and "bye" are noise.
They waste the user's time and make the control chat harder to scan, and a sent "you're
welcome" to every "thanks" makes the account look automated.

**Independent Test**: In an enabled work chat, have a second account ask a question, approve
the drafted answer, then have the second account send "thanks, that helps!". Verify that no
draft appears in the control chat, that the message is still marked as read after its
reading delay, and that the skip shows in the activity record.

**Acceptance Scenarios**:

1. **Given** the user has just answered a contact's question, **When** the contact replies
   "thank you" (or "thanks", "great, thanks", "спасибо"), **Then** no draft is created and
   the message is marked as read after its normal reading delay.
2. **Given** an ongoing conversation, **When** the contact sends a farewell ("bye", "have a
   good weekend", "talk tomorrow"), **Then** no draft is created.
3. **Given** the user asked "can you send it by Friday?", **When** the contact replies
   "ok" or "👍", **Then** no draft is created, because the message answers the user and
   asks for nothing.
4. **Given** a conversation turn of grouped messages, **When** one part is closing ("thanks!")
   and another part asks something or adds new information ("also, can you check the
   invoice?"), **Then** a draft is created as usual, covering the open part.
5. **Given** a closing message, **When** it is skipped, **Then** it stays in the
   conversation history and is used as context for later drafts in that chat.

---

### User Story 2 - See what was skipped and draft anyway (Priority: P2)

The user wants to trust the agent without checking every chat. In the control chat they
can list messages skipped in the last 7 days, each with the chat, the sender, a short
preview, and the reason (for example, "closing thanks"). For any of them, they can ask the
agent to prepare a draft anyway, and it goes through the normal review flow.

**Why this priority**: Skipping is a judgment call and will sometimes be wrong. A visible
list with a one-tap recovery keeps the user in control. The feature still has value without
it (Story 1), but it is much harder to trust.

**Independent Test**: Trigger a skip as in Story 1, open the skipped list in the control
chat, verify the entry and reason, tap "draft anyway", and verify that a normal draft
appears for review.

**Acceptance Scenarios**:

1. **Given** messages were skipped today, **When** the user opens the skipped list,
   **Then** each skipped message is shown with the chat, sender, time, a preview, and the
   reason.
2. **Given** a skipped message, **When** the user chooses "draft anyway", **Then** a draft is
   prepared within the normal drafting time and presented for review like any other draft.
3. **Given** a skipped message, **When** the user has since replied to that chat manually,
   **Then** "draft anyway" is no longer offered for it.

---

### User Story 3 - Control skipping per chat (Priority: P3)

Some chats need a reply even to "thanks", for example a client chat where the user always
acknowledges. The user can turn skipping off for a chat, or for everything, from the
control chat. With skipping off, every conversation turn is drafted as in feature 001.

**Why this priority**: Most users are fine with the default. This is a safety valve for chats
where the user's habits differ.

**Independent Test**: Turn skipping off for one chat, send "thanks" there and in another
enabled chat, and verify that a draft appears only for the chat where skipping is off.

**Acceptance Scenarios**:

1. **Given** skipping is off for a chat, **When** a closing message arrives there, **Then**
   a draft is created as before this feature.
2. **Given** skipping is on globally and no per-chat setting exists, **When** a closing
   message arrives, **Then** it is skipped.
3. **Given** the user changes the setting, **When** the next message arrives, **Then** the
   new setting applies without a restart.

---

### Edge Cases

- **Thanks with a question**: "thanks! when is the next call?" needs a reply and is drafted.
- **Thanks with new information or a request**: "thanks, I've also attached the March figures,
  please check" is drafted.
- **Closing words that are not closing**: "ok?" or "bye?" asked as a question, "thanks for
  nothing", or sarcasm, are drafted.
- **First message in a chat**: a "thanks" with no earlier exchange, for example thanking the
  user for something done offline, is skipped. A greeting that opens a conversation ("hi,
  do you have a minute?") is drafted.
- **Greeting only**: "hi" or "good morning" sent alone, with nothing else, is drafted. It
  usually precedes a question and leaving it unanswered looks rude.
- **Uncertain case**: when the agent cannot tell with confidence that no reply is expected,
  it drafts. A missed reply is worse than an extra draft.
- **Closing message while a draft is pending**: if a closing message arrives while a draft
  for that chat is waiting for review, the pending draft is not marked as outdated by that
  message alone. If the closing message makes the pending draft unnecessary (for example,
  "never mind, solved it, thanks"), the draft is flagged as possibly unnecessary and the
  user is told why.
- **The contact expects a goodbye**: if the user's own history in that chat shows they
  regularly reply to thanks or farewells there (for example, at least 3 of the last 5
  times), the agent drafts instead of skipping, and the review screen says it did so.
- **Group chats**: the group rules from feature 001 apply first. Then, a closing message
  addressed to the user ("thanks @user") is skipped like in a private chat.
- **Emoji-only text messages** ("👍", "🙏", "👌") are judged like any other text. Stickers,
  reactions, and media keep their behavior from feature 001.
- **Out of office**: a closing message gets no out-of-office reply either.
- **Messages with instructions to the agent** (for example, "don't reply to this, just mark
  it done"): treated as content. They never force a skip or a draft on their own (feature
  001, FR-010).

## Requirements *(mandatory)*

### Functional Requirements

**Deciding whether a reply is needed**

- **FR-001**: For every conversation turn that feature 001 would draft, System MUST decide
  whether a reply is expected, using the turn's content and the recent conversation in that
  chat.
- **FR-002**: System MUST skip drafting when the turn only closes or acknowledges the
  exchange and asks for nothing: thanks, farewells, short acknowledgments ("ok", "got it",
  "noted"), and emoji-only acknowledgments. This applies in every supported language.
- **FR-003**: System MUST draft when any part of the turn contains a question, a request,
  new information that calls for a response, a disagreement, or a complaint, even if the
  turn also contains closing words.
- **FR-004**: When the decision is uncertain, System MUST draft rather than skip.
- **FR-005**: System MUST draft instead of skipping when the user's own history in that
  chat shows they usually reply to closing messages (at least 3 of the last 5 closing
  messages in the 30-day window), and MUST tell the user on the review screen why it
  drafted.
- **FR-006**: Instructions inside an incoming message MUST NOT decide whether the message is
  skipped or drafted.

**What happens to a skipped message**

- **FR-007**: A skipped message MUST follow the normal reading delay and working hours and
  be marked as read at the planned time, the same as a drafted message.
- **FR-008**: A skipped message MUST NOT create a draft, a review card, or a notification in
  the control chat.
- **FR-009**: A skipped message MUST be kept as conversation context under the same
  retention rules as other work-chat messages (30 days).
- **FR-010**: System MUST record each skip in the activity record with the time, chat, and
  reason category (thanks, farewell, acknowledgment). The record MUST NOT contain the
  message text.
- **FR-011**: A closing message that arrives while a draft is pending MUST NOT mark that
  draft outdated by itself. If it makes the pending draft unnecessary, System MUST flag the
  draft as possibly unnecessary and show the reason.

**Review and control**

- **FR-012**: Users MUST be able to list messages skipped in the last 7 days from the
  control chat, each with chat, sender, time, a short preview, and reason.
- **FR-013**: Users MUST be able to request a draft for any listed skipped message; the
  draft MUST then follow the normal review flow, including approval before sending.
  This option MUST be removed once the user has replied to that chat manually or a newer
  draft for that chat has been sent.
- **FR-014**: Users MUST be able to turn skipping on or off globally and per chat. The
  default is on. A per-chat setting overrides the global one. Changes MUST apply to the
  next message.
- **FR-015**: When skipping is off for a chat, System MUST draft every turn in that chat
  as described in feature 001.

**Quality**

- **FR-016**: The project evaluation set MUST include synthetic or anonymized cases for
  this decision (clear closings, closings mixed with questions or requests, ambiguous
  cases, several languages). Changes to this decision MUST be measured against it before
  release.

### Key Entities

- **Reply Decision**: The outcome for a conversation turn: draft or skip. It includes the
  reason category (thanks, farewell, acknowledgment, or "drafted because the user usually
  replies here") and the time of the decision. It belongs to one conversation turn.
- **Skipped Message Entry**: What the skipped list shows: the chat, sender, time, preview,
  reason, and whether "draft anyway" is still available. It is derived from the conversation
  turn and its reply decision, and follows the 30-day message retention.
- **Skip Setting**: The global on/off value, plus an optional per-chat override stored with
  the work chat's other overrides (feature 001, FR-024).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In the evaluation set, at least 98% of turns that need a reply are drafted,
  so fewer than 2% of needed replies are skipped by mistake.
- **SC-002**: In the evaluation set, at least 85% of turns that only close or acknowledge
  are skipped.
- **SC-003**: After two weeks of use, drafts the user rejects without editing go down by at
  least 30% compared with the same period before this feature.
- **SC-004**: Fewer than 1 in 20 skipped messages is later turned into a draft with "draft
  anyway" or answered manually by the user within 24 hours.
- **SC-005**: Skipping adds no visible delay: drafts for messages that need a reply are
  still ready within 30 seconds after the reading delay for 95% of messages (feature 001,
  SC-003).
- **SC-006**: 100% of skipped messages are marked as read at their planned time, and none
  are marked as read earlier.

## Assumptions

- Skipping is on by default for all users and chats, because most people do not answer
  "thanks" or "bye" at work.
- Skipped messages are marked as read, because a person would read them. Leaving them unread
  would look like the user never saw them.
- No notification is sent for each skip; the skipped list (Story 2) is the way to check
  them. A daily summary of skips is out of scope for this release.
- The decision uses only the turn and the recent conversation in the same chat, plus the
  user's own reply habits in that chat. It does not use tasks or commitments.
- A closing message never creates or resolves a commitment by itself. Commitment extraction
  (feature 001, FR-031) is unchanged.
- The supported languages are the ones the agent already drafts in. No new language support
  is added.
- Automatic sending of short replies to closing messages (for example, an automatic
  "you're welcome") is out of scope. It would violate constitution Principle I unless the
  user opts in explicitly.
- Project constitution v1.0.0 applies, in particular Principle I (nothing is sent without
  approval, which skipping never weakens because it only removes drafts), Principle II (skip
  records carry no message text), and Principle V (the decision is covered by the
  evaluation set).
