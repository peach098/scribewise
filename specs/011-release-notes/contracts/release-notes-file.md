# Contract: Release Notes File and Message

## File

- Path: `release-notes/<X.Y.Z>.md`, where `X.Y.Z` is the version from `pyproject.toml`
  (three non-negative integers, no leading `v`, no suffixes).
- Encoding: UTF-8.
- Sections: a line that is exactly `## en` or `## ru` starts a section. The section runs to
  the next section line or the end of the file. Each section may appear once.
- Before the first section only blank lines and HTML comments (`<!-- ... -->`, which may
  span several lines) are allowed. They are a place for notes to reviewers and are never sent.
- Section text is stripped of leading and trailing blank lines and sent **as is, as plain
  text**. Markdown is not rendered: write plain sentences and `-` or `•` bullet lines, with
  no `**bold**`, links in brackets or headings inside a section.
- Each section: at most 3,500 characters after stripping.
- Audience: users. Say what they can do now, in short sentences. No internal details, issue
  numbers, migrations or file names.

Example `release-notes/0.2.0.md`:

```markdown
<!-- Announces specs/005 US5 and specs/011. -->

## en

- You can now apply for the beta right in the bot: press "Join the beta".
- After each update the bot tells you what is new, like this message.

## ru

- Заявку в бету теперь можно отправить прямо в боте: кнопка «Хочу в бету».
- После каждого обновления бот рассказывает, что нового, — как в этом сообщении.
```

### Validation (`release-notes check`, the unit test over the directory)

| Problem | Severity |
|---|---|
| File name is not `X.Y.Z.md` | error |
| Text (not a comment) before the first section | error |
| Unknown section (`## de`) or a duplicate section | error |
| Section longer than 3,500 characters | error |
| Section `en` or `ru` missing or empty | warning: users of that language get the other one |
| Both sections missing or empty | warning: the version is treated as having no notes |
| Version newer than the running version | info in `preview`: it will be announced when that version runs |

## Message to a user

- Sent by the control bot to `users.telegram_control_chat_id`, with no `parse_mode`, with
  `disable_notification=True`, and without buttons.
- Body for a single version, in the user's language `L` (`users.language`):

  ```text
  {release_notes.heading in L, version=X.Y.Z}

  {section L, or the other section if L is empty}
  ```

- Digest: the same blocks for each personal version, oldest first, separated by one blank
  line. Blocks are packed into messages of at most 4,096 characters and split only between
  versions. Messages of one digest are sent at least 1.1 s apart.
