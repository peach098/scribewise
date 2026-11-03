# Feature Specification: Telegram QR-Code Login

**Feature Branch**: `002-telegram-qr-login`

**Created**: 2026-10-20

**Status**: Draft

**Input**: User description: "offer another authentication method for our service to access user's messages and replies upon his telegram account"

**Builds on**: `specs/001-telegram-reply-agent` (FR-022 guided setup, FR-030 disconnect /
reconnect Telegram). Today the only way to connect a Telegram account is: send the phone
number to the control chat → receive a login code from Telegram → type the code into the
control chat with spaces between digits → type the two-step verification password if one
is set.

## Problem

The current login has three problems:

- **Friction**: Telegram blocks login codes that are forwarded or pasted into a chat, so
  users must retype the code with spaces between the digits. Users often get this wrong or
  let the code expire, and have to start over.
- **Trust**: Users have to send their phone number and a login code to a bot, which looks
  like a common phishing pattern. Some users stop at this step.
- **Reliability**: Telegram sometimes delays the code or limits how often it can be
  requested, and then the user cannot continue setup.

This feature adds a **QR-code login** as a second way to connect. The user approves the
connection from a Telegram app where they are already signed in (Settings → Devices →
Link Desktop Device), in the same way they would sign in to Telegram Desktop or Telegram
Web. They never type their phone number or a login code.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Connect my Telegram account by scanning a QR code (Priority: P1)

During setup, the user is asked how they want to connect their Telegram account and picks
"Scan a QR code" (offered first, as the recommended option) or "Use phone number and code"
(the existing method). If they choose the QR code, the control chat shows a QR code with
short instructions. The user opens Telegram on another device where they are signed in,
goes to Settings → Devices → Link Desktop Device, and scans the code. Once Telegram
confirms, the control chat says the account is connected (and names it, so the user can
check it is the right one), and setup continues to choosing work chats.

**Why this priority**: This is the feature. It removes the phone-number and code steps
that cause most failed setups.

**Independent Test**: Start setup with a test user, pick the QR option, scan the code from
a second device where the test account is signed in, and check that the control chat
confirms the connection, names the right account, and moves on to choosing work chats.
At no point should the user be asked for a phone number or login code.

**Acceptance Scenarios**:

1. **Given** a new user at the Telegram connection step of setup, **When** the step
   starts, **Then** the user is offered both methods, with the QR code shown first and
   marked as recommended.
2. **Given** the user chose the QR code, **When** it is shown, **Then** the control chat
   displays the QR code and step-by-step instructions for finding the scanner in the
   Telegram app, including how long the code stays valid.
3. **Given** a QR code is shown, **When** the user scans it from a signed-in Telegram app
   and confirms, **Then** within 10 seconds the control chat confirms the connection,
   shows which Telegram account was connected (name and username or masked phone number),
   and setup continues to the next step.
4. **Given** the QR code expires before it is scanned, **When** the user is still on this
   step, **Then** a fresh code replaces the old one automatically (and the old one stops
   working), up to 5 codes in a row, after which the user is asked to tap "Show a new code" to
   continue.
5. **Given** a QR code is shown, **When** the user taps "Use phone number instead",
   **Then** the QR code stops working and the existing phone-and-code flow starts.
6. **Given** the user connected by QR code, **When** setup finishes, **Then** reading,
   drafting, and sending work exactly as with an account connected by phone and code.

---

### User Story 2 - Complete the QR login when my account has two-step verification (Priority: P1)

Many work accounts have a two-step verification password. After the user scans the QR
code, Telegram still requires that password. The control chat asks for it, as in the
existing flow, and removes the message with the password from the chat once it has been
used.

**Why this priority**: Without this, the QR method fails for every user with two-step
verification, which is likely to be a large share of work accounts.

**Independent Test**: With a test account that has two-step verification, scan the QR
code, check that the control chat asks for the password, enter the wrong one and then the
right one, and check that the account connects and the password messages are removed.

**Acceptance Scenarios**:

1. **Given** an account with two-step verification, **When** the user scans and confirms
   the QR code, **Then** the control chat asks for the two-step verification password and
   says why it is needed.
2. **Given** the password prompt, **When** the user sends a wrong password, **Then** they
   are told it is wrong and can try again, and the message with the wrong password is
   removed from the chat.
3. **Given** the password prompt, **When** the user sends the right password, **Then** the
   account is connected, the message with the password is removed, and setup continues.
4. **Given** the password prompt, **When** the user does not answer for 10 minutes,
   **Then** the login attempt is cancelled and the user can start again from the method
   choice.

---

### User Story 3 - Reconnect my Telegram account with a QR code (Priority: P2)

A user whose Telegram connection ended (they disconnected it, removed the session from
Telegram's "Active sessions", or Telegram ended it) reconnects from the control chat. They
get the same choice of methods as during setup. After reconnecting, the agent resumes
with the user's existing settings, work chats, and style profiles; nothing has to be set
up again.

**Why this priority**: Reconnecting happens more often than first setup for long-term
users, and is just as affected by the code-entry problems, but it only matters after
Story 1 works.

**Independent Test**: Connect a test account, end the session from Telegram's "Active
sessions" on another device, reconnect via QR code from the control chat, and check that
work chats, delays, and style profiles are unchanged and new messages are drafted again.

**Acceptance Scenarios**:

1. **Given** a user whose Telegram connection was ended outside the service, **When** the
   service notices, **Then** the user is told in the control chat that the connection was
   lost, with a button to reconnect.
2. **Given** the user chooses to reconnect, **When** the reconnect starts, **Then** the
   user is offered both methods, with the QR code shown first.
3. **Given** the user reconnects with a QR code, **When** it succeeds, **Then** reading
   and drafting resume with the existing settings, work chats, and style profiles, and
   messages that arrived while disconnected are handled as described in the existing
   "Telegram connection unavailable" behaviour.
4. **Given** a user reconnects, **When** the QR code is scanned with a **different**
   Telegram account than the one connected before, **Then** the connection is not
   accepted and the user is told to scan with the original account, or to delete their
   data and set up again if they want to switch accounts.

---

### User Story 4 - Know which method connected my account and how to end it (Priority: P3)

The user can see in the control chat how and when their Telegram account was connected,
and is reminded how to end the connection themselves from Telegram's "Active sessions",
where the service's session is shown under a recognisable name.

**Why this priority**: Supports trust and self-service, but the feature works without it.

**Independent Test**: Connect by QR code, open the Telegram status in the control chat,
and check that it shows the method, the time of connection, and the session name; then
check that the same name appears in Telegram's "Active sessions" list.

**Acceptance Scenarios**:

1. **Given** a connected account, **When** the user opens the Telegram status in the
   control chat, **Then** they see the connected account, the connection method (QR code
   or phone and code), the date and time it was connected, and how to end the session
   from Telegram.
2. **Given** a connected account, **When** the user looks at "Active sessions" in their
   Telegram app, **Then** the service's session appears under the service's name, so it is
   easy to recognise and end.

---

### Edge Cases

- **User has only one device**: The QR code cannot be scanned with the camera of the same
  phone that shows it. The instructions say a second signed-in device (phone, tablet, or
  computer) is needed, and the phone-and-code option stays one tap away. Where the user's
  Telegram app supports it, the QR step also offers a link that the user can open on the
  same device to approve the login without scanning.
- **Somebody else scans the QR code** (e.g., the user shared a screenshot): only a single
  approval is possible per code, the connected account is always shown to the user for
  confirmation, and during reconnect a different account is refused (Story 3, scenario
  4). During first setup, the user can tap "This is not my account" to disconnect it
  immediately and start again.
- **The QR code is forwarded or copied out of the control chat**: each code expires
  within Telegram's short validity window and is replaced, so an old copy is useless.
  Codes are only ever shown in the user's own private control chat.
- **The user starts a second login attempt while one is in progress** (e.g., sends
  `/start` again or opens reconnect twice): the earlier attempt is cancelled and its code
  stops working; only one attempt per user is active at a time.
- **Telegram rejects or rate-limits login attempts**: the user is told to wait and for how
  long, if known, and is offered the other method.
- **The service restarts during a QR login**: the attempt is cancelled and, once the
  service is back, the user is told to start again; no partly connected account is left
  behind.
- **The user's Telegram app is too old to show "Link Desktop Device"**: the instructions
  mention updating Telegram, and the phone-and-code option is offered.
- **The user takes too long overall**: after 5 unscanned codes in a row (about
  2–3 minutes), automatic refreshing stops and the user must tap "Show a new code", so
  the service does not keep generating codes for someone who has walked away.

## Requirements *(mandatory)*

### Functional Requirements

**Choosing a method**

- **FR-001**: When connecting or reconnecting a Telegram account, System MUST offer two
  methods: QR code and phone number with login code. The QR code MUST be shown first and
  marked as recommended.
- **FR-002**: Users MUST be able to switch from one method to the other at any point
  before the connection is complete, and switching MUST cancel the attempt in progress.
- **FR-003**: The existing phone-and-code method MUST keep working as it does now.

**QR-code login**

- **FR-004**: System MUST show the QR code as an image in the user's private control chat,
  with instructions for scanning it from a signed-in Telegram app (Settings → Devices →
  Link Desktop Device) and how long it stays valid.
- **FR-005**: System MUST replace an expired QR code with a new one automatically, and
  MUST make sure an old code can no longer be used once it is replaced. After 5 codes in
  a row expire without a scan (the first code plus 4 automatic replacements), System MUST
  stop and ask the user to tap "Show a new code".
- **FR-006**: When the code is approved, System MUST confirm the connection in the control
  chat within 10 seconds and show which account was connected (display name plus username
  or a masked phone number).
- **FR-007**: If the account has two-step verification, System MUST ask for the password
  in the control chat after the scan, allow retries after a wrong password, and cancel the
  attempt after 10 minutes without an answer.
- **FR-008**: System MUST delete each message containing a two-step verification password
  from the control chat once it has been processed (as far as Telegram allows), and MUST
  NOT store or log the password, the QR code contents, or any login code.
- **FR-009**: Only one login attempt per user MUST be active at a time; starting a new one
  MUST cancel the previous one and make its QR code unusable.
- **FR-010**: Where the user's Telegram app supports it, System MUST also offer a
  link next to the QR code that approves the login on the same device without scanning.

**Account checks**

- **FR-011**: When reconnecting, System MUST refuse a connection from a Telegram account
  that is different from the one previously connected for this user, and MUST explain how
  to switch accounts (delete data and set up again).
- **FR-012**: During first setup, System MUST let the user reject the account that was
  just connected ("This is not my account"), which MUST end that Telegram session
  immediately and return the user to the method choice.
- **FR-013**: A Telegram account MUST be connected to at most one user of the service at
  a time; a connection to an account already linked to another user MUST be refused.

**After connecting**

- **FR-014**: An account connected by QR code MUST behave exactly like one connected by
  phone and code for reading, drafting, sending, disconnecting, and data deletion; the
  stored session MUST be protected in the same way.
- **FR-015**: System MUST record, per user, the connection method and the time the
  account was connected, and show both in the Telegram status in the control chat.
- **FR-016**: The service's session MUST appear in the user's Telegram "Active sessions"
  list under the service's recognisable name.
- **FR-017**: When the Telegram connection ends outside the service, System MUST tell the
  user in the control chat and offer to reconnect with either method.
- **FR-018**: System MUST record each login attempt (method, start time, outcome:
  connected / expired / cancelled / wrong account / account already in use / not my
  account / failed / interrupted by a restart) without any secret values, so the share of
  successful logins per method can be measured.

### Key Entities

- **Telegram Connection** (extends the existing connection of a User Account): the
  connected Telegram account's identity, the method used (QR code or phone and code),
  the time it was connected, and the protected session.
- **Login Attempt**: one try at connecting an account for a user; method, current step
  (waiting for scan, waiting for password, done), number of QR codes shown, start time,
  outcome. At most one active per user; holds no secrets once finished.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Users who choose the QR code connect their account in under 2 minutes from
  choosing the method (median), compared with the current phone-and-code flow.
- **SC-002**: At least 90% of QR-code login attempts that reach a scan end in a connected
  account, and the failure rate of QR-code attempts is at most half the failure rate of
  phone-and-code attempts measured over the same period.
- **SC-003**: At least 60% of new users choose the QR-code method when both are offered.
- **SC-004**: Zero phone numbers, login codes, QR-code contents, or two-step verification
  passwords appear in stored data or logs (verified by inspection of logs and stored
  data after test logins).
- **SC-005**: Zero cases where a reconnect attaches a different Telegram account than the
  one the user had connected before.
- **SC-006**: Users who reconnect by QR code have all their previous settings, work chats,
  and style profiles in place, with no setup steps repeated (verified on 100% of test
  reconnects).

## Assumptions

- The "other authentication method" is login by QR code, approved from a Telegram app
  where the user is already signed in. It is the only alternative to phone-and-code
  that Telegram offers for a full user-account connection. Telegram's "Log in with
  Telegram" website button and bots cannot read or send messages as the user, so they
  do not fit this purpose.
- Importing an existing session from another Telegram app (e.g., a session file from
  Telegram Desktop) is **out of scope**: it is error-prone and would teach users to hand
  over session files, which is a known account-theft pattern.
- A web page for login is out of scope: the existing feature decided on no web app in
  this release, so the QR code is shown in the Telegram control chat.
- Most users have Telegram signed in on at least two devices (typically phone and
  computer). Users who do not can use the same-device link where supported, or the
  phone-and-code method.
- Two-step verification is still required by Telegram after a QR scan. This feature does
  not remove the password step; it only removes the phone number and login code.
- QR codes are valid for Telegram's own validity window (currently about 30 seconds);
  the "5 codes" limit in FR-005 is based on that.
- The baseline for SC-001 and SC-002 is the phone-and-code flow, measured alongside the
  QR-code flow with the same login-attempt records (FR-018); no attempts were recorded
  before this feature.
- Existing users connected by phone and code are not affected and do not need to
  reconnect.
