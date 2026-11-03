"""Release notes files (specs/011-release-notes contracts/release-notes-file.md)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scribe.release_notes.notes import (
    MAX_SECTION_CHARS,
    NotesError,
    ReleaseNotes,
    load_notes,
    parse_notes,
)

V = (0, 2, 0)


def test_both_sections() -> None:
    notes = parse_notes(V, "## en\n\n- New thing.\n\n## ru\n\n- Новое.\n")
    assert notes.texts == {"en": "- New thing.", "ru": "- Новое."}
    assert notes.warnings == []
    assert notes.has_text


def test_missing_ru_section_warns_and_falls_back() -> None:
    notes = parse_notes(V, "## en\nOnly English.\n")
    assert notes.texts == {"en": "Only English."}
    assert len(notes.warnings) == 1 and notes.warnings[0].startswith("missing ru section")
    assert notes.text_for("ru") == "Only English."
    assert notes.text_for("en") == "Only English."


def test_empty_ru_section_counts_as_missing() -> None:
    notes = parse_notes(V, "## en\nHello.\n## ru\n\n   \n")
    assert notes.texts == {"en": "Hello."}
    assert notes.warnings[0].startswith("missing ru section")


def test_both_empty_has_no_text() -> None:
    notes = parse_notes(V, "## en\n\n## ru\n")
    assert not notes.has_text
    assert notes.text_for("en") is None and notes.text_for("ru") is None
    assert notes.warnings  # treated as having no notes


def test_empty_file_has_no_text() -> None:
    assert not parse_notes(V, "").has_text


def test_html_comments_before_first_section_are_ignored() -> None:
    text = "<!-- one -->\n\n<!--\nmulti\nline\n-->\n## en\nHi.\n## ru\nПривет.\n"
    notes = parse_notes(V, text)
    assert notes.texts == {"en": "Hi.", "ru": "Привет."}


def test_plain_text_before_first_section_is_an_error() -> None:
    with pytest.raises(NotesError, match="before the first section"):
        parse_notes(V, "Intro\n## en\nHi.\n")


def test_unclosed_comment_is_an_error() -> None:
    with pytest.raises(NotesError):
        parse_notes(V, "<!-- never closed\n## en\nHi.\n")


def test_unknown_section_is_an_error() -> None:
    with pytest.raises(NotesError, match="unknown section"):
        parse_notes(V, "## en\nHi.\n## de\nHallo.\n")


def test_duplicate_section_is_an_error() -> None:
    with pytest.raises(NotesError, match="duplicate section"):
        parse_notes(V, "## en\nHi.\n## en\nAgain.\n")


def test_section_length_limit() -> None:
    ok = "x" * MAX_SECTION_CHARS
    assert parse_notes(V, f"## en\n{ok}\n").texts["en"] == ok
    with pytest.raises(NotesError, match="3500"):
        parse_notes(V, f"## en\n{ok}x\n")


def test_blank_lines_stripped_inner_lines_verbatim() -> None:
    body = "* star <b>not html</b> _under_\n\n  indented line\n- bullet"
    notes = parse_notes(V, f"## en\n\n\n{body}\n\n\n## ru\n{body}")
    assert notes.texts["en"] == body
    assert notes.texts["ru"] == body


def test_error_names_the_file(tmp_path: Path) -> None:
    (tmp_path / "0.2.0.md").write_text("oops\n## en\nHi.\n", encoding="utf-8")
    with pytest.raises(NotesError, match=r"0\.2\.0\.md"):
        load_notes(tmp_path)


def test_load_notes_skips_readme_and_reads_versions(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Not notes\nfree text\n", encoding="utf-8")
    (tmp_path / "0.2.0.md").write_text("## en\nA.\n## ru\nА.\n", encoding="utf-8")
    (tmp_path / "0.10.0.md").write_text("## en\nB.\n## ru\nБ.\n", encoding="utf-8")
    notes = load_notes(tmp_path)
    assert sorted(notes) == [(0, 2, 0), (0, 10, 0)]
    assert isinstance(notes[(0, 10, 0)], ReleaseNotes)
    assert notes[(0, 10, 0)].version == (0, 10, 0)


@pytest.mark.parametrize("name", ["0.2.md", "v0.2.0.md", "0.2.0-rc1.md"])
def test_load_notes_rejects_bad_version_names(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_text("## en\nA.\n", encoding="utf-8")
    with pytest.raises(NotesError, match="X.Y.Z"):
        load_notes(tmp_path)


def test_load_notes_missing_directory(tmp_path: Path) -> None:
    assert load_notes(tmp_path / "nope") == {}
