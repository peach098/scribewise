# Contract: Control Chat — Telegram Connection (delta)

Extends `specs/001-telegram-reply-agent/contracts/control-chat.md`. Callback data keeps the
format `<action>:<entity_id>[:<extra>]`, ≤ 64 bytes; `0` is used when there is no entity.
All texts below are the English source strings; wording may be polished but the buttons,
callbacks and order are fixed.

## Commands (changed / new)

| Command | Purpose | Spec |
|---|---|---|
| `/start` | Unchanged for new users and for users still in `setting_up` (→ connect step). For a user who finished setup and whose session is `revoked`/`error` (including after Disconnect): "Your Telegram account is disconnected. Use /telegram to reconnect." + **🔗 Reconnect** | 001 FR-022 |
| `/telegram` | Status card (below) with **Reconnect** or **Disconnect**. Replaces 001 T071. | FR-015, FR-017, 001 FR-030 |
| `/cancel` | During any connect state: ends the attempt (`cancelled`), drains a QR client (research Q4). | FR-002, FR-009 |

## Setup FSM states (changed)

`link` → **`connect_choose`** → (`connect_qr` | `connect_phone` → `connect_code`) →
`connect_password?` → `pick_chats` → … (rest unchanged from 001).

The same `connect_*` states are used by `/telegram` → Reconnect with `purpose=reconnect`;
on success, reconnect ends the FSM instead of going to `pick_chats`.

## Method choice (`connect_choose`)

```
🔗 Connect your Telegram account

I'll read your work chats and send the replies you approve from your account.
Choose how to connect:

• QR code (recommended) — scan it from Telegram on another device. You don't share
  your phone number or a login code.
• Phone number — Telegram sends you a login code that you type here.
```

| Button | callback | Effect |
|---|---|---|
| 📷 Scan a QR code (recommended) | `tglogin:0:qr` | Start QR attempt → `connect_qr` |
| 📱 Use phone number and code | `tglogin:0:phone` | → `connect_phone` (existing phone prompt) |

## QR step (`connect_qr`)

A **photo** message (the QR image) with this caption, edited in place on each refresh:

```
📷 Scan this code to connect

1. Open Telegram on another device where you're signed in (phone, tablet or computer).
2. Go to Settings → Devices → Link Desktop Device.
3. Point the camera at this code.

The code changes every ~30 seconds; I'll replace it automatically.
No "Link Desktop Device"? Update Telegram, or use your phone number instead.
Code 2 of 5 · valid until 14:03:27
```

| Button | callback | Shown | Effect |
|---|---|---|---|
| 📲 Approve on this device | URL `tg://login?token=…` | only if `tg_qr_same_device_link=true` | Opens the Telegram app (research Q5) |
| 📱 Use phone number instead | `tglogin:0:phone` | always | Cancel + drain QR; image message → "Switched to phone number." |
| ✖️ Cancel | `tglogin:0:cancel` | always | Same as `/cancel` |

After 5 unscanned codes, the photo is replaced by text (no scannable image remains):

```
⌛ The code expired. Tap below when you're ready to scan.
```
Buttons: **🔄 Show a new code** (`tglogin:0:qr`), **📱 Use phone number instead**.

## Phone step (unchanged prompts, extra button)

`connect_phone` / `connect_code` keep the 001 texts. The phone and code prompts get a
**📷 Use a QR code instead** button (`tglogin:0:qr`).

## Password step (`connect_password`) — both methods

```
🔒 Your account has two-step verification. Telegram needs your password to finish
connecting. Send it here — I'll delete your message right away and never store it.
(This request expires in 10 minutes.)
```
- Wrong password: "Wrong password. Please send it again." (message with the password is
  deleted every time).
- 10 minutes without a reply: "⌛ The connection attempt timed out." + method choice
  buttons.

## Connected (setup)

```
✅ Connected as Anna Petrova (@anna_p).
```
Button: **This is not my account** (`tglogin:0:notme`), valid until setup completes. Then
the 001 chat picker message follows immediately.

Pressing **This is not my account**: session logged out and removed →
"Disconnected that account. Choose how to connect:" + method choice buttons.

The account line shows `@username` if there is one, otherwise a masked phone number
(`+7 ••• ••• •• 34`).

## Connected (reconnect)

```
✅ Reconnected as Anna Petrova (@anna_p). I'm reading your work chats again.
```
If the user has `waiting` turns whose `due_at` has passed, followed by:

```
📥 3 messages arrived while you were disconnected — I'm processing them now.
```

## Error messages

| Situation | Text | Buttons |
|---|---|---|
| Different account on reconnect | "That's a different Telegram account: Ivan K. (@ivan_k). Reconnect with the account you used before (Anna Petrova (@anna_p)), or use /delete_my_data and set up again to switch accounts. Nothing was connected." Both names come from `account_label` (the stored one for the expected account). | method choice |
| Account already linked to another user | "This Telegram account is already connected to another Scribe account. Nothing was connected." | method choice |
| Rate limited | "Telegram asks to wait N s before the next attempt." | other method |
| Old code scanned after cancel (research Q4) | "That code was no longer active; nothing was connected." | — |
| Service restarted mid-login (research Q10) | "The service restarted while you were connecting Telegram. Let's start again." | method choice |
| Telegram unavailable | 001 `TELEGRAM_UNAVAILABLE_TEXT` | method choice |

## `/telegram` status card

```
📡 Telegram connection
Account: Anna Petrova (@anna_p)
Status: ✅ connected  |  ⚠️ disconnected (logged out from another device)  |  ❌ error
Connected: 20 Oct 2026, 14:03 · via QR code | via phone and code

To end this connection yourself: Telegram → Settings → Devices → "Scribe Agent" →
Terminate session.
```

| Button | callback | Shown when | Effect |
|---|---|---|---|
| 🔌 Disconnect | `tgdisconnect:0` | connected | Two-step confirm (`tgdisconnect:1`) → log the session out, stop the client, session `revoked`; `users.state` unchanged (replaces 001's "user → `setting_up`", research Q9) |
| 🔗 Reconnect | `tgreconnect:0` | revoked / error | Method choice with `purpose=reconnect` |

## Notifications (changed)

The 001 revoked / error notices from `client_pool.py` now read "… Use /telegram to
reconnect." and carry the **🔗 Reconnect** button (`tgreconnect:0`).
