# Release notes

One file per version, `X.Y.Z.md`, with a `## en` and a `## ru` section. After a deploy of a
new version, the control bot sends each user with access the section in their language.

- File format and validation: [`specs/011-release-notes/contracts/release-notes-file.md`](../specs/011-release-notes/contracts/release-notes-file.md)
- How to write notes, and the release steps: [`docs/release-notes.md`](../docs/release-notes.md)

Files whose name is not `X.Y.Z.md`, like this one, are ignored.
