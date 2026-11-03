# Feature Specification: Beta & Subscription Access Gate

**Feature Branch**: `005-beta-subscription-access`

**Created**: 2026-10-21

**Status**: Draft

## Clarifications

### Session 2026-10-21

- Q: How are subscriptions obtained? → A: The operator grants them manually for a chosen
  period after an outside payment. Payment inside the bot is a later feature.
- Q: Do access records (beta entry, subscriptions, access history) survive the user's own
  data deletion? → A: No. Deletion removes everything, after a warning that access will be
  lost (constitution, Principle II).

### Session 2026-10-22

- Q: The "no access" message linked to `t.me/scribewise_beta`, which is wrong. How do people
  ask for access? → A: Inside the bot. The message offers two buttons: join the beta, or
  subscribe. Subscribing is a placeholder for now, because prices are not set.
- Q: What does joining the beta involve? → A: The person describes how they plan to use
  Scribe, then submits. A note next to the submit button says that by pressing it they agree
  to the processing of their personal data.
- Q: What happens after an application is submitted? → A: The operator approves or rejects
  it. The operator gets each new application as a message in the bot, with Approve and
  Reject buttons.

**Input**: User description: "Бот должен отвечать только тем, кто входит в список бета-тестеров или у кого куплена подписка" (The bot must respond only to people on the beta-tester list or who have bought a subscription.)

**Builds on**: `specs/001-telegram-reply-agent` (control chat, guided setup, data deletion),
`specs/002-telegram-qr-login` (connecting a Telegram account), `specs/landing` (goal: 30 beta
testers; "Get into the beta" button; cloud is "early access", prices announced to beta testers
first).

## Problem

Today anyone who finds the bot and sends `/start` gets an account and can connect their
Telegram. On 2026-10-21 a stranger did exactly that on the hosted instance minutes after a
restart. This means that:

- **Cost**: every account uses LLM capacity paid for by the operator.
- **Trust & privacy**: the hosted service reads work messages. The operator must know who
  uses it, especially during the beta.
- **Business**: the cloud offering is meant for invited beta testers now and paying
  subscribers later. An open door makes both meaningless.

This feature adds an **access gate**. The bot serves a person only if they are on the beta
list or have an active subscription. Everyone else gets a short, polite answer saying how
to get access, and nothing else happens: no account setup, no Telegram connection, and no
LLM calls.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Strangers are turned away (Priority: P1)

A person who is neither a beta tester nor a subscriber finds the bot and writes to it. The
bot answers with one short message in their language: the service is in closed beta, and
here is how to request access or subscribe. The bot does not start setup, does not ask for a
phone number or show a QR code, and stores nothing about the person except what is needed
to avoid repeating the answer too often.

**Why this priority**: this closes the open door. It is the minimum that protects cost and
privacy, and it is useful even before payments exist.

**Independent Test**: from a Telegram account that is not on the list and has no
subscription, send `/start`, `/help`, any other command and free text. Each time the bot
gives the "no access" answer. No account is created, and no login step is offered.

**Acceptance Scenarios**:

1. **Given** a person with no access, **When** they send `/start`, **Then** the bot replies
   with the "no access" message, which includes a way to request beta access and the
   subscription option, and does not create an account.
2. **Given** a person with no access, **When** they send any other command or text, **Then**
   the bot gives the same "no access" answer and takes no other action.
3. **Given** a person with no access who keeps writing, **When** they send many messages in
   a short time, **Then** the bot does not answer every message. It answers again only after
   a cool-down, so it cannot be used to flood or to burn resources.

---

### User Story 2 - Beta testers get in (Priority: P1)

The operator adds a person to the beta list. That person writes to the bot and goes through
the normal setup: language, Telegram connection, and so on. Nothing in their experience
differs from today's service.

**Why this priority**: the landing goal is 30 beta testers. Without this story the gate
would lock everyone out.

**Independent Test**: the operator adds a test account to the beta list. That account
sends `/start` and completes setup. An account not on the list still gets the "no access"
answer.

**Acceptance Scenarios**:

1. **Given** a person on the beta list who has never used the bot, **When** they send
   `/start`, **Then** normal setup begins.
2. **Given** a person who was refused earlier, **When** the operator adds them to the beta
   list, **Then** their next message to the bot starts normal setup without any other step.
3. **Given** the operator, **When** they view the beta list, **Then** they see every entry,
   when it was added and whether that person has already started using the bot.

---

### User Story 3 - Losing access pauses the service (Priority: P2)

A beta tester is removed from the list, or a subscriber's subscription ends. The agent
stops working for them: it no longer reads their chats, prepares drafts or sends anything.
The bot tells them once why, and how to get access back. Their data is kept, so if access
returns, they continue where they left off. They can still delete all their data at any
time.

**Why this priority**: without it, the gate only stops new people while removed or lapsed
users keep using the service.

**Independent Test**: take an active test user and remove their access. Check that no new
drafts are created for them, that they receive a single notice, that they can still delete
their data, and that restoring access resumes the service without a new setup.

**Acceptance Scenarios**:

1. **Given** an active user, **When** their access ends, **Then** within 5 minutes the agent
   stops processing their messages, and the user receives one notice explaining why and how
   to restore access.
2. **Given** a user whose access ended, **When** they write to the bot, **Then** they get the
   "access ended" answer. The only commands that still work are viewing what is stored about
   them and deleting their data, as required by the constitution (Principle II).
3. **Given** a user whose access ended, **When** access is restored, **Then** the service
   resumes with their existing settings, connection and style profile, and they do not
   repeat setup. If the Telegram connection expired in the meantime, they are asked to
   reconnect.
4. **Given** drafts that were waiting for approval when access ended, **When** access ends,
   **Then** those drafts are not sent. They expire and are not sent even later.

---

### User Story 4 - Subscribers get in (Priority: P2)

A person buys a subscription. From that moment the bot serves them the same way it serves
beta testers, for as long as the subscription is active.

**Why this priority**: this is the path to revenue after the beta. Prices are not
announced yet, so it comes after the beta gate.

**Independent Test**: give a test account an active subscription. It completes setup and
uses the service. When the subscription end date passes, Story 3 applies.

**Acceptance Scenarios**:

1. **Given** a person with an active subscription, **When** they write to the bot, **Then**
   they get the normal service.
2. **Given** a subscription with an end date, **When** that date passes and the
   subscription has not been renewed, **Then** access ends as in Story 3.
3. **Given** a subscriber whose subscription ends in 3 days, **When** that time comes,
   **Then** the bot reminds them once that it is ending and how to renew.
4. **Given** a person with no access, **When** they choose to subscribe from the "no access"
   message, **Then** the message tells them how to arrange a subscription with the operator.
   In this feature there is no payment inside the bot (see FR-017).

---

### User Story 5 - Applying for the beta inside the bot (Priority: P1)

A person without access writes to the bot. The "no access" message offers two buttons:
**Join the beta** and **Subscription**.

- **Join the beta**: the bot asks how they plan to use Scribe. They reply in their own words,
  then press **Send application**. A note next to the button says that by pressing it they
  agree to the processing of their personal data. The bot confirms that the application was
  sent and that they will be told the decision.
- **The operator** gets the application as a bot message with **Approve** and **Reject**
  buttons. Approving adds the person to the beta list, and the person is told they can start
  with /start. Rejecting tells them politely that the beta is full for now.
- **Subscription**: the bot says subscriptions are coming soon and prices are not set yet,
  and suggests joining the beta meanwhile.

**Why this priority**: the gate is on, and today there is no working way to ask for access.
The link in the message is wrong. Without this story, the beta cannot reach its goal of 30
testers.

**Independent Test**:
1. From an account with no access, press **Join the beta**, type a use case and press
   **Send application**.
2. The operator chat receives the application with the text and the buttons.
3. **Approve**: the person gets the "you're in" message, and their `/start` begins setup.
4. A second person who is **Rejected** gets the polite rejection and still has no access.

**Acceptance Scenarios**:

1. **Given** a person without access, **When** they write to the bot, **Then** the "no
   access" message has the **Join the beta** and **Subscription** buttons and no external
   link.
2. **Given** a person who pressed **Join the beta**, **When** they send their use case,
   **Then** the bot shows it back with **Send application**, **Edit** and **Cancel**, and the
   personal-data note.
3. **Given** a submitted application, **When** the operator presses **Approve**, **Then** the
   person is added to the beta list and notified, and the operator's message shows who
   decided and when.
4. **Given** a pending application, **When** the person writes to the bot, **Then** they are
   told their application is under review, and no new application can be started.
5. **Given** a rejected application, **When** the person writes again within 30 days,
   **Then** they are told the beta is full for now and cannot apply again until 30 days
   after the rejection.
6. **Given** a person who pressed **Subscription**, **Then** they get the placeholder text,
   and nothing is stored.

---

### Edge Cases

- **Both beta and subscription**: the person has access while either is valid. Losing one
  does not end access if the other is still valid.
- **Existing users when the gate turns on**: accounts created before this feature (including
  the stranger from 2026-10-21) are checked like everyone else. Those without access get the
  Story 3 treatment, so their data is kept and can be deleted, but not used.
- **Username change**: if the beta list entry was made by @username and the person later
  changes their username, they keep access. The entry is tied to their Telegram account once
  they first use the bot.
- **Entry added for a username nobody has**: the entry waits. It is shown to the operator as
  "not used yet".
- **Access ends mid-draft**: a draft being prepared when access ends is discarded and never
  shown or sent.
- **Group chats and forwarded messages**: the bot answers only in private chats. If the bot
  is added to a group, it ignores it and does not post the "no access" message there.
- **Self-hosted installations**: a person running their own copy is the operator. The gate
  must be easy to turn off there, or must let the operator add themselves without an extra
  step, because self-hosted is promised to be free.
- **Operator locked out**: the operator always has access to their own instance.
- **Use case text**: only text is accepted. It must be 10–1000 characters; anything else is
  asked again. It is shown to the operator as plain text and never interpreted.
- **Application without consent**: an application exists only after **Send application** is
  pressed. A typed use case that was never submitted is dropped when the flow ends.
- **No operator configured**: applications are still stored, and the operator can review
  them from the operator tool.
- **Two operators**: both get the application. The first decision wins, and the other
  operator's buttons then show who decided.
- **Applicant already has access** (added with `beta add` meanwhile): approving changes
  nothing, and the application is closed as approved.

## Requirements *(mandatory)*

### Functional Requirements

**Access check**

- **FR-001**: The system MUST decide for every incoming message or button press in the
  control bot whether the sender has access. A sender has access only if they are on the
  beta list, have an active subscription, or are the operator.
- **FR-002**: A sender without access MUST NOT get an account, start setup, connect a
  Telegram account, trigger LLM calls or reach any feature other than those listed in
  FR-011.
- **FR-003**: The access check MUST happen before any other processing of the message, and
  it MUST apply to every command and free-text message, including those added later.
- **FR-004**: The agent's work on a user's behalf (reading chats, preparing drafts, sending
  approved replies, scheduled jobs) MUST run only while that user has access.

**Response to people without access**

- **FR-005**: A sender without access MUST get a short message in their Telegram interface
  language (Russian or English, English by default). It says the service is in closed beta
  and offers two buttons: **Join the beta** (US5) and **Subscription**. It contains no
  external link. If the sender has a pending application, or one rejected less than 30 days
  ago, the message says so instead and shows no buttons.
- **FR-006**: The bot MUST NOT answer the same sender without access more than once per
  cool-down period (default: once per hour). Messages in between are ignored silently.
- **FR-007**: The system MUST NOT store the content of messages from senders without
  access. It may keep only their Telegram account id, the time of the last answer and a
  count of attempts, for at most 30 days. The only exception is a beta application the
  person explicitly submits (FR-023).

**Beta list**

- **FR-008**: The operator MUST be able to add a person to the beta list by Telegram account
  id or @username, remove a person and view the list. Each entry shows when it was added,
  an optional note and whether it is already linked to an account.
- **FR-009**: When a person whose entry was made by @username first writes to the bot, the
  system MUST tie the entry to their Telegram account id, so a later username change keeps
  their access.
- **FR-010**: Adding or removing a beta entry MUST take effect for the person's next message
  without a restart. Removal MUST stop the agent's work for that person within 5 minutes.

**Losing and regaining access**

- **FR-011**: A user whose access has ended MUST still be able to view what is stored about
  them and delete all of it (constitution, Principle II). "All of it" includes their beta
  entry, subscriptions and access history. Before deletion, the bot warns that beta access
  or an active subscription will be lost and will have to be arranged with the operator
  again. The same applies to users who still have access. All other features are
  unavailable, and the bot replies with an "access ended" message that says how to restore
  access.
- **FR-012**: When access ends, the system MUST notify the user once. It MUST NOT send any
  pending or later-approved draft, and it MUST keep the user's data and settings, subject to
  normal retention rules.
- **FR-013**: When access is restored, the system MUST resume the service with the user's
  existing data and settings and without repeating setup.

**Subscriptions**

- **FR-014**: The system MUST record, for each subscription, whose it is, when it started,
  when it ends and how it was granted.
- **FR-015**: A subscription MUST give access from its start until its end. When it ends
  without renewal, FR-011 and FR-012 apply.
- **FR-016**: The system MUST remind a subscriber once, 3 days before the subscription ends,
  how to renew.
- **FR-017**: In this feature, subscriptions MUST be granted by the operator for a chosen
  period after the person has paid outside the bot. Payment inside the bot is out of scope and
  is left for a later feature, once prices are set.

**Beta applications (US5)**

- **FR-023**: A person without access MUST be able to apply for the beta inside the bot:
  - they press **Join the beta**;
  - they describe their use case in 10–1000 characters of text;
  - they review it and press **Send application**.

  Next to that button the bot MUST say that by pressing it the person agrees to the
  processing of their personal data. An application MUST store:
  - the Telegram id;
  - the @username, if any;
  - the use case;
  - the language;
  - the time of consent;
  - its status: pending, approved or rejected.
- **FR-024**: A person MUST have at most one pending application. They MUST NOT be able to
  apply again until 30 days after a rejection.
- **FR-025**: Each new application MUST be sent to every operator in their bot chat. The
  message shows the use case as plain text, the @username and Telegram id, and **Approve** /
  **Reject** buttons. Only an operator can press them. The first decision wins, and every
  operator copy is updated to show the decision.
- **FR-026**: **Approve** MUST add the person to the beta list (recorded as an operator
  action) and tell them they can start with /start. **Reject** MUST tell them politely that
  the beta is full for now. Both decisions MUST be recorded in the access log.
- **FR-027**: The operator tool MUST list pending applications and approve or reject them by
  id, as an alternative to the buttons.
- **FR-028**: **Subscription** MUST show a placeholder: subscriptions are coming, prices are
  not set yet, and joining the beta is possible meanwhile. If a subscription contact is
  configured, it is added.
- **FR-029**: Application texts MUST be deleted 30 days after the decision. Applications
  MUST be included in `/my_data` and deleted by `/delete_my_data`.

**Operator & audit**

- **FR-018**: The operator MUST be able to grant, extend and end a subscription for a person
  and see each person's access status: beta, subscription with end date, both or none.
- **FR-019**: Every access change (beta added or removed, subscription granted, extended or
  ended, access lost or restored) MUST be logged with time, actor and reason, without
  message content (constitution, Principles I and VI).
- **FR-020**: The operator MUST be able to see how many people were refused in a period, so
  they can measure demand for the beta.
- **FR-021**: The gate MUST be a deployment setting that is on for the hosted service and can
  be turned off for self-hosted installations. When it is off, the bot behaves as it does
  today.
- **FR-022**: The operator of an instance MUST always have access, without being added to the
  beta list.

### Key Entities

- **Beta list entry**: a person invited to the beta. It has a Telegram account id or
  @username, an optional note (for example where the invite came from), who added it and
  when, and the account it is linked to once used.
- **Subscription**: a paid right to use the service. It has the person it belongs to, start
  and end dates, who granted it, an optional note (for example the payment reference) and its state: active, ended or
  cancelled.
- **Access status** (derived, not stored): for a person at a moment in time, one of beta,
  subscribed, operator or none, with the date access ends if known.
- **Refused contact**: a minimal record of a sender without access (Telegram account id, last
  answer time, attempt count) used for the cool-down and the demand count. It holds no message
  content.
- **Access event**: an audit record of an access change: who, what, when and why.
- **Beta application**: a person's request to join the beta. It has the Telegram id, the
  @username, the use case text, the language, the time of consent, the status (pending,
  approved, rejected), and who decided and when.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In testing, 100% of messages from senders without access get no service beyond
  the "no access" answer: no account, no setup step and no LLM call.
- **SC-002**: A person added to the beta list can start setup with their very next message.
  It takes the operator under 1 minute to add someone.
- **SC-003**: After access is removed or a subscription ends, the agent creates no new drafts
  and sends no messages for that user. The service stops within 5 minutes.
- **SC-004**: A user whose access was restored is back in service without repeating setup in
  100% of test cases where their Telegram connection is still valid.
- **SC-005**: A sender without access gets at most one answer per hour, however many messages
  they send.
- **SC-006**: The operator can tell at any time how many beta testers are active against the
  goal of 30, and how many people were refused in the last 7 days.

## Assumptions

- "The bot" means the whole service for that person: the control bot and the agent working
  on their Telegram account. The gate covers both.
- The "operator" is whoever runs the instance (today, the project owner). Operator actions
  use the existing operator tooling, not commands in the public bot, so ordinary users never
  see admin controls.
- Beta access is requested inside the bot (US5, clarified 2026-10-22). The landing page's
  "Get into the beta" button should lead to the bot itself.
- The personal-data consent is the short note next to **Send application**. A full privacy
  policy page is out of scope; the note can link to one later.
- Payment inside the bot, prices, plans, trials, refunds and invoices are out of scope
  (clarified 2026-10-21: subscriptions are granted by the operator). The landing page says prices
  will be announced to beta testers first.
- Beta testers keep access until the operator removes them. When paid plans start, the
  operator decides whether to move them to a subscription. There is no automatic expiry of
  beta access in this feature.
- Data of users without access follows the existing retention rules. This feature adds no
  new deletion deadline.
- The gate must be in place on the hosted instance before more strangers find the bot. The
  beta list (Stories 1–2) can ship first, ahead of subscriptions (Story 4).
