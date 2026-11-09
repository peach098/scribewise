# Feature Specification: Pay for a Subscription with Telegram Stars

**Feature Branch**: `015-telegram-stars-payments`

**Created**: 2026-11-06

**Status**: Draft

## Clarifications

### Session 2026-11-07

- Q: Who can buy a subscription? → A: Anyone who writes to the bot in a private chat. No
  operator approval is needed, even after a rejected beta application.
- Q: Is a payment a one-time purchase of a period or a recurring subscription? → A: A
  recurring Stars subscription. Telegram charges the person again every 30 days until they
  cancel.
- Q: What happens to payment records when the person deletes their data? → A: A minimal
  accounting record is kept for 3 years after deletion: Telegram id, amount, date, payment
  reference and refund status. Nothing else is kept.

**Input**: User description: "добавить возможность оплаты телеграм звёздами" (add the option to pay with Telegram Stars)

**Builds on**: `specs/005-beta-subscription-access` (access gate, subscriptions, the
**Subscription** button that today shows a placeholder, renewal reminder, access log, data
deletion). Its FR-017 left payment inside the bot to a later feature. This is that feature.

## Context

Today a subscription exists only if the operator grants it by hand after the person paid
outside the bot. The **Subscription** button on the "no access" message only says that
subscriptions are coming soon. Nobody can pay, so the cloud service cannot earn anything, and
every renewal is manual work for the operator.

Telegram Stars is Telegram's own in-app currency. People buy it inside Telegram with the card
or store account they already have, and bots that sell digital goods and services are
expected to take payment in it. Paying in Stars takes a few taps without leaving the bot, so
the person does not need to share card details with the operator, and the operator does not
need a separate payment provider.

This feature lets anyone buy a Scribe subscription inside the bot with Stars. The
subscription renews itself every 30 days until the person cancels it. A successful payment
grants access at once, with no operator step. Cancellation, refunds and the operator's view
of payments are covered too.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Subscribe with Stars (Priority: P1)

A person without access writes to the bot and presses **Subscription** on the "no access"
message. Instead of the placeholder, the bot shows the plan: what it includes, that it costs
a given number of Stars every 30 days and renews until cancelled, with a **Subscribe**
button. The person pays in Telegram's own payment window. Right after the payment, the bot
confirms that the subscription is active, says when the next charge is and how to cancel, and
offers to start setup with /start. From then on the person is served as a subscriber
(specs/005 US4).

**Why this priority**: this is the feature. Without it no one can pay, and the rest has
nothing to build on.

**Independent Test**: on a test instance with a price set, from an account without access,
press **Subscription**, pay with test Stars and check that the bot confirms the subscription
with the next charge date, that `/start` begins setup, and that the operator sees the
payment.

**Acceptance Scenarios**:

1. **Given** payments are turned on and a price is set, **When** a person without access
   presses **Subscription**, **Then** the bot shows the price in Stars per 30 days, says it
   renews until cancelled, gives a short description of what is included and shows a
   **Subscribe** button.
2. **Given** a person who pressed **Subscribe**, **When** the payment succeeds, **Then**
   within 10 seconds the bot confirms the subscription with the next charge date and how to
   cancel, the person has access, and the payment and the subscription are recorded.
3. **Given** a person who opened the payment window, **When** they close it or the payment
   fails, **Then** no subscription is created and nothing is charged, and they can try again.
4. **Given** payments are turned off or no price is set, **When** a person presses
   **Subscription**, **Then** they get today's placeholder (specs/005 FR-028), and no payment
   can be started.
5. **Given** a person with a pending beta application or one rejected less than 30 days ago,
   **When** they write to the bot, **Then** the message about their application also offers
   **Subscription**, and they can pay without waiting for the operator.

---

### User Story 2 - Automatic renewal and cancellation (Priority: P1)

Every 30 days Telegram charges the subscriber again, and the access is extended for another
30 days without any action from the person or the operator. Three days before each charge
the bot reminds the subscriber of the date and amount and of how to cancel. The subscriber
can cancel at any time in `/account`. They keep access until the end of the period they
already paid for, and nothing more is charged. Until that date they can turn renewal back
on. If a charge fails (for example, not enough Stars), access ends with the paid period, and
the bot says how to subscribe again.

**Why this priority**: renewal without manual work is what makes the subscription a
subscription. Cancelling must be as easy as subscribing, or people will not trust it.

**Independent Test**: with a short test period, subscribe a test account and check that the
next charge extends access and that the reminder came before it. Cancel in `/account` and
check that access lasts until the end of the paid period and that no further charge happens.
Then make a charge fail on a second account and check that access ends with the paid period
and that the person is told.

**Acceptance Scenarios**:

1. **Given** an active Stars subscription, **When** Telegram makes the next charge, **Then**
   the access is extended by 30 days with no gap, and the payment is recorded.
2. **Given** an active Stars subscription that will renew, **When** 3 days remain before the
   charge, **Then** the bot sends one reminder with the date, the amount and a **Cancel
   subscription** button.
3. **Given** an active subscriber, **When** they open `/account`, **Then** they see the
   subscription state, the date of the next charge (or the end date, if cancelled) and a
   button to cancel or to turn renewal back on.
4. **Given** a subscriber who cancelled, **When** the paid period ends, **Then** access ends
   as in specs/005 Story 3, and no further charge is made.
5. **Given** a subscriber who cancelled, **When** they turn renewal back on before the paid
   period ends, **Then** the next charge happens on the original date.
6. **Given** a charge that fails, **When** the paid period ends, **Then** access ends as in
   specs/005 Story 3, and the bot tells the person why and offers **Subscription** again.
7. **Given** a person whose subscription has ended, **When** they subscribe again, **Then**
   the service resumes with their existing data and settings (specs/005 FR-013).
8. **Given** a subscriber who cancels renewal in Telegram's own settings rather than in the
   bot, **When** the bot learns of it, **Then** `/account` shows the subscription as
   cancelled with its end date.

---

### User Story 3 - Payment help and refunds (Priority: P2)

A subscriber has a problem with a payment: they were charged twice, or they paid and the
service did not start. They send `/paysupport` (Telegram expects every bot that takes
payments to answer it). The bot explains how payments work and how to reach the operator,
and the operator receives their request with the payment details. The operator can refund a
payment from the operator tool. A refund returns the Stars, removes the period that payment
bought and stops further charges. The person is told about the refund.

**Why this priority**: Telegram requires a support path for payments, and sometimes a
refund is the only fair answer. It is needed before going live, but not for the first test
payment.

**Independent Test**: make a test payment, send `/paysupport` and check that the operator
gets the request with the payment reference. Refund that payment from the operator tool and
check that the Stars come back, the access granted by that payment ends, renewal stops and
the person is told.

**Acceptance Scenarios**:

1. **Given** anyone, **When** they send `/paysupport`, **Then** the bot answers with how to
   get help with a payment and asks them to describe the problem. Their description is sent
   to the operator with their Telegram id and their recent payment references.
2. **Given** a recorded payment, **When** the operator refunds it, **Then** the Stars are
   returned, the period it paid for is removed, the Stars subscription is cancelled so
   nothing more is charged, and the person gets a message saying so.
3. **Given** a refund that removes the person's only access, **When** it is done, **Then**
   access ends as in specs/005 Story 3: the agent stops, their data is kept, and they can
   subscribe again or delete their data.
4. **Given** anyone, **When** they send `/terms`, **Then** the bot shows the terms of the
   paid service: what is included, the price per 30 days, automatic renewal and how to
   cancel, the refund rules, what is kept after data deletion and the support contact.

---

### User Story 4 - The operator sets the price and sees the money (Priority: P2)

The operator sets the price in Stars per 30 days and can turn payments on or off without a
code change. They see a list of payments with who paid, how much, when, for which period and
whether it was refunded, plus totals for a period and the number of active and cancelled
Stars subscriptions. Every paid subscription shows up in the access status view next to
subscriptions they granted by hand.

**Why this priority**: the operator has to set a price before anyone can pay, and has to see
income to judge whether the cloud service pays for its LLM costs. A first version can live
with a price set once at deployment.

**Independent Test**: set a price, make two test payments and one refund, and check that the
operator's payment list and totals match, and that a price change applies to new
subscriptions but not to running ones.

**Acceptance Scenarios**:

1. **Given** the operator, **When** they change the price, **Then** new subscriptions use the
   new price, and running subscriptions keep the price they started with.
2. **Given** some payments, **When** the operator views payments for a period, **Then** they
   see each payment and the totals: number of payments, Stars received, Stars refunded, and
   how many Stars subscriptions are active, cancelled but still running, and ended.
3. **Given** a paid subscription, **When** the operator views the person's access status,
   **Then** it shows the subscription as paid in Stars with its next charge or end date,
   alongside any subscription granted by hand.

---

### Edge Cases

- **Already has access**: a person with an active Stars subscription is not offered another
  one; they see how to manage the one they have. A beta tester or a person with a
  subscription granted by hand may subscribe too. Access then lasts while any source is valid
  (specs/005), and the bot says before payment that their current access already runs until
  a given date (or, for beta, indefinitely).
- **Duplicate payment confirmation**: if Telegram reports the same payment more than once, it
  is recorded and credited exactly once.
- **Payment arrives while the service is restarting**: a confirmed payment, first or
  renewal, must never be lost. It is credited as soon as the service is back, and the person
  is told then.
- **Late renewal confirmation**: if a renewal charge is confirmed shortly after the paid
  period ended, access is restored at once for the new period. A short gap of up to 1 hour is
  not treated as losing access: the person gets no "access ended" notice for it.
- **Price changes while the payment window is open**: the person pays the price shown in the
  window they opened. A window older than 1 hour is declined before charging, and the person
  is asked to start again.
- **Payment check fails before charging** (payments turned off meanwhile, person deleted their
  data, offer outdated): the payment is declined so nothing is charged, and the person gets a
  short reason.
- **Payments turned off while subscriptions run**: running Stars subscriptions keep renewing
  and keep giving access. Turning payments off only stops new subscriptions.
- **Deleting data with an active Stars subscription**: `/delete_my_data` warns that the
  subscription will be cancelled, that the remaining paid days are lost and not refunded
  automatically, and that a minimal payment record is kept for 3 years. Deleting cancels the
  Stars subscription, so nothing more is charged.
- **Refund of a payment already partly used**: the whole period that payment bought is
  removed. If that period has already started, access ends at once unless another source of
  access remains.
- **Self-hosted installations**: payments are off by default. Self-hosted stays free, and
  turning payments on is the operator's choice.
- **Group chats**: payment buttons and commands work only in the private chat with the bot.
- **Not enough Stars**: for the first payment, Telegram itself offers to buy Stars inside the
  payment window. For a renewal, the charge fails and User Story 2 scenario 6 applies.

## Requirements *(mandatory)*

### Functional Requirements

**Offer and payment**

- **FR-001**: When payments are on and a price is set, the **Subscription** button (specs/005
  FR-028), the "access ended" message and the application-status message (specs/005 FR-005)
  MUST offer a Stars subscription. Otherwise the placeholder of specs/005 FR-028 stays.
- **FR-002**: The offer MUST show the price in Stars per 30 days, state that it renews
  automatically until cancelled, and give a short description of what is included, in the
  person's interface language (Russian or English).
- **FR-003**: Before charging, the system MUST check that the payment can still be accepted:
  payments are on, the price matches the offer, and the offer is no older than 1 hour. If any
  check fails, the payment MUST be declined so that nothing is charged, and the person told
  why in one sentence.
- **FR-004**: Every confirmed payment, first or renewal, MUST be recorded and credited exactly
  once, even if the confirmation is delivered more than once or arrives during a restart.
- **FR-005**: The first payment MUST start a subscription period of 30 days from the moment of
  payment. Each renewal payment MUST extend it by 30 days from the end of the current period.
  Access MUST be granted for the person's next message without a restart.
- **FR-006**: After the first payment, the bot MUST confirm to the person within 10 seconds
  that the subscription is active, when the next charge is and how to cancel. If they have
  never set up the service, it MUST offer /start. If their access had ended, the service MUST
  resume as in specs/005 FR-013. Successful renewals MUST NOT send a message.
- **FR-007**: Anyone writing to the bot in a private chat MUST be able to subscribe, whatever
  the state of their beta application. A person MUST have at most one active Stars
  subscription.

**Renewal and cancellation**

- **FR-008**: For a Stars subscription that will renew, the 3-day reminder (specs/005 FR-016)
  MUST state the charge date and amount and offer **Cancel subscription**. For a cancelled
  one it MUST state the end date and offer to turn renewal back on.
- **FR-009**: The subscriber MUST be able to cancel renewal and turn it back on from
  `/account` at any time before the paid period ends. Cancelling MUST stop further charges
  and keep access until the end of the paid period.
- **FR-010**: When a Stars subscription ends without renewal (cancelled, charge failed or
  refunded), specs/005 FR-011 and FR-012 MUST apply, and the bot MUST say why it ended and
  offer **Subscription** again.
- **FR-011**: The system MUST reflect a cancellation made outside the bot, in Telegram's own
  settings, no later than the end of the paid period.

**Support, terms and refunds**

- **FR-012**: The bot MUST answer `/paysupport` and `/terms` in the private chat, for
  everyone, with or without access. `/paysupport` MUST forward the person's description of
  the problem to every operator with their Telegram id and the references of their payments
  in the last 90 days. The description is plain text, never interpreted, and limited to
  1000 characters.
- **FR-013**: The operator MUST be able to refund a payment from the operator tool. A refund
  MUST return the Stars, remove the period that payment bought, cancel the Stars subscription
  so nothing more is charged, be recorded in the access log and be reported to the person.
- **FR-014**: If a refund leaves the person without access, specs/005 FR-011 and FR-012
  apply: the agent stops within 5 minutes and pending drafts are not sent.

**Operator**

- **FR-015**: The operator MUST be able to set the price in Stars per 30 days and turn new
  subscriptions on or off without a code change. A price change MUST apply to new
  subscriptions only. Turning payments off MUST NOT stop running subscriptions.
- **FR-016**: The operator MUST be able to list payments for a period with payer, amount,
  date, period bought and refund status, and see totals: count, Stars received, Stars
  refunded, and the number of Stars subscriptions that are active, cancelled but running,
  and ended.
- **FR-017**: The operator's access status view (specs/005 FR-018) MUST show whether a
  subscription was paid in Stars or granted by hand, and for Stars, the next charge date or
  the end date.
- **FR-018**: Every payment, renewal, failed charge, failed pre-charge check, cancellation and
  refund MUST be logged with time, person, amount and outcome, without message content and
  without the person's payment details beyond what Telegram reports (constitution,
  Principles II and VI).

**Data and deployment**

- **FR-019**: `/my_data` MUST list the person's payments (date, amount, period, refund status)
  and the state of their Stars subscription.
- **FR-020**: `/delete_my_data` MUST cancel an active Stars subscription before deleting, and
  warn beforehand that remaining paid days will be lost and not refunded automatically.
  After deletion, the system MUST keep only a minimal payment record (Telegram id, amount,
  date, payment reference, refund status) for each payment, and MUST delete it 3 years after
  the data deletion. Payments of a person who has not deleted their data are kept while their
  data exists. The warning MUST say so.
- **FR-021**: Payments MUST be a deployment setting, off by default. When off and no Stars
  subscription is running, the bot behaves exactly as in specs/005, apart from the
  `/account`, `/paysupport` and `/terms` commands, which are always available.

### Key Entities

- **Plan**: what can be bought. It has a price in Stars per 30 days, a short description per
  language and whether new subscriptions are open. There is one plan in this feature.
- **Stars subscription**: a person's recurring subscription in Stars. It has the person, the
  price it started with, when it started, the end of the current paid period, whether it will
  renew, and when and how it was cancelled (by the person in the bot, in Telegram, by a
  refund, by data deletion or by a failed charge).
- **Payment**: one charge in Stars, first or renewal. It has the person's Telegram id, the
  amount, Telegram's payment reference, when it was paid, the 30-day period it bought and its
  refund status with time and actor. After data deletion only the minimal record of FR-020
  remains.
- **Subscription** (from specs/005): access periods now also record how they were obtained,
  either granted by the operator or paid in Stars, and link to the payment that bought them.
- **Payment support request**: a person's description of a payment problem, forwarded to the
  operator, with the Telegram id, the time and the payment references it concerns.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A person without access can go from the "no access" message to an active
  subscription in under 1 minute and at most 4 taps, with no operator involvement.
- **SC-002**: In testing, 100% of confirmed payments, first and renewal, are credited exactly
  once, including repeated confirmations and a restart between payment and crediting.
- **SC-003**: In testing, 0 people are charged when the pre-charge check fails (payments off,
  outdated offer, wrong price), and 0 people are charged after cancelling, being refunded or
  deleting their data.
- **SC-004**: A subscriber whose renewal succeeds never loses access and never gets an
  "access ended" notice.
- **SC-005**: The operator spends no time on routine subscriptions: after launch, every paid
  subscription and renewal happens without an operator action.
- **SC-006**: Cancelling takes a subscriber at most 2 taps from `/account` or from the
  reminder.
- **SC-007**: The operator can answer "how many Stars did we receive and refund last month,
  and how many subscriptions are active" in under 1 minute.
- **SC-008**: A refund takes the operator under 1 minute, and the person is told within
  1 minute after it.

## Assumptions

- **One plan, 30 days**: a single plan that renews every 30 days, the only period Telegram
  offers for Stars subscriptions. Its price in Stars is set by the operator, since prices are
  not announced yet. Several plans, trials, promo codes, discounts and gifting are out of
  scope.
- **Stars only**: no card payments through payment providers and no outside payment links.
  Granting a subscription by hand after an outside payment (specs/005 FR-017) keeps working.
- **Converting Stars to money** (withdrawal through Telegram) is done by the operator outside
  Scribe and is out of scope.
- **Refunds** are manual, by the operator, for a whole payment. There are no automatic or
  partial refunds. Telegram also lets people dispute payments. Such cases reach the operator
  through `/paysupport`.
- **Payment records and data deletion** (clarified 2026-11-07): specs/005 says deletion
  removes everything. For payments, a minimal accounting record is kept for 3 years, because
  the operator needs it for refunds, disputes and tax. This is a documented exception to
  Principle II and must be recorded in the plan's Constitution Check.
- **Beta testers** keep free access. They are not asked to pay in this feature. Moving beta
  testers to paid plans is a later decision.
- **Landing page**: updating the landing page's "Early access" pricing card is out of scope.
  It can follow once a price is set.
- **Telegram's rules for digital goods** apply: payment in Stars, a `/paysupport` command and
  a terms page. They are reflected in Story 3.
