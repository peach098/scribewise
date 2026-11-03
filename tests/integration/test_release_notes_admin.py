"""Operator CLI ``release-notes`` commands (specs/011-release-notes contracts/operator-cli.md).
In-process ``scribe.admin.run`` against the test DB; synthetic data only."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe import admin
from scribe.config import Settings
from scribe.db.models import ReleaseDeliveryState, ReleaseTrigger
from scribe.release_notes import version as release_version
from scribe.release_notes.announce import ensure_auto_announcement
from tests.conftest import FrozenClock, LogCapture, MakeUser
from tests.integration.bot_support import offline_bot
from tests.integration.release_notes_support import (
    announcements,
    deliveries,
    make_sender,
    notes_settings,
    write_notes,
)

SENDS_SOON = "the running service sends this within about a minute"


@pytest.fixture(autouse=True)
def _running(monkeypatch: pytest.MonkeyPatch, log_capture: LogCapture) -> None:
    """The CLI's running version; service logs go to ``log_capture``, not to stdout."""
    monkeypatch.setattr(release_version, "running_version", lambda: (0, 2, 0))


async def _run(
    argv: list[str],
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    settings: Settings,
) -> tuple[int, str, str]:
    code = await admin.run(argv, session_factory, clock=clock, settings=settings)
    out, err = capsys.readouterr()
    return code, out, err


async def test_preview_shows_both_languages(
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    settings: Settings,
    tmp_path: Path,
) -> None:
    write_notes(tmp_path, "0.2.0", en="- New.", ru="- Новое.")
    configured = notes_settings(settings, tmp_path)
    code, out, err = await _run(
        ["release-notes", "preview", "0.2.0"], session_factory, capsys, clock, configured
    )
    assert (code, err) == (0, "")
    assert out.splitlines() == [
        "--- en ---",
        "What's new in Scribe 0.2.0",
        "",
        "- New.",
        "--- ru ---",
        "Что нового в Scribe 0.2.0",
        "",
        "- Новое.",
    ]


async def test_preview_missing_ru_and_future_version(
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    settings: Settings,
    tmp_path: Path,
) -> None:
    write_notes(tmp_path, "0.3.0", en="Only English.")
    configured = notes_settings(settings, tmp_path)
    code, out, _ = await _run(
        ["release-notes", "preview", "0.3.0"], session_factory, capsys, clock, configured
    )
    assert code == 0
    lines = out.splitlines()
    assert lines[lines.index("--- ru ---") + 1 :][:3] == [
        "Что нового в Scribe 0.3.0",
        "",
        "Only English.",
    ]
    assert any(line.startswith("warning: missing ru section") for line in lines)
    assert lines[-1] == "note: announced automatically when 0.3.0 runs"


async def test_preview_missing_or_invalid_file(
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    settings: Settings,
    tmp_path: Path,
) -> None:
    configured = notes_settings(settings, tmp_path)
    code, out, err = await _run(
        ["release-notes", "preview", "0.2.0"], session_factory, capsys, clock, configured
    )
    assert (code, out) == (1, "") and "no release notes file" in err
    (tmp_path / "0.2.0.md").write_text("## de\nHallo\n", encoding="utf-8")
    code, _, err = await _run(
        ["release-notes", "preview", "0.2.0"], session_factory, capsys, clock, configured
    )
    assert code == 1 and "unknown section" in err


async def test_check(
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    settings: Settings,
    tmp_path: Path,
) -> None:
    configured = notes_settings(settings, tmp_path)
    write_notes(tmp_path, "0.2.0", en="A.", ru="А.")
    write_notes(tmp_path, "0.3.0", en="B.")
    (tmp_path / "README.md").write_text("free text", encoding="utf-8")
    code, out, err = await _run(
        ["release-notes", "check"], session_factory, capsys, clock, configured
    )
    assert (code, err) == (0, "")
    assert out.splitlines()[0] == "0.2.0 ok"
    assert out.splitlines()[1].startswith("0.3.0 warning: missing ru section")

    (tmp_path / "0.4.md").write_text("## en\nC.\n", encoding="utf-8")
    code, _, err = await _run(
        ["release-notes", "check"], session_factory, capsys, clock, configured
    )
    assert code == 1 and "0.4.md" in err


async def test_send_dry_run_writes_nothing(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    settings: Settings,
    make_user: MakeUser,
    tmp_path: Path,
) -> None:
    await make_user(language="en")
    await make_user(language="ru")
    write_notes(tmp_path, "0.2.0", en="A.", ru="А.")
    configured = notes_settings(settings, tmp_path)
    code, out, _ = await _run(
        ["release-notes", "send", "0.2.0", "--dry-run"],
        session_factory,
        capsys,
        clock,
        configured,
    )
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == "dry run 0.2.0: 2 recipients (en 1, ru 1), 0 already have it"
    assert "--- en ---" in lines and "--- ru ---" in lines
    assert await announcements(db_session) == []


async def test_send_queues_users_who_lack_the_version(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    settings: Settings,
    make_user: MakeUser,
    tmp_path: Path,
) -> None:
    got_it = await make_user(language="en")
    write_notes(tmp_path, "0.2.0", en="A.", ru="А.")
    configured = notes_settings(settings, tmp_path)
    await ensure_auto_announcement(session_factory, configured, now=clock.now, running=(0, 2, 0))
    bot, _ = offline_bot()
    await make_sender(session_factory, bot, configured, clock, running=None).drain()
    newcomer = await make_user(language="ru")
    clock.advance(minutes=1)

    code, out, err = await _run(
        ["release-notes", "send", "0.2.0"], session_factory, capsys, clock, configured
    )

    assert (code, err) == (0, "")
    assert out.splitlines() == [
        "queued 0.2.0: 1 recipients (en 0, ru 1), 1 already have it",
        SENDS_SOON,
    ]
    _, manual = await announcements(db_session)
    assert (manual.trigger, manual.versions, manual.recipients) == (
        ReleaseTrigger.manual,
        ["0.2.0"],
        1,
    )
    pending = [d for d in await deliveries(db_session) if d.announcement_id == manual.id]
    assert [(d.user_id, d.state) for d in pending] == [(newcomer.id, ReleaseDeliveryState.pending)]
    assert got_it.id not in {d.user_id for d in pending}

    clock.advance(minutes=1)
    await make_sender(session_factory, bot, configured, clock, running=None).drain()
    code, _, err = await _run(
        ["release-notes", "send", "0.2.0"], session_factory, capsys, clock, configured
    )
    assert code == 1 and err.startswith("nothing to send")


async def test_send_version_without_text(
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    settings: Settings,
    make_user: MakeUser,
    tmp_path: Path,
) -> None:
    await make_user()
    write_notes(tmp_path, "0.2.0")
    configured = notes_settings(settings, tmp_path)
    code, _, err = await _run(
        ["release-notes", "send", "0.2.0"], session_factory, capsys, clock, configured
    )
    assert code == 1 and "no en or ru text" in err


async def test_status(
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    clock: FrozenClock,
    settings: Settings,
    make_user: MakeUser,
    tmp_path: Path,
) -> None:
    configured = notes_settings(settings, tmp_path)
    code, out, _ = await _run(
        ["release-notes", "status"], session_factory, capsys, clock, configured
    )
    assert code == 0
    assert out.splitlines() == ["running=0.2.0 last_handled=- auto=on", "no announcements"]

    await make_user()
    await make_user()
    write_notes(tmp_path, "0.2.0", en="A.")
    await ensure_auto_announcement(session_factory, configured, now=clock.now, running=(0, 2, 0))
    bot, _ = offline_bot()
    await make_sender(session_factory, bot, configured, clock, running=None).drain()
    clock.advance(hours=1)
    await ensure_auto_announcement(session_factory, configured, now=clock.now, running=(0, 2, 1))

    code, out, _ = await _run(
        ["release-notes", "status"], session_factory, capsys, clock, configured
    )
    assert out.splitlines() == [
        "running=0.2.0 last_handled=0.2.1 auto=on",
        "2026-10-14 11:00 auto release=0.2.1 versions=- recipients=0 sent=0 pending=0 "
        "blocked=0 failed=0 skipped=0 unknown=0 finished=2026-10-14 11:00",
    ]
    code, out, _ = await _run(
        ["release-notes", "status", "--all"], session_factory, capsys, clock, configured
    )
    lines = out.splitlines()
    assert len(lines) == 3
    assert lines[2].startswith("2026-10-14 10:00 auto release=0.2.0 versions=0.2.0 recipients=2 ")
    assert " sent=2 pending=0 " in lines[2]


@pytest.mark.parametrize("argv", [["send", "v0.2.0"], ["preview", "0.2"], ["send"]])
def test_bad_version_is_a_usage_error(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        admin.main(["release-notes", *argv])
    assert exc.value.code == 2
