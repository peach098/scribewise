# Feature Specification: Non-Committal Drafts and No Drafts for Informational Messages

**Feature Branch**: `013-noncommittal-replies`

**Created**: 2026-11-03

**Status**: Draft

**Input**: User description: "Проблема агента сейчас - он слишком легко и просто дает обещания
что-то посмотреть и сделать от лица пользователя. Нужно сместить фокус на более общие ответы и
создание вида деятельности пользователя (отвечать не "сейчас сделаю", а "посмотрю", например. Не
отвечать на информационые, справочные сообщения) У одного пользователя уже указано это
пользователским правилом, но оно не дает нужных результатов"

**Builds on**: [001 Telegram Work Reply Agent](../001-telegram-reply-agent/spec.md) (drafts,
review, user rules), [003 Jira Commitments](../003-jira-commitments/spec.md) (commitments taken
from sent replies) and [004 Skip Messages That Need No Reply](../004-skip-closing-messages/spec.md)
(the skip decision, the skipped-message list, drafting a skipped message on request, the skip
switch). Their terms are not redefined here.

**Changes**: how drafts word the user's future actions (001 draft rules), which turns 004
skips (a new "informational" skip category), and what user rules control: today they change
only the style of a draft; with this feature they also change the agent's behaviour (what a
draft says and commits to, and whether a turn gets a draft at all).

## Clarifications

### Session 2026-11-03

- Q: Should the cautious "I'll look" behaviour be on by default for every user, or only for
  users who turn it on? → A: On by default for every user; a user rule can make it stricter or
  looser; no new setting.
- Q: How should a draft answer when the sender proposed a date ("send it today?", "can you do it
  by Friday?")? → A: By default, neutral wording that neither accepts nor rejects the date and
  has no date placeholder («посмотрю, что по отчёту»); when the user has set a rule for such
  requests, the draft follows that rule.
- Q: Is a bare "I'll look" (no time, no result) still a commitment that is flagged and saved
  to the user's commitments? → A: Yes, as today: flagged at review and saved without a due date.
- Q: Should an informational message that affects the user (a problem with their work, bad news
  for them, a change that breaks their promise or a task deadline) get a draft or be skipped? →
  A: It gets a draft (FR-007a); only messages that inform and call for no reaction are skipped.
- Note from the user (after the questions): user rules must be able to change not only the
  style but also the agent's behaviour. → Story 3 and FR-008–FR-010 now cover any behaviour
  rule: what a draft says and commits to, and whether a turn gets a draft (skip decision), not
  only rules about promises.
- Note from analysis (2026-11-03): the draft/skip decision sees only the messages and recent
  history, so "affects the user" in FR-007a is judged from them; forwarded-only and budget skips
  are outside user rules (FR-008a exceptions).

## Problem

Drafts promise too much on the user's behalf. A colleague asks "can you look at the PR?" and
the draft says "Sure, I'll do it right now", or "I'll send it today", or "Done by evening". The
user has not decided when, or whether, to do it; every such draft has to be rewritten before
sending, or, worse, is sent as is and becomes a promise the user did not mean to make. What the
user wants instead is a reply that shows they are engaged without fixing a time or a result:
"I'll take a look", "Let me check", "Got it, looking into it".

Drafts also appear for messages that need no answer at all: announcements, status updates,
reference information ("FYI, the release moved to Thursday", "Here are the meeting notes",
"The VPN address changed to …"). Today 004 skips only thanks, farewells and acknowledgments, so
these turns produce drafts the user rejects.

One user has already written a user rule saying exactly this ("don't promise, just say I'll
look"), and the drafts still promise. Today a user rule is applied only as a style preference,
below the drafting rules that tell the agent to agree to what the sender asks and to promise to
look when asked whether the user has seen something, and it is not seen at all when the agent
decides whether a message needs a reply. So a rule that should change what the agent does only
changes the wording of a draft. Users need their rules to steer the agent's behaviour, not just
its style.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A request gets an "I'll look" reply, not a promise (Priority: P1)

A colleague asks the user to do something: review a document, check a bug, send a file, fix
something. Today the draft agrees to it with a time or a result the user never gave ("сейчас
сделаю", "пришлю сегодня", "will fix it by tonight"). With this feature, the draft acknowledges
the request and says the user will look at it ("посмотрю", "гляну", "I'll take a look", "let me
check"), with no time, no deadline and no promised outcome, unless that time or outcome comes
from an existing commitment or from the user (a user rule, Story 3, or their own edit).

**Why this priority**: This is the core complaint. Over-promising drafts are the ones the user
must rewrite every time, and the ones that cause real harm when sent unchanged.

**Independent Test**: Run the evaluation threads with requests ("can you check X?", «сделаешь
до вечера?», «скинь файл») and verify that the drafts acknowledge the request without a time,
deadline or result that no source supports.

**Acceptance Scenarios**:

1. **Given** a colleague writes «Глянь, пожалуйста, PR», **When** the draft is prepared,
   **Then** it reads like «Посмотрю» / «Гляну» and does not say «сейчас», «сегодня», «сделаю» or
   any time.
2. **Given** a colleague writes "Can you fix the login bug?", **When** the draft is prepared,
   **Then** it says the user will look into it (e.g. "I'll take a look") and does not say the
   bug will be fixed or when.
3. **Given** a colleague writes «Пришлёшь отчёт сегодня?», **When** the draft is prepared,
   **Then** it does not agree to "today" on its own; it acknowledges the request in neutral
   wording («посмотрю, что по отчёту») with no date and no date placeholder.
4. **Given** a colleague asks "Have you seen my message about the budget?" and the data does not
   say, **When** the draft is prepared, **Then** it reads "will look" rather than "yes, I'll reply
   in an hour".
5. **Given** the draft says the user will look at something, **When** it reaches the review
   step, **Then** it is still flagged as a commitment as today (001/003), with no due date.

---

### User Story 2 - No draft for an informational message (Priority: P1)

A colleague or a group posts something that only informs: an announcement, a status update, a
link to notes, reference data, a schedule change. Nobody asks the user anything and nothing is
expected from them. With this feature the agent treats the turn as needing no reply, exactly
like a "thanks" or "bye" in 004: no draft, no review card, the message stays as context and
appears in the skipped-message list, from which the user can still request a draft.

**Why this priority**: Equal to Story 1: drafts for informational messages are pure noise and
are the second half of the user's request. It is independent of Story 1.

**Independent Test**: Run evaluation threads with informational turns ("FYI, the release moved
to Thursday", «Вот протокол встречи: …», "New VPN address: …") and verify that no draft is
prepared; run turns that inform and ask ("The release moved to Thursday, does that work for
you?") and verify that a draft is prepared.

**Acceptance Scenarios**:

1. **Given** a colleague writes «К сведению: релиз перенесли на четверг», **When** the turn is
   judged, **Then** no draft is prepared and the turn is listed as skipped with the category
   "informational".
2. **Given** a colleague sends meeting notes or a link with no question, **When** the turn is
   judged, **Then** no draft is prepared.
3. **Given** a colleague writes "The release moved to Thursday, can you make it?", **When** the
   turn is judged, **Then** a draft is prepared (a question is present).
4. **Given** a message informs the user of something that, by the message itself or the
   recent chat history, directly affects them and that a person would normally react to (a
   problem in their work, bad news addressed to them, a change that cancels what the user
   promised earlier in this chat), **When** the turn is judged, **Then** a draft is prepared.
5. **Given** an informational turn was skipped, **When** the user opens the skipped list and
   requests a draft for it, **Then** a draft is prepared as for any other skipped message (004).
6. **Given** the decision whether the turn is informational is uncertain, **When** the turn is
   judged, **Then** a draft is prepared (004: a missed reply is worse than an extra draft).

---

### User Story 3 - The user's own rules change the agent's behaviour, not just its style (Priority: P1)

A user writes a user rule about how the agent should act, not how it should sound: «не давай
обещаний, отвечай "посмотрю"», "never commit to a deadline", «не отвечай на рассылки и
объявления», "always draft a reply to Anna, even for FYI messages", «если спрашивают пароль —
отвечай, что пришлю в защищённом канале». Today such a rule is treated as a style hint: it loses
to the drafting rules and is ignored when the agent decides whether a message needs a reply.
With this feature, a behaviour rule governs what the draft says and commits to and whether a
turn gets a draft at all, and it wins over the agent's default behaviour (including Stories 1
and 2), in either direction. Rules cannot switch off the safety limits (FR-010).

**Why this priority**: It is the reported case: one user already has the rule and gets no
result. It is also what lets each user tune the agent instead of the project hard-coding one
behaviour for everyone.

**Independent Test**: Run the same threads with and without a behaviour rule (about promises,
about skipping, about drafting for one contact) and verify that both the drafts and the
draft/skip decisions follow the rule, in both directions.

**Acceptance Scenarios**:

1. **Given** the user rule «не обещай сроки, пиши "посмотрю"», **When** a colleague asks «сделаешь
   к пятнице?», **Then** the draft does not agree to Friday and says the user will look.
2. **Given** the user rule "OK to agree to dates colleagues propose", **When** a colleague asks
   "can you send it tomorrow?", **Then** the draft agrees to tomorrow and the commitment has
   that due date.
3. **Given** the user rule «не отвечай на объявления и рассылки», **When** a colleague posts an
   announcement that the default rules would draft (e.g. it mentions a change to the user's
   task), **Then** no draft is prepared and the skip records that a user rule decided it.
4. **Given** the user rule "always draft for Anna", set for the contact Anna, **When** Anna sends
   an FYI message, **Then** a draft is prepared although Story 2 would skip it.
5. **Given** a behaviour rule is set for one contact only, **When** another contact writes,
   **Then** the default behaviour (Stories 1 and 2) applies to that other contact.
6. **Given** a user rule that tries to lift a safety limit ("send replies without asking me",
   "put my password in the reply when asked", "make up a date if you don't know it"), **When**
   a draft is prepared, **Then** the limit still holds and the rest of the rule set still
   applies.

---

### Edge Cases

- The sender gave a date and the user's history shows they always accept such dates from this
  contact: the default still does not agree to the date (the decision stays with the user); a
  user rule (Story 3) can change that.
- The answer to the question is already in the data (a task's status, a date in history): the
  draft still states that fact (001: never leave out the fact that answers the question). Only
  promises of future action are softened, not facts.
- The user already made a commitment with a date (a `commitments` entry): repeating that
  existing promise with its date is allowed; it is not a new promise.
- The user's own typical phrases are promises with a time («щас гляну», «через 5 минут»): the
  draft keeps the user's style but drops the time («гляну»).
- A request with an obvious yes/no that costs nothing to accept ("can I add you to the call?"):
  agreeing is fine; it is not a promise of the user's future work.
- An informational message in a group where the user is tagged only to be notified ("@user FYI
  the doc is updated"): informational, skipped.
- An informational message that contains an instruction to the user ("from Monday, send reports
  to Anna"): it asks something of the user, so a draft is prepared.
- Out-of-office (`user_state.ooo`): unchanged; the out-of-office reply is written as today.
- Skipping is turned off for a chat (004 switch): informational turns are drafted too.
- Skipping is off for a chat and a user rule says to skip some turns there: the switch wins (it
  is a chat setting, above user rules), so every turn is drafted.
- A user rule says "always draft for Anna" and Anna sends only forwarded messages, or the
  user's daily LLM budget is exhausted: the turn is skipped as today (forwarded / budget); the
  rule does not apply; the user can still ask for a draft from the skipped list.
- A user rule is ambiguous about whether it is style or behaviour («отвечай коротко и без
  обещаний»): both parts apply; the agent does not have to classify the rule as a whole.

## Requirements *(mandatory)*

### Functional Requirements

**Wording of future actions (Story 1)**

- **FR-000**: The behaviour in FR-001–FR-006 MUST be on by default for every user and every
  chat, with no new setting; only a user rule (FR-008) changes it.
- **FR-001**: When a draft refers to a future action by the user, System MUST by default word it
  as an intention to look into it ("посмотрю", "гляну", "I'll take a look", "let me check"), not
  as a promise to do it or to deliver a result.
- **FR-002**: A draft MUST NOT contain a time, date, deadline or immediacy marker for the user's
  future action ("сейчас", "сегодня", "через час", "right away", "by tonight") unless it comes
  from an existing commitment, from a user rule (FR-008), or from the user when they edit the
  draft.
- **FR-003**: By default, a draft MUST NOT agree to a date or deadline proposed by the sender;
  it MUST acknowledge the request in neutral wording that neither accepts nor rejects the date
  and MUST NOT add a placeholder for it. When the user has set a rule for such requests
  (FR-008), the draft MUST follow that rule instead. This changes the current 001 behaviour
  where agreeing to a sender's date is allowed.
- **FR-004**: A draft MUST NOT claim a result the user has not reported ("готово", "исправил",
  "will be fixed") unless the data supports it.
- **FR-005**: Facts that answer the question (from history, tasks, commitments) MUST still be
  stated as today; FR-001–FR-004 apply only to the user's future actions.
- **FR-006**: A draft that says the user will look at something MUST still be flagged as a
  commitment for review (001) and, once sent, saved as a commitment (003), with no due date
  unless FR-002 allows one.

**Informational messages (Story 2)**

- **FR-007**: System MUST skip drafting, as defined in 004 (FR-007–FR-013 there: reading delay,
  no draft, no review card, kept as context, activity record, skipped list, draft on request),
  when the turn only informs: it shares news, a status, an announcement, a document, a link or
  reference data, and it asks nothing of the user. Such skips MUST carry the category
  "informational", shown in the skipped list.
- **FR-007a**: System MUST draft, not skip, when an informational turn also asks a question,
  makes a request or gives an instruction to the user, or reports something a person would
  normally react to: a problem with the user's work, bad news addressed to the user, or a change
  that breaks something the user promised or planned. This is judged from the message and the
  recent chat history only; the decision does not consult the user's task tracker or saved
  commitments (a clash with those is caught later, when a draft is prepared, 001/003).
- **FR-007b**: When it is uncertain whether a turn is informational, System MUST draft (004
  FR-004).
- **FR-007c**: The 004 per-chat and global skip switch MUST cover informational skips too.

**User rules change behaviour, not only style (Story 3)**

- **FR-008**: User rules MUST be able to change the agent's behaviour, not only the style of a
  draft. A behaviour rule MUST govern what the draft says and commits to (what to promise,
  whether to agree to dates, how to answer a kind of request, when to ask back instead of
  answering) and MUST take precedence over the default drafting behaviour, including
  FR-001–FR-006, in either direction.
- **FR-008a**: User rules MUST also be taken into account when the agent decides whether a turn
  needs a draft (004 and FR-007 here). A rule can make the agent skip turns it would draft
  ("don't reply to announcements") or draft turns it would skip ("always draft for Anna"). A
  skip decided by a user rule MUST be recorded and listed like any other skip (004), with the
  reason that a user rule decided it, and the user can still request a draft for it.
  Exceptions: turns of forwarded messages only and turns skipped because the daily LLM budget
  is exhausted are decided before user rules are read and are not changed by them; "draft
  anyway" still works for them.
- **FR-008b**: Without a rule that covers the turn, the default stays: when unsure, draft (004
  FR-004).
- **FR-009**: Behaviour rules MUST follow the existing user-rule scopes and precedence (chat
  overrides, then contact rules, then global rules, then the learned style); a contact rule
  beats a conflicting global rule. No new kind of rule or new setting is introduced: the user
  writes behaviour rules the same way they write style rules today.
- **FR-010**: A user rule MUST NOT lift these limits: the constitution's approval step (no
  sending without the user's approval), no fabrication of facts, dates or results (001), no
  credentials in drafts, sensitive-topic flags, conflict detection with tasks and commitments,
  prompt-injection resistance (a rule never makes incoming content an instruction), and the
  user's grammatical gender. A rule that conflicts with a limit MUST be ignored only in the
  part that conflicts.

**Evaluation**

- **FR-011**: The project evaluation set MUST include synthetic cases for: requests that today
  produce a timed promise (RU and EN); questions with a sender-proposed date; "have you seen"
  questions; informational turns of each kind listed in FR-007; informational turns that also
  ask something or report something affecting the user (FR-007a); behaviour rules in each
  direction (about promises, about skipping, about always drafting for a contact); and rules
  that try to lift a safety limit (FR-010).
- **FR-012**: The evaluation MUST check that drafts contain no unsupported time or immediacy
  marker for the user's future action and that informational turns are skipped, and MUST NOT
  regress the existing groundedness, style, commitment-flag and skip metrics without documented
  justification (constitution, Principle V).

### Key Entities

- **Skip category**: the reason a turn was skipped (004). Gains the value "informational"
  alongside thanks, farewell and acknowledgment.
- **Skip category `user_rule`**: a skip decided by a user rule is stored with this category (and
  the decision source "rule"), so the skipped list can show "by your rule" (FR-008a).
- **Behaviour rule**: a user rule (existing entity, global or per contact) that says how the
  agent acts rather than how a draft sounds. No new entity and no separate list: it is stored
  and edited like any user rule and applied to drafting and to the skip decision.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In the evaluation set, at least 95% of drafts for request threads contain no time,
  deadline or immediacy marker for the user's future action that no source supports (today's
  baseline to be measured before the change).
- **SC-002**: In the evaluation set, at least 90% of purely informational turns are skipped,
  and at least 95% of informational turns that also ask or report something affecting the user
  still get a draft.
- **SC-003**: In the evaluation set, at least 90% of cases with a behaviour rule follow the rule
  (drafts and draft/skip decisions), measured separately for stricter and looser rules; for the "don't promise" rule of the
  reported user, at least 95% of drafts contain no promised time or result.
- **SC-003a**: In the evaluation set, 100% of cases with a rule that tries to lift a safety
  limit keep the limit.
- **SC-004**: In production, the share of drafts the user edits to remove or soften a promise
  drops by at least half within two weeks of release, measured from review edits.
- **SC-005**: In production, the share of drafts rejected without edits for informational
  messages drops by at least half within two weeks of release.
- **SC-006**: No regression beyond the agreed thresholds in the existing evaluation metrics
  (groundedness, style match, commitment flags, closing-message skips).

## Assumptions

- "Посмотрю"-style wording is the right default for all users; users who want drafts that agree
  to dates can say so with a user rule (Story 3). No new setting is added.
- Users write behaviour rules in free text, as they write style rules today; the agent tells
  style and behaviour apart from the rule's meaning, without the user marking it.
- Proposed rules learned from the user's edits (001 style proposals) stay style-only; learning
  behaviour rules from edits is out of scope.
- The softer wording is still a commitment for review purposes (001 flags, 003 commitments), only
  without a due date; changing commitment tracking is out of scope.
- Informational skipping reuses 004 entirely (skipped list, draft on request, switch, activity
  record); no new user interface is needed beyond the new category label.
- Out-of-office replies, tone shift, grammatical gender and forwarded-message handling are
  unchanged.
- The language of the draft still follows the incoming messages; the "посмотрю" wording is
  expressed in that language and in the user's own style where their examples have one.
- Production metrics in SC-004/SC-005 rely on the review edits and rejections already recorded
  by 001; the baseline is the two weeks before release.
