"""Every release notes file in the repository is valid (specs/011-release-notes US5, FR-025).

Warnings count as failures here: files in the repository must have both languages."""

from __future__ import annotations

import tomllib
from pathlib import Path

from scribe.release_notes import version
from scribe.release_notes.notes import MAX_SECTION_CHARS, load_notes

ROOT = Path(__file__).resolve().parents[2]
NOTES_DIR = ROOT / "release-notes"


def test_every_file_parses_with_both_languages() -> None:
    notes = load_notes(NOTES_DIR)  # raises NotesError naming the file
    assert notes, "release-notes/ has no X.Y.Z.md file"
    for item in notes.values():
        name = version.format(item.version)
        assert item.warnings == [], f"{name}.md: {item.warnings}"
        assert set(item.texts) == {"en", "ru"}, name
        assert all(len(text) <= MAX_SECTION_CHARS for text in item.texts.values())


def test_no_notes_newer_than_the_project_version() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    current = version.parse(project["version"])
    newest = max(load_notes(NOTES_DIR))
    assert newest <= current, (
        f"release-notes/{version.format(newest)}.md is newer than pyproject.toml's "
        f"{project['version']}: bump the version (and run uv lock)"
    )


def test_lock_file_has_the_project_version() -> None:
    """``uv sync --locked`` in the Docker build fails when the bump skipped ``uv lock``."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    (scribe,) = [pkg for pkg in lock["package"] if pkg["name"] == "scribe"]
    assert scribe["version"] == project["version"]
