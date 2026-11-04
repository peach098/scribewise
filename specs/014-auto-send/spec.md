# Feature Specification: Auto-Send for Trusted Chats

**Feature Branch**: `014-auto-send`

**Created**: 2026-11-04

**Status**: Draft

**Input**: User description: "автоотправка сообщений. предлагать ее пользователю только если больше 60% сообщений отправлено без правок, либо включение вручную" (auto-sending of messages; offer it to the user only when more than 60% of messages were sent without edits, or let the user turn it on manually)

## Context

Today every draft waits for the user's tap on **Send** (constitution Principle I). In chats
where the user keeps sending drafts unchanged, that tap adds delay and no review value.
This feature adds an **opt-in, per-chat auto-send mode**: Scribe offers it only where the
user's own track record shows the drafts are already good, and the user can also turn it on
by hand. It stays within Principle I: off by default, scoped to a chat, reversible at any
time, logged, and never applied to a draft that carries a hard stop.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Turn auto-send on manually for a chat (Priority: P1)

A user trusts Scribe in a specific work chat (for example, a teammate who only asks routine
status questions) and turns on auto-send for that chat in settings. From then on, eligible
drafts in that chat go out on their own after a short cancel window; the user still sees
each one in the bot chat and can stop or edit it before it leaves.

**Why this priority**: This is the core mechanism. Without it the offer in Story 2 has
nothing to enable. It is usable on its own: a user who knows what they want does not need
to wait for statistics.

**Independent Test**: Enable auto-send for one test chat, send an ordinary question into it,
and check that the draft card shows a countdown, that the reply is sent as the user when the
countdown ends, and that Cancel during the countdown leaves the draft pending as usual.

**Acceptance Scenarios**:

1. **Given** auto-send is off for a work chat, **When** the user opens that chat's settings
   and turns auto-send on, **Then** Scribe asks for one explicit confirmation that explains
   what will happen and, after confirmation, shows the chat as "auto-send on".
2. **Given** auto-send is on for a chat, **When** an eligible draft is prepared, **Then** the
   draft card in the bot chat says it will be sent automatically, shows the remaining cancel
   window, and offers **Send now**, **Edit**, **Don't send automatically** and **Reject**.
3. **Given** a card with a running cancel window, **When** the window ends without any action
   from the user, **Then** the reply is sent from the user's account like an approved reply,
   the card is updated to "sent automatically", and the action appears in `/history` as
   automatic.
4. **Given** a card with a running cancel window, **When** the user taps **Don't send
   automatically** or **Edit**, **Then** the countdown stops and the draft becomes an ordinary pending draft
   that needs the usual approval.
5. **Given** auto-send is on for a chat, **When** the user turns it off, **Then** no further
   drafts in that chat are sent automatically, and drafts whose cancel window is still
   running revert to ordinary pending drafts.

---

### User Story 2 - Scribe offers auto-send where drafts are already accepted as-is (Priority: P2)

Scribe watches, per chat, how often the user sends drafts without changes. When more than
60% of the user's decisions in a chat are "sent unchanged" over a meaningful sample, Scribe
suggests auto-send for that chat once, with the numbers behind the suggestion. The user can
accept, decline, or ask not to be offered again for that chat.

**Why this priority**: It makes the feature discoverable and ties it to evidence, but it
depends on Story 1 for the actual sending.

**Independent Test**: In a test chat, produce at least 20 decided drafts of which more than
60% are sent unchanged, and check that exactly one offer appears with the correct figures;
repeat with 60% or fewer and check that no offer appears.

**Acceptance Scenarios**:

1. **Given** a work chat with at least 20 decided drafts in the last 30 days and more than
   60% of them sent unchanged, **When** the next decision is recorded, **Then** Scribe sends
   one offer in the bot chat naming the chat and the figure (for example, "17 of 22 drafts
   sent unchanged in the last 30 days") with **Turn on**, **Not now** and **Don't offer for
   this chat**.
2. **Given** a chat at exactly 60% or below, or with fewer than 20 decided drafts, **When**
   decisions are recorded, **Then** no offer is shown for that chat.
3. **Given** the user tapped **Not now**, **When** the chat still qualifies, **Then** the offer
   is not repeated for that chat for 30 days.
4. **Given** the user tapped **Don't offer for this chat**, **When** the chat qualifies again
   later, **Then** no offer is shown for that chat, while manual enabling (Story 1) stays
   available.
5. **Given** the user taps **Turn on** in an offer, **When** they confirm, **Then** auto-send is
   enabled for that chat exactly as in Story 1.

---

### User Story 3 - Stay in control after enabling (Priority: P3)

The user can see where auto-send is on, turn it off everywhere in one action, and is warned
when the user's own corrections suggest a chat no longer deserves it.

**Why this priority**: It keeps the mode reversible and safe over time, but the first two
stories already deliver the value.

**Independent Test**: Enable auto-send in two chats, use the "turn off everywhere" action and
check both are off; separately, intervene in most auto-send cards in one chat and check that
Scribe suggests turning it off there.

**Acceptance Scenarios**:

1. **Given** auto-send is on in several chats, **When** the user opens settings, **Then** they
   see the list of chats with auto-send on and a single action to turn it off everywhere.
2. **Given** auto-send is on in a chat, **When** among the last 10 auto-send drafts in that
   chat the user cancelled, edited or rejected more than 4 (so fewer than 60% went out
   unchanged), **Then** Scribe suggests turning auto-send off for that chat, with the figures.
3. **Given** the user ran `/pause` or turned on out-of-office mode, **When** messages arrive,
   **Then** nothing is sent automatically while that mode is active.

---

### Edge Cases

- The card cannot be posted to the bot chat (Bot API error, flood wait, network failure):
  nothing is sent automatically. When the card is posted later (retry, issue #15), the
  countdown starts only then, with the full window counted from the moment the card
  appeared, and eligibility is checked again at that moment. The send never happens earlier
  than the time printed on the card.
- The user deletes the card message in the bot chat: Telegram does not report this, so it is
  not treated as cancelling; the draft is sent at the printed time. The help text says so.
- A draft carries any hard stop (suspected prompt injection, a claim of an action a reply
  cannot perform, a new commitment or a date conflict with a task, a sensitive-topic flag, or
  an unfilled `[[…]]` placeholder): it is never sent automatically; the card says why and
  needs the usual approval. Hard-stop drafts still count in the per-chat statistics.
- New messages arrive in the chat during the cancel window: the countdown stops, the draft
  is superseded or regenerated as today, and the new draft starts its own window only if it
  is eligible.
- The user replies in the chat themselves during the cancel window: the countdown stops and
  the draft is handled as it is today when the user answers on their own (it is not sent).
- The cancel window would end outside the user's working hours: the draft is not sent then;
  it waits as an ordinary pending draft and the user is told it needs a manual decision.
- Sending fails (connection lost, account disconnected): the draft stays pending and the
  user is told on the card; there is no automatic retry beyond what approved sends already do.
- The chat is removed from work chats, or work account mode is turned off for it: auto-send
  for that chat is turned off and running countdowns stop.
- The style profile for the chat has too little data (weak-style warning): drafts are not
  sent automatically while the warning applies.
- A group chat: auto-send can be enabled manually and offered like any other chat; drafts
  there still only appear on mention or reply, as today.
- The user deletes their data (`/delete_my_data`): auto-send settings and statistics are
  deleted with everything else.
- Statistics fall below the threshold after the offer was sent but before the user answered:
  the offer can still be accepted; the figures shown are the ones at the time of the offer.

## Requirements *(mandatory)*

### Functional Requirements

**Mode and scope**

- **FR-001**: Auto-send MUST be off by default for every chat and for every new user.
- **FR-002**: Auto-send MUST be enabled and disabled per work chat; there MUST NOT be a single
  switch that enables it for all chats at once.
- **FR-003**: Users MUST be able to enable auto-send for any work chat manually from settings
  at any time, regardless of that chat's statistics, after one explicit confirmation.
- **FR-004**: Users MUST be able to disable auto-send for a single chat, and for all chats in
  one action; disabling MUST take effect immediately, including for drafts whose cancel
  window is running.

**Eligibility and sending**

- **FR-005**: A draft MUST be sent automatically only if auto-send is on for its chat, the
  draft has no hard stop (suspected injection, action claim, new commitment, date conflict,
  sensitive-topic flag, unfilled placeholder), the chat's style profile is not under the
  weak-style warning, and neither pause nor out-of-office mode is active.
- **FR-006**: Every auto-send draft MUST still be shown to the user in the bot chat before
  it is sent, with a visible cancel window and the actions Send now, Edit, Don't send
  automatically and Reject. The countdown MUST start only after the card has been posted
  successfully, MUST last the full window from that moment, and the send MUST NOT happen
  earlier than the time printed on the card. A draft whose card has not been posted MUST
  never be sent automatically.
- **FR-007**: The cancel window MUST default to 2 minutes and MUST be adjustable by the user
  to one of 1, 2, 5, 10 or 15 minutes.
- **FR-008**: Any user action on the card during the cancel window (Edit, Don't send
  automatically, Reject, Regenerate) MUST stop the countdown; Send now MUST send immediately.
  A draft produced by Regenerate or by filling placeholders MUST NOT be sent automatically:
  the user is already deciding on it.
- **FR-009**: New messages in the chat or a manual reply by the user during the cancel window
  MUST stop the countdown and follow the existing handling for those events.
- **FR-010**: A draft MUST NOT be sent automatically outside the user's working hours; if the
  window would end outside them, the draft MUST become an ordinary pending draft.
- **FR-011**: An automatically sent reply MUST go out from the user's account exactly as an
  approved reply does, and MUST be recorded as "sent automatically" in `/history` and in the
  card, distinguishable from manually approved sends.
- **FR-012**: A draft that is not eligible for auto-send in an auto-send chat MUST show on its
  card why it needs manual approval.

**Statistics and offer**

- **FR-013**: Scribe MUST keep, per work chat, the count of the user's decisions on drafts in
  the last 30 days, split into "sent unchanged", "sent after edit", "rejected" and
  "regenerated"; expired and dropped drafts MUST NOT count as decisions.
- **FR-014**: The unchanged rate for a chat MUST be "sent unchanged" divided by all decisions
  in that chat over the last 30 days. Drafts sent automatically without intervention MUST NOT
  count towards it.
- **FR-015**: Scribe MUST offer auto-send for a chat only when the chat has at least 20
  decisions in the last 30 days and its unchanged rate is strictly above 60%, auto-send is
  off for that chat, and the user has not opted out of offers for that chat.
- **FR-016**: The offer MUST name the chat, show the figures behind it, and give the choices
  Turn on, Not now and Don't offer for this chat; Turn on MUST go through the same
  confirmation as manual enabling.
- **FR-017**: After Not now, Scribe MUST NOT repeat the offer for that chat for 30 days; after
  Don't offer for this chat, Scribe MUST NOT offer it for that chat again until the user
  resets offers in settings.
- **FR-018**: Scribe MUST send at most one auto-send offer per day across all chats.

**Ongoing control**

- **FR-019**: Settings MUST list the chats with auto-send on, with each chat's current
  unchanged rate.
- **FR-020**: When, among the last 10 auto-send drafts in a chat, the user intervened
  (cancelled, edited, rejected or regenerated) in more than 4, Scribe MUST suggest turning
  auto-send off for that chat, with the figures; the suggestion MUST NOT repeat for that chat
  within 7 days. It is evaluated only once the chat has at least 10 auto-send drafts. Sending
  early with Send now without changes is not an intervention.
- **FR-021**: Removing a chat from work chats (or excluding it from work account mode) MUST
  turn auto-send off for that chat.
- **FR-022**: Every change to auto-send settings (enabled, disabled, offer shown, offer
  answered) and every automatic send MUST be logged without message content.
- **FR-023**: Auto-send settings and statistics MUST be included in `/my_data` and removed by
  `/delete_my_data`.
- **FR-024**: Content of incoming messages MUST NOT be able to enable auto-send, extend or
  skip the cancel window, or make a hard-stop draft eligible.
- **FR-025**: All new texts (offer, confirmation, card states, settings) MUST be available in
  English and Russian.

### Key Entities

- **Chat auto-send setting**: per user and work chat; on/off state, when and how it was
  enabled (manually or from an offer), offer state (never offered, offered, snoozed until a
  date, opted out).
- **Draft decision record**: per draft; which chat, the user's decision (sent unchanged, sent
  after edit, rejected, regenerated, auto-sent, auto-send cancelled), and when; used for the
  30-day per-chat rate. Holds no message content.
- **Auto-send countdown**: per eligible draft; when the cancel window ends and whether it was
  stopped and by what.
- **User auto-send preferences**: per user; cancel window length.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 0 drafts with a hard stop, and 0 drafts whose card was not shown, are ever sent
  automatically (verified by automated tests covering every block reason, at both the arming
  and the sending check, and a failed card post).
- **SC-002**: 0 messages are sent automatically in chats where the user has not enabled
  auto-send, or after the user disabled it.
- **SC-003**: Offers appear only for chats that meet the threshold: 100% of offers shown
  correspond to a chat with at least 20 decisions and a rate above 60% at the time of the
  offer, and the figures in the offer match the user's history.
- **SC-004**: In chats with auto-send on, the median time from draft ready to reply sent
  drops to the cancel window length (2 minutes by default), compared with the current median
  time to manual approval.
- **SC-005**: A user can turn auto-send off for a chat, or everywhere, in at most 2 taps from
  the settings screen.
- **SC-006**: Among users who accept an offer, at least 80% still have auto-send on in that
  chat 30 days later.

## Assumptions

- "Messages sent without edits" is read per chat, not across the whole account, to satisfy
  the constitution's requirement that auto-send be scoped. Rejections and regenerations count
  against the rate, as they are a stronger sign of a bad draft than an edit.
- 20 decisions in 30 days is the minimum sample: smaller samples make 60% too noisy. The
  30-day window matches the existing message retention period.
- The cancel window is a safety net, not a "typing" imitation; the existing reading delay
  before drafting still applies unchanged.
- Auto-send reuses the existing send path and hard-stop checks; no new message channels.
- Retrying a card that failed to post is a separate change (issue #15). This feature only
  guarantees that the countdown starts whenever, and only when, a card post succeeds, so it
  works with that retry without further changes.
- The out-of-office replies of `/ooo` are never sent automatically in this release.
- Revoking auto-send automatically (rather than suggesting it) is out of scope for this
  release; Scribe only suggests turning it off (FR-020).
- The README statement "Nothing is sent without your approval" will need to be reworded for
  users who enable auto-send; the constitution already allows opt-in, scoped auto-send.
