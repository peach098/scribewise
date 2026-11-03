"""English control-bot texts (the default). Keys and placeholders must match ``ru.py``."""

from __future__ import annotations

from collections.abc import Mapping

CATALOG: Mapping[str, str | Mapping[str, str]] = {
    # --- languages (endonyms, the same in every catalog) ---
    "language.en": "English",
    "language.ru": "Русский",
    # --- dates and times ---
    "month.1": "Jan",
    "month.2": "Feb",
    "month.3": "Mar",
    "month.4": "Apr",
    "month.5": "May",
    "month.6": "Jun",
    "month.7": "Jul",
    "month.8": "Aug",
    "month.9": "Sep",
    "month.10": "Oct",
    "month.11": "Nov",
    "month.12": "Dec",
    "day.0": "Mon",
    "day.1": "Tue",
    "day.2": "Wed",
    "day.3": "Thu",
    "day.4": "Fri",
    "day.5": "Sat",
    "day.6": "Sun",
    "date.day_month": "{day} {month}",
    "date.full": "{day} {month} {year}, {time}",
    "when.today": "{time}",
    "when.tomorrow": "{time} tomorrow",
    "when.yesterday": "{time} yesterday",
    "when.other": "{time} {date}",
    "unit.h": "{n} h",
    "unit.min": "{n} min",
    "unit.s": "{n} s",
    # --- commands (the / menu) and /help ---
    "cmd.start": "Create your account and run setup",
    "cmd.chats": "Choose work chats and per-chat settings",
    "cmd.settings": "Reading delay, working hours and days, time zone, language",
    "cmd.profile": "Your writing style profile and rules",
    "cmd.commitments": "Open commitments you made",
    "cmd.waiting": "Messages waiting to be read",
    "cmd.pause": "Pause drafting (messages stay unread)",
    "cmd.resume": "Resume drafting",
    "cmd.ooo": "Set or clear out-of-office",
    "cmd.tasks": "Connect or disconnect your task tracker",
    "cmd.history": "Recent activity (no message content)",
    "cmd.my_data": "What I store about you; export it",
    "cmd.telegram": "Telegram connection status, disconnect or reconnect",
    "cmd.delete_my_data": "Delete everything and log out",
    "cmd.help": "This list",
    "help.intro": (
        "Scribe drafts replies to your work chats; nothing is sent without your approval."
    ),
    # --- common ---
    "common.not_set_up": "Send /start first.",
    "common.someone": "Someone",
    "common.chat": "Chat",
    "common.send_to_continue": "{problem} Send {command} to continue.",
    "common.try_again": "⚠️ {problem} Try again or /cancel.",
    "alert.saved": "Saved",
    "alert.unknown_tz": "Unknown time zone",
    "alert.chat_not_found": "Chat not found",
    "alert.option_removed": "This option is no longer available",
    "alert.global_delay": "Uses the global delay now",
    # --- /start ---
    "start.welcome": (
        "👋 Hi! I'm Scribe. I read the work chats you choose, wait a little like a person "
        "would, and prepare reply drafts in your style. Nothing is ever sent without your "
        "explicit approval.\n"
        "\n"
        "Your account is created and setup has started. Next, I'll ask you to connect your "
        "Telegram account. Heads-up: Telegram has no limited permissions for this, so the "
        "connection gives full access to your account. I only read the chats you enable, and "
        "you can disconnect at any time (/telegram, or Settings → Devices → Active sessions in "
        "Telegram).\n"
        "\n"
        "Send /help to see all commands."
    ),
    "start.in_progress": "Setup is in progress. Send /help to see all commands.",
    "start.already_set_up": "You're already set up. Send /help to see all commands.",
    # --- setup steps ---
    "setup.pick_prompt": (
        "💼 Step 2 — choose your work chats. I only read and store the chats you enable here. "
        "Tap a chat to toggle it, then press Done. "
        "In groups I prepare a draft only when you are tagged or someone replies to your message."
    ),
    "setup.pick_none": "Pick at least one work chat.",
    "setup.chats_saved": "💼 Work chats saved: {n}.",
    "setup.tz_prompt": (
        "🌍 Step 3 — your time zone, so working hours follow your clock. Send your city "
        "(e.g. Moscow, Berlin, New York), an IANA time zone (Europe/Moscow) or a UTC offset "
        "(UTC+3), or tap one below."
    ),
    "setup.tz_unknown": "⚠️ {problem} Try again, or /cancel to stop setup.",
    "setup.importing": (
        "⏳ Learning your style from your own messages in the chosen chats (last 6 months)…"
    ),
    "setup.learned": {
        "one": "✅ Learned your style from {n} of your messages.",
        "other": "✅ Learned your style from {n} of your messages.",
    },
    "setup.weak": (
        "That's fewer than {n}, so style matching will be weak at first and drafts use a "
        "neutral, polite tone."
    ),
    "setup.learn_failed": (
        "⚠️ I couldn't learn your style right now, so drafts use a neutral, polite tone."
    ),
    "setup.done": (
        "✅ All set! Default reading delay {delay}, working hours {start}–{end}. "
        "Your first draft will appear here when someone writes in one of your work chats. "
        "Nothing is ever sent without your approval."
    ),
    "setup.cancelled": "Setup cancelled. Your account stays in setup; send /start to begin again.",
    "setup.reconnect_cancelled": "Reconnect cancelled. Use /telegram to try again.",
    "btn.pick_done": "Done ({n} selected)",
    "btn.keep_tz": "Keep {zone}",
    # --- connecting Telegram ---
    "connect.method_choice": (
        "🔗 Connect your Telegram account\n"
        "\n"
        "I'll read your work chats and send the replies you approve from your account.\n"
        "Choose how to connect:\n"
        "\n"
        "• QR code (recommended) — scan it from Telegram on another device. You don't share "
        "your phone number or a login code.\n"
        "• Phone number — Telegram sends you a login code that you type here."
    ),
    "connect.qr_expired": "⌛ The code expired. Tap below when you're ready to scan.",
    "connect.switched_to_phone": "Switched to phone number.",
    "connect.password_prompt": (
        "🔒 Your account has two-step verification. Telegram needs your password to finish "
        "connecting. Send it here — I'll delete your message right away and never store it.\n"
        "(This request expires in 10 minutes.)"
    ),
    "connect.login_timeout": "⌛ The connection attempt timed out.",
    "connect.connected": "✅ Connected as {account}.",
    "connect.not_my_account_done": "Disconnected that account. Choose how to connect:",
    "connect.reconnected": "✅ Reconnected as {account}. I'm reading your work chats again.",
    "connect.backlog": {
        "one": "📥 {n} message arrived while you were disconnected — I'm processing it now.",
        "other": ("📥 {n} messages arrived while you were disconnected — I'm processing them now."),
    },
    "connect.wrong_account": (
        "That's a different Telegram account: {other}. Reconnect with the account you used "
        "before ({expected}), or use /delete_my_data and set up again to switch accounts. "
        "Nothing was connected."
    ),
    "connect.original_account": "the original",
    "connect.account_taken": (
        "This Telegram account is already connected to another Scribe account. "
        "Nothing was connected."
    ),
    "connect.rate_limited": "Telegram asks to wait {seconds} s before the next attempt.",
    "connect.late_scan": "That code was no longer active; nothing was connected.",
    "connect.restarted": (
        "The service restarted while you were connecting Telegram. Let's start again."
    ),
    "connect.not_your_button": "This button only works in your own chat with me.",
    "connect.qr_caption": (
        "📷 Scan this code to connect\n"
        "\n"
        "1. Open Telegram on another device where you're signed in (phone, tablet or "
        "computer).\n"
        "2. Go to Settings → Devices → Link Desktop Device.\n"
        "3. Point the camera at this code.\n"
        "\n"
        "The code changes every ~30 seconds; I'll replace it automatically.\n"
        'No "Link Desktop Device"? Update Telegram, or use your phone number instead.\n'
        "Code {code_no} of {total} · valid until {until}"
    ),
    "connect.unknown_account": "Unknown account",
    "connect.phone_prompt": (
        "📱 Step 1 — connect your Telegram account.\n"
        "Send the phone number of your Telegram account in international format, "
        "e.g. +44 7700 900123. Send /cancel to stop."
    ),
    "connect.code_prompt": (
        "🔑 Telegram has sent you a login code (look for a message from “Telegram”). "
        "Send it here with spaces between the digits, e.g. 1 2 3 4 5: Telegram blocks codes "
        "that are forwarded as they are."
    ),
    "connect.invalid_phone": (
        "That doesn't look like a valid phone number of a Telegram account. "
        "Send it in international format, e.g. +44 7700 900123."
    ),
    "connect.invalid_code": (
        "That code didn't work. Check it and send it again with spaces between digits."
    ),
    "connect.code_expired": "That code can't be used any more. Choose how to connect:",
    "connect.invalid_password": "Wrong password. Please send it again.",
    "connect.password_as_text": "Send your password as a text message.",
    "connect.login_lost": "This login attempt is no longer active. Choose how to connect:",
    "connect.telegram_unavailable": (
        "I couldn't reach Telegram right now. Please try again in a minute."
    ),
    "connect.not_my_account_unavailable": (
        "Setup is already finished; use /telegram to manage the account."
    ),
    "connect.finish_connecting": "Finish connecting first, or send /cancel.",
    "btn.use_phone_instead": "📱 Use phone number instead",
    "btn.qr_recommended": "📷 Scan a QR code (recommended)",
    "btn.phone_and_code": "📱 Use phone number and code",
    "btn.approve_same_device": "📲 Approve on this device",
    "btn.cancel": "✖️ Cancel",
    "btn.new_code": "🔄 Show a new code",
    "btn.use_qr_instead": "📷 Use a QR code instead",
    "btn.not_my_account": "This is not my account",
    "btn.reconnect": "🔗 Reconnect",
    # --- /telegram ---
    "telegram.disconnected_start": (
        "Your Telegram account is disconnected. Use /telegram to reconnect."
    ),
    "telegram.not_connected": (
        "Your Telegram account isn't connected yet. Send /start to set it up."
    ),
    "telegram.already_connected": "Your Telegram account is already connected.",
    "telegram.disconnect_confirm": (
        "🔌 Disconnect your Telegram account?\n"
        "I'll log my session out and stop reading your work chats until you reconnect. "
        "Your settings, work chats and style profiles are kept."
    ),
    "telegram.disconnected": (
        "🔌 Disconnected. I've stopped reading your work chats; your settings are kept. "
        "Use /telegram to reconnect."
    ),
    "telegram.status.active": "✅ connected",
    "telegram.status.revoked": "⚠️ disconnected",
    "telegram.status.error": "❌ error",
    "telegram.logged_out_elsewhere": "{status} (logged out from another device)",
    "telegram.method.qr": "via QR code",
    "telegram.method.phone_code": "via phone and code",
    "telegram.card.title": "📡 Telegram connection",
    "telegram.card.account": "Account: {account}",
    "telegram.card.status": "Status: {status}",
    "telegram.card.connected": "Connected: {when} · {method}",
    "telegram.card.terminate": (
        'To end this connection yourself: Telegram → Settings → Devices → "{device}" → '
        "Terminate session."
    ),
    "btn.disconnect": "🔌 Disconnect",
    "btn.disconnect_yes": "🔌 Yes, disconnect",
    # --- background notices ---
    "notice.session_revoked": (
        "⚠️ Your Telegram session was logged out or revoked, so I stopped reading your work "
        "chats. Use /telegram to reconnect."
    ),
    "notice.session_error": (
        "⚠️ I couldn't connect to your Telegram account, so I stopped reading your work chats."
        " Use /telegram to reconnect."
    ),
    "notice.media_only": (
        "📎 {name} sent a voice/photo/file — I can't draft for that; please handle it manually."
    ),
    "notice.manual_reply": (
        "✅ You replied to {name} yourself — dropped the waiting message/draft."
    ),
    "notice.this_chat": "this chat",
    "notice.reminder": "⏰ Still waiting for your decision on this draft.",
    # --- draft card ---
    "card.unknown_contact": "Unknown",
    "card.kind.private": "private chat",
    "card.kind.group": "group",
    "card.kind.channel_comments": "comments",
    "card.read": "read {time}",
    "card.tone.none": "none",
    "card.tone.more_formal": "more formal",
    "card.tone.urgent": "more urgent",
    "card.tone.emotional": "more emotional",
    "card.profile.default": "default",
    "card.profile.global": "global",
    "card.profile.contact": "contact",
    "card.commits": "🤝 commits you to {what}",
    "card.commitment_something": "something",
    "card.commitment_due": "{what} (by {due})",
    "card.conflict": '⚠️ conflicts with "{what}"',
    "card.sensitive": "🔒 sensitive",
    "card.sensitive_categories": "🔒 sensitive ({categories})",
    "card.sensitive.credentials": "credentials",
    "card.sensitive.payment": "payment",
    "card.sensitive.personal_data": "personal data",
    "card.sensitive.legal": "legal",
    "card.sensitive.hr": "HR",
    "card.flags": "⚑ Flags: {flags}",
    "card.needs_info": "❓ Needs info:\n{questions}",
    "card.needs_info_see_draft": "❓ Needs info: see the draft",
    "card.fill_hint_one": "↩️ Reply with the missing value, or tap ✏️ Edit to rewrite.",
    "card.fill_hint_many": (
        "↩️ Reply with the missing values (one per line), or tap ✏️ Edit to rewrite."
    ),
    "card.sources": "📎 Sources: {sources}",
    "card.sources_more": "+{n} more",
    "card.warning.tasks_unavailable": "⚠️ drafted without task context",
    "card.warning.weak_style_profile": "⚠️ weak style profile: not enough of your messages yet",
    "card.warning.history_truncated": "⚠️ older history was left out",
    "card.warning.filter_unavailable": (
        "⚠️ context selection failed: used the most recent messages"
    ),
    "card.warning.unsupported_sources": (
        "⚠️ the draft cited sources I could not verify: check the facts"
    ),
    "card.warning.other": "⚠️ {code}",
    "card.expires": "⏳ Expires {when}",
    "card.draft": "✍️ Draft ({profile}, tone shift: {tone})",
    "card.truncated": "… (truncated)",
    "btn.send": "✅ Send",
    "btn.edit": "✏️ Edit",
    "btn.regen": "🔁 Regenerate",
    "btn.reject": "❌ Reject",
    "btn.sources": "🧾 Sources",
    "btn.send_anyway": "Send anyway",
    # --- review ---
    "review.expired": "This draft has expired",
    "review.not_found": "Draft not found",
    "review.edit_cancelled": "Edit cancelled. The draft is still waiting for your decision.",
    "review.send_failed": (
        "⚠️ I couldn't send this right now (Telegram connection problem). "
        "The draft is still pending: try again."
    ),
    "review.send_unresolved": (
        "⚠️ An earlier attempt to send this draft may already have reached the chat, so it "
        "can't be changed now. Tap ✅ Send to finish sending the same text (it won't be "
        "delivered twice), or ❌ Reject it."
    ),
    "review.already_delivered": (
        "✅ Telegram says this reply was already delivered by an earlier attempt, so I won't "
        "send it again. Please check the chat, then tap ❌ Reject to close the draft."
    ),
    "review.regen_failed": (
        "⚠️ I couldn't regenerate the draft right now. The current draft is still pending."
    ),
    "review.nothing_to_fill": (
        "This draft has no missing values to fill in. Tap ✏️ Edit to rewrite it."
    ),
    "review.outdated_banner": "⚠️ New messages arrived — this draft may be out of date.",
    "review.outdated_prompt": (
        "⚠️ New messages arrived after this draft. 🔁 Regenerate it, or Send anyway as it is?"
    ),
    "review.read_now": "👀 Reading now…",
    "review.read_now_paused": "Scribe is paused: this message will be read after /resume.",
    "review.turn_not_waiting": "This message is no longer waiting",
    "review.already_handled": "This draft was already handled",
    "review.no_sources": "🧾 This draft cites no sources.",
    "review.sources": "🧾 Sources for this draft:\n{sources}",
    "review.source_own": "your msg",
    "review.source_message": "message",
    "review.source_forwarded": "{who}, forwarded from {origin}",
    "review.source_forwarded_unknown": "{who}, forwarded",
    "review.reason.placeholders": "unresolved placeholders",
    "review.reason.sensitive": "sensitive content",
    "review.reason_and": "{first} and {second}",
    "review.confirm_send": "⚠️ This draft has {reasons}. Send anyway?",
    "review.placeholders_left": "⚠️ Your text still has [[…]] placeholders. Send anyway?",
    "review.sending": "Sending…",
    "review.rejected": "Rejected",
    "review.regenerating": "Regenerating…",
    "review.regen_limit": {
        "one": (
            "🔁 This draft was already regenerated {n} time, that's the limit. "
            "Edit it, send it as it is, or reject it and reply yourself."
        ),
        "other": (
            "🔁 This draft was already regenerated {n} times, that's the limit. "
            "Edit it, send it as it is, or reject it and reply yourself."
        ),
    },
    "review.fill_need_one": "1 value",
    "review.fill_need_many": {
        "one": "{n} value, one per line, in this order",
        "other": "{n} values, one per line, in this order",
    },
    "review.fill_mismatch": (
        "⚠️ This draft needs {need}:\n{questions}\n"
        "You sent {got}. Nothing was changed: reply again, or tap ✏️ Edit to rewrite."
    ),
    "status.sent": "✅ Sent {time}",
    "status.sent_edited": "✅ Sent (edited) {time}",
    "status.rejected": "❌ Rejected",
    "status.superseded": "🔁 Superseded by a new version",
    "status.filled": "✍️ Superseded: values filled in a new version",
    "status.expired": "⌛ Expired — handle manually",
    "status.manual_reply": "✅ You replied yourself — dropped",
    # --- edit prompt ---
    "edit.prompt": "✏️ Reply to this message with your version of the reply, or send /cancel.",
    "edit.inline_prompt": (
        "✏️ Tap the button to put the draft into the input field, change it and send it. "
        "Or type your version, or send /cancel."
    ),
    "edit.copy_prompt": (
        "✏️ Tap the button to copy the draft, paste it, change it and send it. "
        "Or type your version, or send /cancel."
    ),
    "edit.long_prompt": (
        "✏️ Tap the draft below to copy it, then reply to this message with your version, "
        "or send /cancel."
    ),
    "edit.placeholder": "Your version…",
    "btn.edit_inline": "✏️ Edit in input field",
    "btn.copy_draft": "📋 Copy draft",
    # --- /settings ---
    "settings.title": "⚙️ Settings",
    "settings.delay": "⏱ Reading delay: {value}",
    "settings.hours": "🕘 Working hours: {start}–{end}",
    "settings.days": "📅 Working days: {days}",
    "settings.tz": "🌍 Time zone: {zone}",
    "settings.language": "🌐 Language: {language}",
    "settings.drafting": "▶️ Drafting: {state}",
    "settings.per_chat": "Per-chat settings: /chats",
    "settings.state.active": "active (/pause to pause)",
    "settings.state.paused": "paused (/resume to resume)",
    "settings.state.out_of_office": "out of office (/ooo to turn it off)",
    "settings.state.setting_up": "setting up",
    "settings.delay_fixed": "{value} (fixed)",
    "settings.delay_random": "{low}–{high}, random for every message",
    "settings.saved": "✅ Saved.\n\n{settings}",
    "settings.finish_setup": "Finish setup first (/start).",
    "settings.cancelled": "Cancelled. Nothing was changed.",
    "settings.delay_prompt": (
        "⏱ Send the reading delay range, e.g. 2-10 min, 90-300 s or 1-2 h. "
        "The delay is picked at random within the range for every message; "
        "a single value (e.g. 5 min) gives a fixed delay. Numbers without a unit are minutes. "
        "Maximum 24 h. /cancel to keep the current value."
    ),
    "settings.hours_prompt": "🕘 Send your working hours, e.g. 09:00-19:00. /cancel to keep them.",
    "settings.tz_prompt": (
        "🌍 Send your city (e.g. Moscow, Berlin, New York), an IANA time zone (Europe/Moscow) "
        "or a UTC offset (UTC+3), or tap one below. /cancel to keep it."
    ),
    "settings.tz_choose": "🌍 {problem} Choose one, or send another city:",
    "settings.days_prompt": "📅 Tap to toggle working days:",
    "settings.language_prompt": "🌐 Choose the bot language:",
    "settings.chat_delay_prompt": (
        "⏱ Send the reading delay range for this chat, e.g. 5-15 min (0 reads at once). "
        "/cancel to keep it."
    ),
    "settings.chat": "💬 {title}\n⏱ Reading delay: {delay}",
    "settings.chat_global": "global ({value})",
    "settings.paused": (
        "⏸ Paused. Incoming work messages stay unread and no drafts are prepared until /resume."
    ),
    "settings.resumed": (
        "▶️ Resumed. Waiting messages: {count}; any that are already due are read now."
    ),
    "settings.already_paused": "Already paused. /resume to continue.",
    "settings.not_paused": "Scribe is not paused.",
    "btn.delay": "⏱ Delay",
    "btn.hours": "🕘 Hours",
    "btn.days": "📅 Days",
    "btn.tz": "🌍 Time zone",
    "btn.language": "🌐 Language",
    "btn.done": "Done",
    "btn.set_delay": "⏱ Set delay",
    "btn.use_global": "↩️ Use global",
    "btn.read_now": "👀 Read now",
    "err.units": "Use numbers with an optional unit: s, min or h.",
    "err.unknown_unit": "Unknown unit {unit!r}: use s, min or h.",
    "err.range_format": "Send one value or a range like 2-10 min.",
    "err.min_gt_max": "The minimum must not be larger than the maximum.",
    "err.max_delay": "The delay can be at most 24 h.",
    "err.hhmm": "Use HH:MM, e.g. 09:00-19:00.",
    "err.hours_range": "Hours go from 00:00 to 23:59.",
    "err.start_end": "Send a start and an end, e.g. 09:00-19:00.",
    "err.start_before_end": "The start must be earlier than the end.",
    "err.tz_pick": "{problem} Pick one of: {choices}.",
    "err.unknown_day": "Unknown day.",
    "err.keep_one_day": "Keep at least one working day.",
    # --- /chats, /waiting ---
    "chats.none": "💼 No work chats are enabled yet.",
    "chats.list": "💼 Your work chats. Tap one for its settings:",
    # specs/007: ➕ Add work chats in /chats
    "btn.chat_add": "➕ Add work chats",
    "chats.add_prompt": (
        "➕ Choose chats to add. I'll read only new messages in them, from now on.\n"
        "Chats already in /chats aren't listed; turn a disabled (⏸) one back on from its "
        "settings there."
    ),
    "chats.add_none_left": (
        "💼 All your Telegram chats are already in /chats. To turn a disabled (⏸) one back on, "
        "open its settings there."
    ),
    "chats.added": {
        "one": (
            "✅ Added {n} chat. I'm learning from your own messages in it in the background "
            "and will tell you when it's done."
        ),
        "other": (
            "✅ Added {n} chats. I'm learning from your own messages in them in the background "
            "and will tell you when it's done."
        ),
    },
    "chats.add_nothing": "Nothing was added.",
    "chats.add_cancelled": "Nothing was added.",
    "chats.add_hint": "Tap chats to select them, then Done. /cancel to stop.",
    "chats.add_nothing_to_learn": (
        "ℹ️ None of your own messages from the last {months} months are in the new chats, so "
        "your style profiles stay as they are."
    ),
    "chats.add_learn_failed": (
        "⚠️ The chats are added, but I couldn't learn from your messages in them right now. "
        "Your profiles are unchanged; try 🔄 Rebuild in /profile later."
    ),
    "alert.picker_closed": "This list is closed. Open /chats again.",
    "waiting.item": ("⏳ {contact} · arrived {arrived} · will be read at {when}\n› {preview}"),
    "waiting.now": "now",
    "waiting.nothing": "✅ Nothing is waiting to be read.",
    "waiting.paused": "⏸ Scribe is paused: these stay unread until /resume.",
    "waiting.more": "…and {n} more.",
    # --- time zones ---
    "tz.unknown": (
        "I don't know the time zone {text!r}. Send a city (Moscow, Berlin, New York), "
        "an IANA name (Europe/Moscow) or a UTC offset (UTC+3)."
    ),
    "tz.ambiguous": "Several time zones match {text!r}.",
    "tz.offset_minutes": "Offsets are whole hours or :30 / :45, e.g. UTC+3 or UTC+5:30.",
    "tz.offset_range": "UTC offsets go from UTC-12 to UTC+14.",
    "tz.no_zone_at": "No time zone is at {offset} now. Send your city instead.",
    "tz.offset_note": (
        "💡 A fixed offset doesn't follow daylight saving time. If your clocks change during "
        "the year, send your city instead (e.g. Berlin)."
    ),
    "tz.non_whole_note": (
        "💡 {zone} is at {offset} now. If that isn't your region, send your city instead."
    ),
    "tz.confirm": "✅ Time zone set to {zone} ({offset}, now {time} there).",
    # --- city names on time zone buttons (zones not listed show the IANA city) ---
    "city.Europe/London": "London",
    "city.Europe/Berlin": "Berlin",
    "city.Europe/Moscow": "Moscow",
    "city.Asia/Dubai": "Dubai",
    "city.America/New_York": "New York",
    "city.America/Los_Angeles": "Los Angeles",
    "city.America/Chicago": "Chicago",
    "city.Europe/Kaliningrad": "Kaliningrad",
    "city.Europe/Samara": "Samara",
    "city.Asia/Yekaterinburg": "Yekaterinburg",
    "city.Asia/Omsk": "Omsk",
    "city.Asia/Novosibirsk": "Novosibirsk",
    "city.Asia/Krasnoyarsk": "Krasnoyarsk",
    "city.Asia/Irkutsk": "Irkutsk",
    "city.Asia/Yakutsk": "Yakutsk",
    "city.Asia/Vladivostok": "Vladivostok",
    "city.Asia/Magadan": "Magadan",
    "city.Asia/Kamchatka": "Kamchatka",
    "city.Europe/Kyiv": "Kyiv",
    "city.Europe/Kiev": "Kyiv",
    "city.Europe/Warsaw": "Warsaw",
    "city.Europe/Minsk": "Minsk",
    "city.Asia/Almaty": "Almaty",
    "city.Asia/Aqtobe": "Aqtobe",
    "city.Asia/Tashkent": "Tashkent",
    "city.Asia/Tbilisi": "Tbilisi",
    "city.Asia/Yerevan": "Yerevan",
    "city.Asia/Baku": "Baku",
    "city.Europe/Vienna": "Vienna",
    "city.Europe/Zurich": "Zurich",
    "city.Europe/Paris": "Paris",
    "city.Europe/Madrid": "Madrid",
    "city.Europe/Rome": "Rome",
    "city.Europe/Lisbon": "Lisbon",
    "city.America/Mexico_City": "Mexico City",
    "city.America/Sao_Paulo": "Sao Paulo",
    "city.Europe/Istanbul": "Istanbul",
    "city.Europe/Athens": "Athens",
    "city.Asia/Tokyo": "Tokyo",
    "city.Asia/Kolkata": "Kolkata",
    # --- /chats: per-chat overrides (US4) ---
    "settings.chat_language": "🌐 Language: {language}",
    "settings.chat_language_auto": "the language of the incoming message",
    "settings.chat_formality": "🎩 Formality: {value}",
    "settings.chat_formality_value": "{n}/5 (1 casual … 5 formal)",
    "settings.chat_formality_profile": "from your style profile",
    "settings.chat_group": "👥 Drafts in this group: only when you are tagged or replied to",
    "settings.chat_enabled": "▶️ Enabled: messages are read and drafted",
    "settings.chat_disabled": "⏸ Disabled: nothing is read or stored from this chat",
    "settings.chat_language_prompt": (
        "🌐 Send the language for drafts in this chat as a code (e.g. en, ru, de, pt-BR) or a "
        "name (English, Russian). /cancel to keep it."
    ),
    "settings.saved_short": "✅ Saved.",
    "alert.saved_check": "✅ Saved",
    "alert.chat_language_auto": "Drafts follow the incoming message's language",
    "status.chat_disabled": "⏸ Chat disabled — dropped",
    "err.chat_language": "Send a language code like en, ru or pt-BR, or a name like English.",
    "btn.chat_language": "🌐 Language",
    "btn.chat_language_auto": "↩️ Auto",
    "btn.chat_disable": "⏸ Disable chat",
    "btn.chat_enable": "▶️ Enable chat",
    # --- /ooo ---
    "ooo.on": (
        "🏖 Out of office is on. Drafts are prepared as out-of-office replies based on: "
        "“{text}”. /ooo turns it off."
    ),
    "ooo.on_paused": " Scribe is paused: this applies after /resume.",
    "ooo.off": "✅ Out of office is off. Drafts are prepared as usual.",
    "ooo.off_paused": " Scribe is still paused (/resume).",
    "ooo.usage": (
        "Out of office is off. To turn it on, send /ooo with the reply to suggest, "
        "e.g. /ooo On vacation until 20 Oct, back on Monday."
    ),
    # --- /history ---
    "history.empty": "🧾 No activity yet.",
    "history.header": {
        "one": "🧾 Recent activity (last {n}, newest first; no message content):",
        "other": "🧾 Recent activity (last {n}, newest first; no message content):",
    },
    "history.when": "{weekday} {date} {time}",
    "history.kind.draft_prepared": "✍️ Draft prepared",
    "history.kind.approved": "👍 Approved by you",
    "history.kind.edited_and_approved": "✏️ Edited and approved by you",
    "history.kind.rejected": "❌ Rejected by you",
    "history.kind.regenerated": "🔁 Regenerated",
    "history.kind.sent": "✅ Sent",
    "history.kind.expired": "⌛ Expired",
    "history.kind.dropped": "🗑 Dropped",
    "history.kind.read_now": "👀 Read now",
    "history.kind.profile_refreshed": "🔄 Profiles refreshed",
    "history.kind.profile_undo": "↩️ Profile refresh undone",
    "history.kind.rule_accepted": "💡 Style rule added",
    "history.kind.data_exported": "📦 Data exported",
    "history.kind.data_deleted": "🗑 Data deleted",
    "history.kind.chats_added": "➕ Work chats added",  # 007
    "history.more": "+{n} more",
    "history.profile_checked": "🔄 Profiles checked (nothing new to learn)",
    "history.profile_removed": "🗑 Contact profile removed",
    "history.profiles_rebuilt": "🔄 Profiles rebuilt",
    # --- /tasks ---
    "tasks.title": "🗂 Task source",
    "tasks.not_connected_status": "Not connected. Drafts use your messages and commitments only.",
    "tasks.status_error": (
        "⚠️ {name}: error — it stopped answering or the token was revoked; drafts are "
        "prepared without task context. Reconnect below."
    ),
    "tasks.status_connected": "✅ {name}: connected (read-only)",
    "tasks.boards_selected": {"one": "📋 Boards: {n} selected", "other": "📋 Boards: {n} selected"},
    "tasks.boards_none": "📋 Boards: none selected yet",
    "tasks.key_prompt": (
        "🔌 Connecting Trello (read-only).\n"
        "1/2 Send your Trello API key: open https://trello.com/power-ups/admin, create or open a "
        "Power-Up and copy its API key. I delete the message right away. /cancel to stop."
    ),
    "tasks.token_prompt": (
        "2/2 Now open this link, allow read access and send me the token it shows:\n"
        "https://trello.com/1/authorize?expiration=never&scope=read&response_type=token"
        "&name=Scribe&key={key}\n"
        "I delete the message right away. /cancel to stop."
    ),
    "tasks.invalid_input": "⚠️ That doesn't look like a Trello key or token. Try again or /cancel.",
    "tasks.rejected": (
        "⚠️ Trello did not accept this key and token, or did not answer. Nothing was saved. "
        "Check them and try /tasks → Connect again."
    ),
    "tasks.unavailable": "⚠️ Trello is not answering right now. Try again later.",
    "tasks.not_connected": "Trello is not connected. /tasks to connect it.",
    "tasks.disconnected": "❌ Trello disconnected. Drafts no longer use your tasks.",
    "tasks.pick": "📋 Pick the boards I may read (✅ = used for drafts):",
    "tasks.connect_abandoned": (
        "↩️ Trello connection cancelled, and the key you sent is forgotten. Don't paste "
        "the key or token now: start again with /tasks → Connect."
    ),
    "tasks.expired": (
        "⌛ That took too long, so I forgot the key. Start again with /tasks → Connect."
    ),
    "tasks.no_boards": "📋 Your Trello account has no open boards.",
    "tasks.connected": "✅ Trello connected (read-only).",
    "alert.tasks_disconnected": "Disconnected",
    "alert.unknown_board": "Unknown board",
    "btn.tasks_connect": "🔌 Connect Trello",
    "btn.tasks_boards": "📋 Boards",
    "btn.tasks_reconnect": "🔁 Reconnect",
    "btn.tasks_disconnect": "❌ Disconnect",
    # --- /commitments ---
    "date.weekday_day_month": "{weekday} {day} {month}",
    "commitments.none": (
        "🤝 No open commitments. I note them from what you promise in your work chats; "
        "they stay here until you resolve or delete them."
    ),
    "commitments.header": {
        "one": "🤝 Open commitments ({n}):",
        "other": "🤝 Open commitments ({n}):",
    },
    "commitments.item": "🤝 {what} · {contact} · {due}",
    "commitments.someone": "someone",
    "commitments.due": "due {day}",
    "commitments.no_date": "no date",
    "commitments.not_open": "This commitment is no longer open",
    "commitments.edit_prompt": (
        "✏️ Send the new text, a new date (e.g. 22.10, 2026-10-22 or Thursday), both as "
        "“text; date”, or “no date” to remove the date. /cancel to keep it."
    ),
    "commitments.resolved": "✅ Resolved: {what} · {contact}",
    "commitments.deleted": "🗑 Deleted: {what} · {contact}",
    "alert.resolved": "Resolved",
    "alert.deleted": "Deleted",
    "err.commitment_empty": "Send some text or a date.",
    "err.commitment_date": "I couldn't read the date. Example: send it; 22.10",
    "btn.commitment_resolve": "✅ Resolve",
    "btn.commitment_delete": "🗑 Delete",
    # --- /profile ---
    "profile.none": (
        "🧑 No style profile yet. It is built at the end of setup (/start), or tap 🔄 Rebuild."
    ),
    "profile.not_found": "Profile not found",
    "profile.weak": (
        "⚠️ Built from fewer than {n} of your messages, so style matching is weak: drafts use a "
        "neutral, polite default until more of your messages are available."
    ),
    "profile.neutral_summary": (
        "There are not enough of your own messages yet to learn your style, so drafts use a "
        "neutral, polite and concise default until more of your messages are available."
    ),
    "profile.rules_prompt": (
        "Send a rule to add it (e.g. never use emojis), “-2” to remove rule 2, or /done to finish."
    ),
    "profile.rebuild_confirm": (
        "🔄 Rebuild re-reads your own messages of the last 6 months in your enabled work chats and "
        "learns your profiles again (rules you set are kept). Nothing of other people is read."
    ),
    "profile.rebuilding": "⏳ Reading your own messages and rebuilding your profiles…",
    "profile.rebuild_failed": "⚠️ I couldn't rebuild your profiles right now. Nothing was changed.",
    "profile.rebuild_busy": "⏳ Already rebuilding your profiles; wait for it to finish.",
    "profile.rebuilt": "✅ Rebuilt: global profile from {n} of your messages, {contacts}.",
    "profile.rebuilt_contacts": {"one": "{n} contact profile", "other": "{n} contact profiles"},
    "profile.rebuilt_global_failed": (
        "⚠️ I couldn't rebuild your global profile right now, so it stays as it was. "
        "Rebuilt: {contacts}."
    ),
    "profile.rebuild_contacts_failed": {
        "one": "⚠️ {n} contact profile couldn't be rebuilt and stays as it was.",
        "other": "⚠️ {n} contact profiles couldn't be rebuilt and stay as they were.",
    },
    "profile.contact": "contact",
    "profile.group_name": "{name} (group)",
    "profile.rules_none": "Rules you set: none yet",
    "profile.rules": "Rules you set:",
    "profile.rules_global": "Global rules (apply here too; edit them in your global profile):",
    "profile.rules_global_more": {
        "one": "…and {n} more global rule: see your global profile.",
        "other": "…and {n} more global rules: see your global profile.",
    },
    "profile.rules_more": {
        "one": "…and {n} more rule of yours.",
        "other": "…and {n} more rules of yours.",
    },
    "profile.scope_global": "global",
    "profile.title_global": {
        "one": "🧑 Your style ({scope}, v{version}, built from {n} message)",
        "other": "🧑 Your style ({scope}, v{version}, built from {n} messages)",
    },
    "profile.title_contact": {
        "one": "👤 Your style with {owner} (v{version}, {n} message)",
        "other": "👤 Your style with {owner} (v{version}, {n} messages)",
    },
    "profile.contacts_none": (
        "👥 Contact profiles ({total}): none yet. Your most active contacts get one "
        "automatically; the others use your global profile."
    ),
    "profile.contacts": "👥 Contact profiles ({total}). 📌 pinned ones are always kept:",
    "profile.removed": (
        "🗑 Removed the profile for {name}: drafts to them use your global profile. It comes "
        "back only if your messages to them from now on (not the earlier ones, also on 🔄 "
        "Rebuild) make them one of your most active contacts again."
    ),
    "profile.your_global": "your global profile",
    "profile.rules_for": "✏️ Rules for {whose}",
    "profile.rule_invalid": "⚠️ {problem} {prompt}",
    "profile.rules_done": "✅ Rules saved. They apply to the next drafts. /profile to see them.",
    "profile.restored": (
        "↩️ Restored your previous profiles ({n} restored). Rules you set are unchanged."
    ),
    "profile.rule_added_global": (
        '✅ Added rule "{rule}" to your global profile. /profile shows it.'
    ),
    "profile.rule_added_contact": (
        '✅ Added rule "{rule}" to {owner}\'s profile. /profile shows it.'
    ),
    "profile.rule_already_global": (
        '✅ "{rule}" is already one of your global rules, so it applies to {owner} too. '
        "Nothing was added."
    ),
    "profile.rule_typed_already_global": (
        '"{rule}" is already one of your global rules, so it applies to {owner} too. '
        "Nothing was added."
    ),
    "profile.proposal_dismissed": "🚫 Dismissed. Your profile is unchanged.",
    "alert.global_always_kept": "The global profile is always kept",
    "alert.pin_limit": {
        "one": "At most {n} contact can be pinned",
        "other": "At most {n} contacts can be pinned",
    },
    "alert.pinned": "📌 Pinned: always kept",
    "alert.unpinned": "Unpinned",
    "alert.global_not_removable": "The global profile cannot be removed",
    "alert.removed": "Removed",
    "alert.nothing_to_undo": "Nothing to undo",
    "alert.undo_refused": (
        "Nothing to undo: only the latest refresh can be undone, once, and not after a "
        "rebuild or a later change of those profiles"
    ),
    "alert.restored": "↩️ Restored",
    "alert.proposal_closed": "This suggestion is no longer open",
    "alert.rule_added": "✅ Rule added",
    "alert.dismissed": "Dismissed",
    "btn.edit_rules": "✏️ Edit rules",
    "btn.contact_profiles": "👥 Contact profiles ({count}/{max})",
    "btn.undo_refresh": "↩️ Undo last refresh",
    "btn.rebuild": "🔄 Rebuild",
    "btn.rebuild_now": "🔄 Rebuild now",
    "btn.pin": "📌 Pin",
    "btn.unpin": "📌 Unpin",
    "btn.undo": "↩️ Undo",
    "btn.add_rule": "✅ Add rule",
    "btn.dismiss": "🚫 Dismiss",
    "err.rule_empty": "Send the rule as text, e.g. never use emojis.",
    "err.rule_too_long": "Keep a rule under {n} characters.",
    "err.rule_limit": "A profile can have at most {n} rules; remove one first.",
    "err.rule_missing": "There is no rule {number}.",
    # --- style rule proposals ---
    "proposal.this_contact": "this contact",
    "proposal.text_global": (
        "💡 I noticed you {noticed} in {count} of your last {total} edits with your global "
        'profile.\nAdd rule "{rule}" to your global profile?'
    ),
    "proposal.text_contact": (
        "💡 I noticed you {noticed} in {count} of your last {total} edits for {owner}.\n"
        'Add rule "{rule}" to {owner}\'s profile?'
    ),
    "proposal.noticed.emoji_removed": "removed emojis",
    "proposal.noticed.emoji_added": "added emojis",
    "proposal.noticed.greeting_removed": "removed the greeting",
    "proposal.noticed.sign_off_removed": "removed the sign-off",
    "proposal.noticed.shorter": "made the reply much shorter",
    "proposal.noticed.longer": "made the reply much longer",
    "proposal.noticed.more_formal": "made the reply more formal",
    "proposal.noticed.more_casual": "made the reply more casual",
    "proposal.noticed.language_en": "switched the reply to English",
    "proposal.noticed.language_ru": "switched the reply to Russian",
    "proposal.rule.emoji_removed": "no emojis",
    "proposal.rule.emoji_added": "use emojis",
    "proposal.rule.greeting_removed": "no greeting",
    "proposal.rule.sign_off_removed": "no sign-off",
    "proposal.rule.shorter": "keep replies short",
    "proposal.rule.longer": "write longer replies",
    "proposal.rule.more_formal": "more formal tone",
    "proposal.rule.more_casual": "more casual tone",
    "proposal.rule.language_en": "reply in English",
    "proposal.rule.language_ru": "reply in Russian",
    # --- weekly profile refresh note ---
    "refresh.where.overall": "overall",
    "refresh.where.group": 'with "{name}" group',
    "refresh.where.a_group": 'with "group" group',
    "refresh.where.contact": "with {name}",
    "refresh.where.a_contact": "with a contact",
    "refresh.change.new_profile": "new profile {where}",
    "refresh.change.more_formal": "more formal {where}",
    "refresh.change.more_casual": "more casual {where}",
    "refresh.change.shorter": "shorter replies {where}",
    "refresh.change.longer": "longer replies {where}",
    "refresh.change.more_emojis": "more emojis {where}",
    "refresh.change.fewer_emojis": "fewer emojis {where}",
    "refresh.change.language": "mostly {language} {where}",
    "refresh.scope_global": "global",
    "refresh.scope_contacts": {"one": "{n} contact", "other": "{n} contacts"},
    "refresh.head": "🔄 Profiles refreshed ({scope}).",
    "refresh.head_plain": "🔄 Profiles updated.",
    "refresh.no_changes": "no notable changes",
    "refresh.changes": "{head} Changes: {changes}.",
    "refresh.retired": {
        "one": (
            " Retired {n} contact profile of contacts you write to less now (they use your "
            "global profile)."
        ),
        "other": (
            " Retired {n} contact profiles of contacts you write to less now (they use your "
            "global profile)."
        ),
    },
    "refresh.rules_unchanged": " Your own rules are unchanged.",
    # --- /my_data, /delete_my_data ---
    "date.day_month_year": "{day} {month} {year}",
    "my_data.title": "📦 What I store about you",
    "my_data.oldest": "oldest {day}",
    "my_data.next_removal": "next removal {day}",
    "my_data.messages": "💬 Messages: {n} ({details})",
    "my_data.messages_rule": "each is deleted {n} days after it was sent",
    "my_data.drafts": "✍️ Drafts: {n} ({details})",
    "my_data.drafts_rule": "the text is deleted 30 days after your decision; the record stays",
    "my_data.profiles": "🧑 Style profiles: {n} ({details})",
    "my_data.profiles_rule": (
        "{current} current, {previous} previous, {retired} retired; retired and previous "
        "versions are deleted 30 days after they were replaced, except the one ↩️ Undo of your "
        "last refresh would restore"
    ),
    "my_data.proposals": "💡 Rule suggestions: {n}",
    "my_data.commitments": "🤝 Commitments: {open} open, {closed} closed",
    "my_data.commitments_removal": (
        " (closed ones deleted 30 days after closing; next removal {day})"
    ),
    "my_data.activity": "🧾 Activity records: {n} ({details})",
    "my_data.activity_rule": "kept until you delete your data",
    "my_data.chats": "👥 Contacts: {contacts} · 💼 Work chats: {work_chats}",
    "my_data.connections": "🔌 Telegram session: {telegram} · 🗂 Task source: {tasks}",
    "my_data.not_connected": "not connected",
    "my_data.footer": (
        "📦 Export sends all of it as a JSON file. /delete_my_data deletes everything."
    ),
    "my_data.export_caption": "📦 Everything I store about you (only your data).",
    "alert.export_preparing": "Preparing your export…",
    "btn.export": "📦 Export",
    "delete.ask": (
        "⚠️ This deletes everything I store about you: messages, drafts, style profiles, "
        "commitments, settings, the task-source connection and the activity log, and logs out the "
        "Telegram session I use. It cannot be undone.\n\n"
        "Type {word} to confirm, or /cancel."
    ),
    "delete.cancelled": "Not deleted. Nothing was changed.",
    "delete.done": (
        "🗑 Done. All your data was deleted and the Telegram session was logged out. "
        "Send /start if you want to set Scribe up again."
    ),
    "delete.failed": (
        "⚠️ The Telegram session I used is logged out already, but I couldn't delete your data "
        "just now. Nothing else was changed and Scribe no longer reads your chats. Send "
        "/delete_my_data again in a minute to delete the rest."
    ),
    "delete.logout_failed": (
        "⚠️ I couldn't reach Telegram to end the session, so nothing was deleted yet. "
        "Try /delete_my_data again in a minute."
    ),
    # --- draft card and Sources: tasks and commitments (US3) ---
    "review.tasks_error_alert": (
        "⚠️ Trello is not answering or the token was revoked, so drafts are prepared without task "
        "context. Reconnect it in /tasks."
    ),
    "review.due_suffix": " (due {day})",
    "review.source_task": "{provider} “{title}”{due}",
    "review.source_promise": "your promise “{what}”{due}",
    "review.source_said": " · said {day} “{quote}”",
    "review.source_task_unavailable": "a task (details unavailable)",
    "review.source_conflict": "⚠️ Conflict with “{what}”: {why}",
    "card.warning.tasks_partial": "⚠️ some task boards could not be read",
    "card.warning.out_of_office": "⚠️ out-of-office reply (/ooo to turn it off)",
    # --- injection checks (issue #23): hard stops, never sent without confirmation ---
    "card.warning.suspected_injection": (
        "🛑 the message reads like instructions to the assistant (possible prompt injection):"
        " check the draft, it is never sent automatically"
    ),
    "card.warning.action_claim": (
        "🛑 the draft claims an action a reply cannot do (sent, approved, settings changed):"
        " check it before sending"
    ),
    "review.reason.suspected_injection": "a possible prompt injection in the message",
    "review.reason.action_claim": "a claim of an action that was not taken",
    # --- task deadlines from comments / the description (issue #6) ---
    "review.due_suffix_noted": " (due {day}, {note})",
    "review.deadline_description": "from the description",
    "review.deadline_comment": "from a comment{by}",
    "review.deadline_by": " by {author}",
    "review.deadline_when": ", {day}",
    "review.deadline_comment_quote": "from a comment{by}{when}: “{quote}”",
    "review.deadline_description_quote": "from the card description: “{quote}”",
    # --- access gate: control-bot texts (specs/005, AccessGateMiddleware, /delete_my_data) ---
    "access.refused": (
        "Scribe is in closed beta right now. You can apply to join the beta, or learn about "
        "the subscription."
    ),
    "access.subscription_line": "Or arrange a subscription: {subscription_contact}.",
    "access.ended_reply": (
        "Your access to Scribe has ended, so I'm not reading your chats or preparing replies. "
        "Your data is kept. You can still view it with /my_data or delete it with "
        "/delete_my_data."
    ),
    "access.delete_warning": (
        "Deleting your data also removes {what}. To use Scribe again, you'll need to ask for "
        "access again."
    ),
    "access.delete_what_beta": "your beta access",
    "access.delete_what_subscription": "your subscription until {date}",
    "access.delete_what_both": "your beta access, and your subscription until {date}",
    # --- access gate: agent-side notices (specs/005, access_reconcile) ---
    "access.ended_notice": (
        "Your access to Scribe has ended ({reason_text}). I've stopped reading your chats. "
        "Replies waiting for your approval were not sent. Your data is kept: /my_data, "
        "/delete_my_data. To get access back, send /start."
    ),
    "access.reason_beta_removed": "beta access removed",
    "access.reason_subscription_ended": "subscription ended",
    "access.restored_notice": (
        "Your access to Scribe is back. I'm working again with your previous settings."
    ),
    "access.reconnect_hint": "Your Telegram connection has expired; reconnect with /telegram",
    # --- access gate: subscription reminder (specs/005 US4, access_reconcile) ---
    "access.sub_reminder": "Your Scribe subscription ends on {date}. To renew: {renew}",
    "access.renew_send_start": "send /start",
    # --- access gate: beta applications in the bot (specs/005 US5, scribe.access.apply) ---
    "btn.apply_join": "🙋 Join the beta",
    "btn.apply_subscription": "💳 Subscription",
    "btn.apply_send": "📨 Send application",
    "btn.application_approve": "✅ Approve",
    "access.apply_pending": (
        "Your application to join the beta is being reviewed. I'll write to you as soon as "
        "there is a decision."
    ),
    "access.apply_rejected_recently": "The beta is full for now. You can apply again after {date}.",
    "access.apply_has_access": "You already have access to Scribe. Send /start to begin.",
    "access.apply_ask": (
        "How do you plan to use Scribe? A sentence or two is enough: which chats, what kind of "
        "replies."
    ),
    "access.apply_too_short": "That's a bit short. Please write at least {n} characters.",
    "access.apply_too_long": "That's too long. Please keep it under {n} characters.",
    "access.apply_text_only": "Please describe it in a text message.",
    "access.apply_review": "Your application:\n\n{use_case}",
    "access.apply_consent": (
        "By pressing «Send application» you agree to the processing of your personal data."
    ),
    "access.apply_cancelled": "Application cancelled. Nothing was saved.",
    "access.apply_sent": (
        "Thanks! Your application has been sent. I'll write to you when there is a decision."
    ),
    "access.subscription_placeholder": (
        "Subscriptions are coming soon. Prices aren't set yet. Meanwhile you can apply to join "
        "the beta."
    ),
    "access.apply_approved": "You're in the Scribe beta! Send /start to set up.",
    "access.apply_rejected": (
        "Thank you for your interest! The beta is full for now. You can apply again after {date}."
    ),
    # To the operator, as plain text: the use case is never interpreted as markup.
    "access.apply_operator": (
        "New beta application\n{who} · id {telegram_id} · {language}\nUse case:\n{use_case}"
    ),
    "access.apply_operator_no_username": "no username",
    "access.apply_operator_approved": "✅ Approved by {by}, {when}",
    "access.apply_operator_rejected": "❌ Rejected by {by}, {when}",
    "access.apply_operator_by_cli": "CLI",
    # --- access gate: the /my_data Access section (specs/005 T032) ---
    "access.mydata_title": "🔑 Access: {status}",
    "access.mydata_status_operator": "operator",
    "access.mydata_status_beta": "beta",
    "access.mydata_status_subscribed": "subscription",
    "access.mydata_status_beta_and_subscribed": "beta and subscription",
    "access.mydata_status_none": "no access",
    "access.mydata_beta_since": "Beta since {day}",
    "access.mydata_subscription": "Subscription {start} – {end}: {state}",
    "access.mydata_sub_active": "active",
    "access.mydata_sub_ended": "ended",
    "access.mydata_sub_cancelled": "cancelled",
    "access.mydata_sub_scheduled": "scheduled",
    "access.mydata_application": "Beta application of {day}: {status}",
    "access.mydata_app_pending": "under review",
    "access.mydata_app_approved": "approved on {day}",
    "access.mydata_app_rejected": "declined on {day}",
    "access.mydata_events": (
        "Access history records: {n} (each is deleted 365 days after it was written)"
    ),
    # --- Jira (specs/003-jira-commitments) ---------------------------------------------------
    "btn.jira_connect": "🔌 Connect Jira",
    "btn.jira_projects": "📋 Projects",
    "btn.jira_cloud": "☁️ Jira Cloud",
    "btn.jira_self_hosted": "🏢 Self-hosted",
    "btn.jira_trust_cert": "Trust this certificate",
    "btn.replace_with_jira": "Replace with Jira",
    "btn.replace_with_trello": "Replace with Trello",
    "btn.commitment_reopen": "↩️ Reopen",
    "alert.no_projects": "Pick at least one project",
    "alert.too_many_projects": "Up to 20 projects",
    "alert.not_found": "Not found",
    "alert.reopened": "Reopened",
    "alert.jira_cert_expired": "This certificate card has expired. Start again with /tasks.",
    "tasks.jira_name_cloud": "Jira (Cloud · {host})",
    "tasks.jira_name_self_hosted": "Jira (self-hosted · {host}{version})",
    "tasks.projects_selected": "📋 Projects: {keys}",
    "tasks.projects_none": "📋 Projects: none selected yet",
    "tasks.pick_projects": "📋 Pick the Jira projects I may read (✅ = used for drafts, up to 20):",
    "tasks.no_projects": "📋 You can't see any Jira project.",
    "tasks.replace_jira": (
        "You have Jira connected. Connecting Trello replaces it: drafts will use Trello cards "
        "instead of Jira issues. Continue?"
    ),
    "tasks.jira_disconnected": (
        "❌ Jira disconnected. Drafts no longer use your issues; commitments found in your Jira "
        "comments stay in /commitments."
    ),
    "jira.deployment_prompt": "🔌 Connecting Jira (read-only). Where does your Jira run?",
    "jira.site_prompt": "1/3 Send your Jira address, like {example}. /cancel to stop.",
    "jira.email_prompt": "2/3 Send the email address of your Atlassian account.",
    "jira.token_prompt": (
        "3/3 Create a scoped API token at "
        "https://id.atlassian.com/manage-profile/security/api-tokens → “Create API token with "
        "scopes” → Jira → scopes read:jira-work and read:jira-user, and send it to me. I delete "
        "the message right away. /cancel to stop."
    ),
    "jira.pat_prompt": (
        "2/2 Create a personal access token in Jira (your avatar → Profile → Personal Access "
        "Tokens) and send it to me. Jira gives such tokens all your permissions; Scribe only ever "
        "reads. You can revoke it there at any time. I delete the message right away. "
        "/cancel to stop."
    ),
    "jira.replace_trello": (
        "You have Trello connected. Connecting Jira replaces it: drafts will use Jira issues "
        "instead of Trello cards. Continue?"
    ),
    "jira.connected": (
        "✅ Jira connected (read-only).\nℹ️ I read your open issues in the projects you pick and "
        "your own comments on issues you are assigned to, reported or watch (keep Jira's "
        "automatic watching on, so issues you comment on count). I never change anything in Jira."
    ),
    "jira.connect_abandoned": (
        "↩️ Jira connection cancelled, and what you sent for it is forgotten. Don't paste "
        "the token now: start again with /tasks."
    ),
    "jira.expired": "⌛ That took too long, so I forgot the token. Start again with /tasks.",
    "jira.invalid_email": "⚠️ That doesn't look like an email address. Try again or /cancel.",
    "jira.invalid_token": "⚠️ That doesn't look like a token. Try again or /cancel.",
    "jira.address_invalid": (
        "⚠️ That doesn't look like a Jira address. Send it like https://jira.example.com"
    ),
    "jira.unreachable": (
        "⚠️ Scribe couldn't reach this server. Check the address: the server must answer over "
        "HTTPS from the internet (a server in a private network must be allowed by the Scribe "
        "operator). Nothing was saved."
    ),
    "jira.port_not_allowed": (
        "⚠️ Scribe connects to Jira only on these ports: {ports}. Send the address with one of "
        "them (no port means 443) or ask the Scribe operator. Nothing was saved."
    ),
    "jira.too_many_attempts": (
        "⏳ Too many connection attempts. Try again in {minutes} min (/tasks). Nothing was saved."
    ),
    "jira.version": (
        "⚠️ Jira {found} is not supported. Supported: {minimum} and newer. Nothing was saved."
    ),
    "jira.auth": "⚠️ Jira rejected the token. Nothing was saved.{hint}",
    "jira.classic_token": (
        " This looks like a classic token: please create a scoped token with read:jira-work and "
        "read:jira-user."
    ),
    "jira.unavailable": "⚠️ Jira is not answering right now. Try again later. Nothing was saved.",
    "jira.cert_card": (
        "🔐 This server uses a certificate your company issued itself.\n"
        "Subject: {subject}\nIssuer: {issuer}\nValid until: {not_after}\nSHA-256: {sha256}\n"
        "Only trust it if this matches what your IT team expects."
    ),
    "jira.cert_changed": (
        "⚠️ The certificate changed since you trusted it. Review the new one:\n{card}"
    ),
    "jira.first_scan": {
        "one": "🔎 Found {n} commitment in your Jira comments. /commitments to review.{capped}",
        "other": "🔎 Found {n} commitments in your Jira comments. /commitments to review.{capped}",
    },
    "jira.first_scan_capped": {
        "one": " Only the most recently updated issue was scanned.",
        "other": " Only the {n} most recently updated issues were scanned.",
    },
    "commitments.source_telegram": "Telegram",
    "commitments.source_jira": "Jira {key} comment",
    "commitments.source_about": "about {key}",
    "commitments.restricted": "(restricted)",
    "commitments.unreachable": "⚠️ {key} is no longer reachable",
    "commitments.recently_closed": "Recently closed by Scribe:",
    "commitments.closed_item": "🗂 {what} · {contact} · {due}",
    "commitments.closed_by_scribe": "Closed by Scribe: {note}",
    "commitments.note_issue_done": "issue done",
    "commitments.note_source_removed": "source comment removed",
    "commitments.note_source_changed": "source comment changed",
    "commitments.note_source_unreachable": "issue no longer reachable",
    "commitments.reopened": "↩️ Reopened.\n\n{item}",
    "card.dates_differ": "📅 Dates differ: {issue}{dates}",
    "card.date_part": "{day} ({source})",
    "card.date_source_at": "{source}, {day}",
    "card.date_source.due": "due date",
    "card.date_source.comment": "a comment",
    "card.date_source.description": "the description",
    "card.date_source.promise_jira": "your Jira comment",
    "card.date_source.promise_telegram": "your Telegram message",
    # --- skipping messages that need no reply (specs/004-skip-closing-messages) ---
    "cmd.skipped": "Messages I didn't draft for (thanks, bye…)",
    "card.warning.drafted_by_habit": "✍️ Drafted: you usually reply to messages like this here",
    "card.warning.drafted_on_request": "✍️ You asked for this draft",
    "card.possibly_unnecessary": "💤 {sender}: this may no longer be needed",
    "skipped.on": "ON",
    "skipped.off": "OFF",
    "skipped.header": {
        "one": "💤 Skipped in the last {n} day — {total}   ·   Skipping thanks/ok/bye: {state}",
        "other": "💤 Skipped in the last {n} days — {total}   ·   Skipping thanks/ok/bye: {state}",
    },
    "skipped.empty": {
        "one": "Nothing skipped in the last {n} day. Skipping thanks/ok/bye is {state}.",
        "other": "Nothing skipped in the last {n} days. Skipping thanks/ok/bye is {state}.",
    },
    "skipped.forwarded_note": (
        "Forwarded messages without the contact's own text are always skipped."
    ),
    "skipped.reason": "Reason: {reason}",
    "skipped.reason.thanks": "thanks",
    "skipped.reason.farewell": "goodbye",
    "skipped.reason.acknowledgment": "acknowledgment",
    "skipped.reason.forwarded": "forwarded message",
    "skipped.drafting": "✍️ Drafting…",
    "skipped.replied": "— you already replied",
    "skipchats.title": "⚙️ Skip thanks/bye per chat",
    "skipchats.global_state": "Global: {state}",
    "skipchats.none": "No enabled work chats.",
    "skipchats.choice.global": "Global ({state})",
    "skipchats.choice.off": "Always draft",
    "skipchats.choice.on": "Skip",
    "settings.chat_skip": "💤 Skip thanks/bye: {value}",
    "btn.skip_turn_off": "Turn skipping off",
    "btn.skip_turn_on": "Turn skipping on",
    "btn.skip_per_chat": "⚙️ Per-chat settings",
    "btn.draft_anyway": "{number} · ✍️ Draft anyway",
    "btn.skip_always_draft_chat": "{number} · Always draft in this chat",
    "btn.skip_use_global_chat": "{number} · Use global setting",
    "btn.skip_back": "‹ Back to skipped",
    "alert.already_handled": "Already handled",
    "alert.drafting": "Drafting…",
    "alert.skipping": "Skipping thanks/ok/bye is {state}",
    "history.kind.skipped": "💤 Skipped",
    "history.skipped": "💤 Skipped — {reason}",
    "history.kind.draft_anyway": "✍️ Drafted on request",
    # specs/004: search over earlier conversations
    "card.warning.earlier_search_unavailable": "⚠️ drafted without searching earlier conversations",
    "card.warning.earlier_search_no_match": "🔎 nothing found in earlier conversations",
    "card.warning.earlier_search_off": (
        "🔎 earlier conversations not searched (turned off in /settings)"
    ),
    "card.source_earlier": "💬 {chat} · {day}",
    "review.source_earlier": "💬 {chat} · {sender} · {day} {time}",
    "review.source_earlier_missing": "💬 earlier message",
    "settings.search_earlier": "🔎 Search earlier conversations: {state}",
    "settings.search_earlier_state.on": "on",
    "settings.search_earlier_state.off": "off",
    "btn.search_earlier_on": "🔎 Search earlier: turn on",
    "btn.search_earlier_off": "🔎 Search earlier: turn off",
    # --- work account mode (specs/008) ---
    "setup.pick_prompt_work_account": (
        "💼 Step 2 — work account mode is on: every private chat with a person is a work chat, "
        "including people who write to you for the first time later. Bots, Saved Messages and "
        "groups are not included. Tap a private chat to exclude it (🚫). Groups: tick the ones "
        "you want. Then press Done."
    ),
    "btn.work_account_switch_on": "💼 Work account mode: off (tap to turn on)",
    "btn.work_account_switch_off": "💼 Work account mode: on (tap to turn off)",
    "btn.pick_done_work_account": (
        "Done (all private chats; excluded: {excluded}; groups: {groups})"
    ),
    "setup.chats_saved_work_account": (
        "💼 Work account mode is on: every private chat is a work chat. Excluded: {excluded}. "
        "Groups: {groups}."
    ),
    "settings.work_account.on": (
        "💼 Work account: on — every private chat is a work chat, excluded: {n}"
    ),
    "settings.work_account.off": "💼 Work account: off — only the chats you chose",
    "settings.work_account.on_plain": "💼 Work account: on — every private chat is a work chat",
    "btn.work_account_on": "💼 Work account: turn on",
    "btn.work_account_off": "💼 Work account: turn off",
    "settings.work_account_confirm_on": (
        "Turn on work account mode?\n"
        "\n"
        "• New messages from every private chat with a person will be read and stored as work "
        "messages, including people who write to you for the first time.\n"
        "• Groups, channels and bots are not included. Add groups in /chats.\n"
        "• You can exclude any chat in /chats, and turn this off any time.\n"
        "• Old messages are not drafted. Your own recent messages in up to {n} recent chats are "
        "used to learn your style at the next profile rebuild."
    ),
    "settings.work_account_confirm_off": (
        "Turn off work account mode?\n"
        "\n"
        "Only the chats you chose yourself will be handled. Chats picked up by this mode will be "
        "paused: {chats}. Pending drafts in them that will be dropped: {drafts}; waiting "
        "messages: {waiting}. Your exclusions are kept if you turn it on again."
    ),
    "btn.wacct_on": "✅ Turn on",
    "btn.wacct_off": "⏸ Turn off",
    "btn.wacct_cancel": "Cancel",
    "settings.work_account_now_on": "💼 Work account mode is on.",
    "settings.work_account_now_off": "💼 Work account mode is off. Chats paused: {n}.",
    "settings.work_account_cancelled": "Nothing was changed.",
    "status.work_account_off": "⏸ Work account mode off — dropped",
    "status.chat_excluded": "🚫 Chat excluded — dropped",
    "btn.chat_exclude": "🚫 Exclude",
    "btn.chat_include": "↩️ Include again",
    "settings.chat_origin.auto": "💼 Included by work account mode",
    "settings.chat_origin.picked": "💼 Chosen by you",
    "settings.chat_origin.excluded": "🚫 Excluded: nothing is read or stored from this chat",
    "settings.chat_origin.paused": "⏸ Paused: work account mode is off",
    "chats.header_work_account": (
        "💼 Work account: on — every private chat is a work chat. Excluded: {n}."
    ),
    "chats.paused_footer": (
        "💼 Chats picked up by work account mode, paused while it is off: {n}. Turn it on in "
        "/settings."
    ),
    "btn.exclude_chats": "🚫 Exclude chats…",
    "chats.exclude_prompt": (
        "🚫 Tap a chat to exclude it (🚫) or include it again (✅). Excluded chats are never "
        "read. Then press Done."
    ),
    "chats.exclude_none": "💼 There are no private chats to exclude.",
    "chats.exclude_mode_off": (
        "💼 Work account mode is off, so there is nothing to exclude. Turn it on in /settings."
    ),
    "btn.exclude_done": "Done",
    "chats.exclude_done": "✅ Excluded: {excluded}. Included again: {included}.",
    "chats.exclude_cancelled": "Nothing was changed.",
    "chats.exclude_hint": "Tap chats to exclude or include them, then Done. /cancel to stop.",
    "chats.add_prompt_groups": (
        "➕ Choose groups to add. Work account mode already covers every private chat. I'll "
        "read only new messages in the groups, from now on."
    ),
    "chats.add_none_left_groups": (
        "💼 All your Telegram groups are already in /chats, and work account mode covers every "
        "private chat."
    ),
    "history.kind.work_account_on": "💼 Work account mode on",
    "history.kind.work_account_off": "💼 Work account mode off",
    "history.kind.chat_excluded": "🚫 Chat excluded",
    "history.kind.chat_included": "↩️ Chat included again",
    "history.chat_picked_up": "➕ New work chat (work account)",
    "my_data.work_account_on": (
        "💼 Work account mode: on (since {day}) · picked-up chats: {auto} · excluded: {excluded}"
    ),
    "my_data.work_account_off": (
        "💼 Work account mode: off · picked-up chats: {auto} · excluded: {excluded}"
    ),
    # specs/009: mark chats unread after the agent reads or answers there.
    "settings.mark_unread": "🔵 Mark chats unread after the agent: {state}",
    "settings.mark_unread_state.on": "on",
    "settings.mark_unread_state.off": "off",
    "btn.mark_unread_on": "🔵 Unread marks: turn on",
    "btn.mark_unread_off": "🔵 Unread marks: turn off",
    # specs/011: release notes after an update
    "release_notes.heading": "What's new in Scribe {version}",
    "my_data.release_notes": "📣 Release notes received: {n}",
    # Issue #46: the user's grammatical gender in drafts.
    "settings.gender": "👤 Gender in drafts: {value}",
    "settings.gender_value.masculine": "masculine («я сделал»)",
    "settings.gender_value.feminine": "feminine («я сделала»)",
    "settings.gender_value.unset": "not set (drafts follow your messages)",
    "settings.gender_prompt": (
        "👤 In languages with grammatical gender (Russian, Spanish, French…) words about you "
        "change with it. Which form should drafts use for you?"
    ),
    "btn.gender": "👤 Gender in drafts",
    "btn.gender.masculine": "Masculine: «я сделал»",
    "btn.gender.feminine": "Feminine: «я сделала»",
    "btn.gender.suggested": "{label} · suggested",
    "gender.question": (
        "👤 One question about drafts. In languages with grammatical gender (Russian, Spanish, "
        "French…) words about you change with it: «я сделал» or «я сделала». Which form should "
        "drafts use?\n"
        "Until you choose, drafts follow your own messages. You can change it any time in "
        "/settings."
    ),
    "gender.suggestion.masculine": "Your messages suggest the masculine form.",
    "gender.suggestion.feminine": "Your messages suggest the feminine form.",
    "gender.saved.masculine": "✅ Saved: drafts will use the masculine form («я сделал», «готов»).",
    "gender.saved.feminine": "✅ Saved: drafts will use the feminine form («я сделала», «готова»).",
    # --- daily LLM budget per user (issue #74) ---
    "skipped.reason.budget": "daily AI limit reached",
    "history.kind.budget_paused": "⏸ Daily AI limit reached: drafting paused",
    "budget.paused_notice": (
        "⏸ You've reached today's AI usage limit, so I've paused drafting for the rest of the "
        "day. New messages get no draft today; you'll find them in /skipped.\n"
        "Drafting resumes automatically at {time} your time (midnight UTC)."
    ),
    "budget.paused_alert": (
        "⏸ Today's AI usage limit is reached. Drafting resumes automatically at {time} your time."
    ),
    "spend_alert.header": {
        "one": "📊 LLM spend on {day} (UTC): {n} user, {total} in total.",
        "other": "📊 LLM spend on {day} (UTC): {n} users, {total} in total.",
    },
    "spend_alert.p95": "Above the 95th percentile ({p95}):",
    "spend_alert.p95_skipped": "No percentile: fewer than {min} users spent anything.",
    "spend_alert.soft": "Reached the soft cap ({cap}, drafts on the economy tier):",
    "spend_alert.hard": "Reached the hard cap ({cap}, drafting paused):",
}
