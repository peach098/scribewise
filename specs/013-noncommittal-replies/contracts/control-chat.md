# Contract: Control chat changes

Extends [004 control-chat](../../004-skip-closing-messages/contracts/control-chat.md).

## Skipped list (`/skipped`)

- New reason labels (`render.py` `SKIP_CATEGORIES`, i18n `skipped.reason.*`):
  - `informational` — RU «информационное сообщение», EN "informational message";
  - `user_rule` — RU «по вашему правилу», EN "by your rule".
- Both are setting-dependent (not in `SETTING_INDEPENDENT_SKIPS`): they disappear from new
  turns when skipping is off for the chat. "Draft anyway" works for them as for any skip.

## Rules (`/rules`, `profile_command.py`)

- Help text gains one line (RU/EN): rules can describe what the agent does, not only how it
  writes — e.g. «не обещай сроки, пиши "посмотрю"», «не отвечай на объявления», «всегда готовь
  черновик для Анны».
- No new commands, buttons or settings.
