# Feature Specification: Search Over Earlier Conversations

**Feature Branch**: `004-conversation-search`

**Created**: 2026-10-22

**Status**: Draft

**Input**: User description: "Search over earlier conversations (GitHub issue #18). The service has no search over conversations today: no bot command for the user and no search for the agent while drafting a reply. When drafting, the agent only sees a fixed window: up to 40 most recent messages in the chat and up to 20 most recent messages from the same contact in other chats, only within the message retention period (30 days by default), only messages received after the service was connected, and only from enabled work chats. If a client refers to something discussed earlier ("as we agreed last month", "send the same invoice"), the agent cannot find it. Needed: (1) search for the agent while drafting; (2) a search command in the control bot (later removed from scope, see Clarifications). Constraints: message text is stored encrypted, must respect the data retention policy, scope limited to enabled work chats, Telegram rate limits."

## Clarifications

### Session 2026-10-22

- Q: How far back can search go? → A: The full history of the enabled work chats as kept by
  the messaging platform, searched on demand without storing it on our side (not limited to
  the 30-day retention window or to the time the service was connected).
- Q: How are queries matched to messages? → A: Hybrid: the incoming message is turned into several word-based queries that cover paraphrases and word forms;
  the matches are then ranked by relevance to the question. No meaning index is stored.
- Q: Should the agent search earlier conversations for every draft or only when needed? → A:
  Only when needed: a cheap check of the incoming message decides whether it refers to the
  past or asks for facts missing from the recent window; search runs only then.
- Q: In group work chats, which messages may a draft search use? → A: The whole group is
  searched, but only messages linked to the sender or the user (replies to or from them,
  mentions of them, the same reply thread) are added to the draft context.
- Q: What should a search command in the control bot return? → A: No search command: it is
  removed from scope; the user searches with Telegram's own search. This feature covers only
  the agent's search while drafting.

## Context

Today a draft is grounded only in a fixed window of recent messages: the last 40 messages of
the chat and the last 20 messages the same contact wrote in other work chats, all within the
30-day retention window and only from the time the user connected the service. Anything
older or further back in a busy chat is invisible to the agent, so a reply to "as we agreed
last month" either misses the agreement or, worse, guesses it (Principle III).

This feature lets the agent find earlier messages relevant to the incoming message while it
prepares a draft. A search command for the user in the control chat is out of scope: users
search their conversations with Telegram's own search.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Drafts use relevant earlier messages (Priority: P1)

A client writes "Please send the invoice again, same as last time" or "As we agreed, the
deadline stays Friday, right?". The relevant message is older than the recent-messages
window. While drafting, the agent looks for earlier messages in the conversation with this
contact that relate to the incoming message, adds the relevant ones to the draft context,
and cites them like any other source message. In the approval card the user sees which
earlier messages the draft relied on.

**Why this priority**: grounded replies are the core promise of the product; references to
past agreements are common in work chats and today they are either missed or guessed.

**Independent Test**: seed a work chat where the agreement is outside the recent window,
send a message referring to it, and check that the draft cites the earlier message and
states the agreed fact correctly; with no relevant earlier message, the draft does not
invent one and leaves a placeholder or asks.

**Acceptance Scenarios**:

1. **Given** a work chat where the contact and the user agreed on a price 60 messages ago,
   **When** the contact writes "Is the price still as we agreed?", **Then** the draft cites
   the earlier message with the agreed price and the approval card shows that citation.
2. **Given** the same contact discussed an invoice in a different enabled work chat (e.g. a
   group), **When** they ask for "the same invoice" in a private chat, **Then** the agent
   can find and cite the message from the other enabled work chat.
3. **Given** no earlier message matches the reference, **When** the draft is prepared,
   **Then** the draft contains no invented details and marks the missing information with a
   placeholder (existing behavior), and the card notes that earlier conversations were
   searched without a match.
4. **Given** the search is temporarily unavailable (e.g. rate-limited), **When** a draft is
   prepared, **Then** the draft is still prepared from the recent window and the card says
   it was drafted without searching earlier conversations.

---

### User Story 2 - The user controls what is searched (Priority: P2)

The user can turn the agent's automatic search of earlier conversations on or off in
settings and can see, in the approval card, exactly which earlier messages were used.

**Why this priority**: transparency and control (Principles II and IV) but not required for
the first useful version, since search is limited to work chats the user already enabled.

**Independent Test**: turn the setting off, prepare a draft that would need an earlier
message, and verify that no search happens and the card says so.

**Acceptance Scenarios**:

1. **Given** the setting is off, **When** a draft is prepared, **Then** no earlier
   conversations are searched and the draft uses only the recent window.
2. **Given** the setting is on, **When** the user opens the draft card, **Then** every
   earlier message used is listed with chat and date.

---

### Edge Cases

- The contact refers to something that is not in the chat history at all (e.g. discussed by
  phone or in a non-work chat): no match; the draft must not guess (placeholder or question).
- A chat has years of history: search is limited by the 10-second draft budget (FR-S08) and
  by platform rate limits; the most recent relevant matches win.
- In a group, a third participant states the agreement ("Anton, the budget is approved at
  500k") in reply to the sender: the message is linked by the reply and may be cited; an
  unrelated message from the same participant is not used.
- Several earlier messages conflict (the price changed twice): the agent uses the most
  recent relevant one and the card shows all used messages so the user can check.
- The earlier message was deleted or edited in Telegram: search reflects the current state;
  a deleted message is never cited.
- A chat was a work chat and then was disabled: its messages are no longer searchable.
- A search returns a very large number of matches: only the most relevant few are added to
  the draft context.
- Queries and messages in Russian and English, including different word forms ("счёт",
  "счета", "счетом"), must still match.
- Messages that contain only media or files: matched by caption or file name if any;
  otherwise not searchable.
- Incoming message text tries to instruct the agent to search for and reveal other chats
  (prompt injection): the search scope stays limited to this contact's work conversations
  for drafts, and search results never leave the user's own approval card.
- Rate limits from the messaging platform: the draft falls back to the recent window
  (Story 1, scenario 4).

## Requirements *(mandatory)*

### Functional Requirements

**Search scope and data**

- **FR-S01**: Search MUST cover only chats the user enabled as work chats, at the time of the
  search. Messages in non-work chats MUST never be searched, returned, or sent to an LLM.
- **FR-S02**: Search MUST cover the full history of each enabled work chat as kept by the
  messaging platform, including messages from before the service was connected and older
  than the retention window. Search MUST run on demand against that history; messages found
  this way MUST NOT be stored by the service beyond the preparation of the draft, except as
  part of a draft that follows the existing draft-retention rules.
- **FR-S03**: The feature MUST NOT add any stored index or copy of message content.
- **FR-S04**: The incoming message MUST be turned into up to 5 word-based search
  queries that cover likely paraphrases and Russian/English word forms (e.g. "the same
  invoice" → "счёт", "счет на оплату", "invoice"); the combined matches MUST then be ranked
  by relevance to the incoming message, and only the most relevant are used (FR-S06).
- **FR-S04a**: Turning a message into search queries and ranking the matches MAY use an LLM.
  The ranking call MUST receive only the incoming messages and the found snippets; the
  decision-and-queries call MUST NOT receive more data than context selection receives today.
  Both calls MUST be logged like other LLM calls (route, model, tokens, latency, cost) without
  content.

**Search while drafting (Story 1)**

- **FR-S05**: When preparing a draft, the agent MUST first decide whether the incoming
  message refers to earlier conversations ("as we agreed", "the same invoice", "last time")
  or asks for facts that are not in the recent window. Only then MUST it search earlier
  messages of the conversations with the sender (this chat and the other enabled work chats
  defined in Assumptions: "Search for drafts is scoped …") for content relevant to the incoming message. Messages that need
  no earlier context (acknowledgements, greetings, questions answered by the recent window)
  MUST NOT trigger a search.
- **FR-S05a**: The decision whether to search MUST be logged (searched / not needed) without
  message content, so the share of drafts that trigger a search can be measured.
- **FR-S05b**: In enabled group work chats, the draft search MAY cover messages from any
  participant, but only messages linked to the sender or the user MUST be added to the draft
  context: messages written by them, replies to or from their messages, messages that
  mention them, and messages in the same reply thread. Other participants' unrelated
  messages MUST NOT be sent to the LLM.
- **FR-S06**: At most 10 earlier messages found by search MUST be added to the draft context,
  most relevant first, each citable with the same stable reference as other source messages.
- **FR-S07**: Every fact the draft takes from an earlier message MUST be cited; the approval
  card MUST list the earlier messages used, with chat name and date.
- **FR-S08**: If search fails, is rate-limited, or takes longer than 10 seconds, the draft
  MUST be prepared from the recent window only and the card MUST say "drafted without
  searching earlier conversations"; the draft MUST NOT be delayed beyond that.
- **FR-S09**: If search finds nothing relevant, the draft MUST NOT invent the referenced
  information; it MUST use the existing placeholder / ask-the-user behavior.
- **FR-S10**: Only the found messages (not whole conversations) MUST be sent to the LLM
  provider, in line with data minimization.

**Search from the control chat**

- **FR-S11–FR-S15**: Removed (2026-10-22): no search command in the control bot; users rely
  on Telegram's own search.

**Control and observability (Story 2)**

- **FR-S16**: Users MUST be able to turn automatic search while drafting on or off in
  settings; default is on.
- **FR-S17**: Each search MUST be logged with user id, turn id, number of results, duration and outcome (ok / no match / failed / rate-limited); queries and message
  content MUST NOT appear in logs.
- **FR-S18**: Search MUST respect the messaging platform's rate limits: on a rate limit, the
  draft falls back (FR-S08); the service MUST NOT retry in a tight loop.

### Key Entities

- **Search request**: the draft it serves, the expanded word-based queries derived from the
  incoming message, and the scope (the sender's conversations in enabled work chats). Not
  stored after the search.
- **Search hit**: a reference to one earlier message in a work chat: chat, sender, date,
  snippet; hits are ordered by relevance; for drafts it becomes a cited source message.
- **Search setting**: per user, whether automatic search while drafting is on.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In the evaluation set, for messages that refer to an earlier agreement outside
  the recent window, at least 80% of drafts cite the correct earlier message (today: 0%).
- **SC-002**: In the evaluation set, drafts with no matching earlier message contain no
  invented referenced facts (100%, same bar as today's groundedness checks).
- **SC-003**: Drafting with search adds no more than 5 seconds to the time until the draft
  card appears for 95% of drafts.
- **SC-004**: Zero results from non-work chats in any search (verified by tests and by audit
  of search logs).
- **SC-005**: Draft rejection rate for messages that reference earlier conversations drops
  by at least 30% compared with the month before release.

## Assumptions

- The work-chat selection, retention window (30 days), message encryption and data-deletion
  flows from spec 001 stay as they are; searching older history on demand does not extend
  what the service stores.
- The messaging platform's own search over a chat's history is word-based; finding
  paraphrases relies on query expansion (FR-S04), so some references phrased very
  differently may still be missed.
- Search for drafts is scoped to the sender's conversations: this chat, the private chat with
  the sender (if it is an enabled work chat), and up to 2 other enabled group work chats where
  the sender wrote within the last 30 days, most recent first. Groups where the sender wrote
  only earlier are not searched. Searching other contacts' conversations for a draft is out of
  scope.
- Group chats are searched over their platform history (FR-S02) with the draft-context
  filter of FR-S05b; the feature does not change which group messages trigger a draft.
- Media is searchable only by caption or file name; contents of files and images are out of
  scope.
- Supported languages are those of the product today: Russian and English.
- Search inside Jira or Trello comments is out of scope (covered by specs 003 and #6).
- A search command for the user in the control chat is out of scope; users search with
  Telegram's own search.
- Limits (10 hits in context, 10-second draft timeout) are initial values and may be tuned during planning.
