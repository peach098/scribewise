# Search over earlier conversations

Scribe keeps only the last 30 days of messages. When an incoming message refers to something
older ("as we agreed", "the same invoice", "last time") or asks for a fact that isn't in that
window, the draft searches your earlier Telegram conversations with the sender and cites what
it used.

- **What is searched:** only your enabled work chats that involve the sender: the chat the
  message came from, your private chat with the sender, and up to 2 other groups where the
  sender wrote in the last 30 days. Disabled chats and all other chats are never searched. In
  groups, a found message is used only if you or the sender wrote it, it replies to or mentions
  one of you, or it is in the same thread.
- **How:** on demand, with Telegram's own search inside each of those chats (any age of
  history), for at most a few seconds per draft. Nothing found is stored, except the excerpts a
  draft actually cites (up to 200 characters each, encrypted, removed with the draft text 30
  days after your decision, included in `/my_data` and deleted by `/delete_my_data`).
- **On the card:** cited messages appear as **💬 chat · date** sources; **Sources** shows the
  sender, time and excerpt (and a link for supergroups). The card also says when nothing was found (**🔎 nothing found in earlier
  conversations**) or when the search could not run (**⚠️ drafted without searching earlier
  conversations**).
- **Turning it off:** `/settings` → **🔎 Search earlier: turn off** (on by default). While it's
  off, drafts that would have needed older messages say **🔎 earlier conversations not
  searched**.
- **Operator switches:** `SCRIBE_EARLIER_SEARCH_ENABLED=false` turns the search off for
  everyone (no card note). `SCRIBE_EARLIER_SEARCH_TIMEOUT_S` (default 6, 1–9) bounds the
  Telegram search step; search plus ranking stay under 10 seconds.
- **Telegram rate limits:** Telegram limits how often an account may search. When it asks
  Scribe to wait, the draft doesn't wait with it: the search gives up within its time limit,
  that draft is made from the last 30 days only and the card shows **⚠️ drafted without
  searching earlier conversations**. The next drafts search again.
