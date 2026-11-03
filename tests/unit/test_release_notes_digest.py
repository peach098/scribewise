"""The message a user gets (specs/011-release-notes contracts/release-notes-file.md "Message
to a user", research R5)."""

from __future__ import annotations

from scribe.i18n import t
from scribe.release_notes.notes import (
    MAX_SECTION_CHARS,
    TELEGRAM_MAX_CHARS,
    ReleaseNotes,
    build_parts,
)


def _notes(version: tuple[int, int, int], en: str | None, ru: str | None) -> ReleaseNotes:
    texts = {lang: text for lang, text in (("en", en), ("ru", ru)) if text}
    return ReleaseNotes(version, texts, [])


def test_one_version() -> None:
    notes = {(0, 2, 0): _notes((0, 2, 0), "- New.", "- Новое.")}
    assert build_parts(notes, [(0, 2, 0)], "en") == ["What's new in Scribe 0.2.0\n\n- New."]
    assert build_parts(notes, [(0, 2, 0)], "ru") == ["Что нового в Scribe 0.2.0\n\n- Новое."]


def test_two_versions_oldest_first() -> None:
    notes = {
        (0, 2, 0): _notes((0, 2, 0), "A", "А"),
        (0, 3, 0): _notes((0, 3, 0), "B", "Б"),
    }
    heading = t("release_notes.heading", "en", version="0.2.0")
    expected = f"{heading}\n\nA\n\n{t('release_notes.heading', 'en', version='0.3.0')}\n\nB"
    assert build_parts(notes, [(0, 3, 0), (0, 2, 0)], "en") == [expected]


def test_language_fallback() -> None:
    notes = {(0, 2, 0): _notes((0, 2, 0), "English only", None)}
    assert build_parts(notes, [(0, 2, 0)], "ru") == ["Что нового в Scribe 0.2.0\n\nEnglish only"]


def test_version_without_text_is_left_out() -> None:
    notes = {
        (0, 2, 0): _notes((0, 2, 0), None, None),
        (0, 3, 0): _notes((0, 3, 0), "B", "Б"),
    }
    assert build_parts(notes, [(0, 2, 0), (0, 3, 0)], "en") == ["What's new in Scribe 0.3.0\n\nB"]
    assert build_parts(notes, [(0, 2, 0)], "en") == []
    assert build_parts(notes, [(0, 9, 0)], "en") == []  # not in the snapshot


def test_long_versions_split_between_versions_only() -> None:
    versions = [(0, 2, 0), (0, 3, 0), (0, 4, 0)]
    notes = {v: _notes(v, str(v[1]) * MAX_SECTION_CHARS, None) for v in versions}
    parts = build_parts(notes, versions, "en")
    assert len(parts) == 3
    for part, v in zip(parts, versions, strict=True):
        assert len(part) <= TELEGRAM_MAX_CHARS
        assert part.startswith(f"What's new in Scribe 0.{v[1]}.0\n\n")


def test_short_versions_share_a_part() -> None:
    versions = [(0, 2, 0), (0, 3, 0), (0, 4, 0)]
    notes = {v: _notes(v, "x" * 1500, None) for v in versions}
    parts = build_parts(notes, versions, "en")
    assert len(parts) == 2  # two fit in 4,096, the third does not
    assert all(len(part) <= TELEGRAM_MAX_CHARS for part in parts)
