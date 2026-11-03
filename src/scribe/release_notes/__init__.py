"""Release notes for users after an update (specs/011-release-notes, GitHub issue #20).

- :mod:`.version`: the running version and ``X.Y.Z`` parsing.
- :mod:`.notes`: ``release-notes/<X.Y.Z>.md`` files, validation and the digest messages (pure).
- :mod:`.repo`: announcement and delivery queries (the caller's session, never commits).
- :mod:`.announce`: the startup check, recipients, manual sends and status lines for the CLI.
- :mod:`.sender`: the scheduler job that delivers pending messages, paced, with crash recovery.
"""
