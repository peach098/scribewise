"""Release notes files and the messages built from them (pure; specs/011-release-notes
contracts/release-notes-file.md).

A file ``release-notes/<X.Y.Z>.md`` has a ``## en`` and a ``## ru`` section. Before the first
section only blank lines and HTML comments are allowed. Each section is sent as plain text,
stripped of leading and trailing blank lines, and holds at most :data:`MAX_SECTION_CHARS`
characters, so one version always fits in a Telegram message with its heading.

The message to a user is one block per version (oldest first): the ``release_notes.heading``
line in the user's language, a blank line, then the section in that language (or the other
one when it is empty). Blocks are packed into messages of at most :data:`TELEGRAM_MAX_CHARS`
characters and split only between versions.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scribe.i18n import LANGUAGES, Msg, normalize_lang
from scribe.logging import get_logger
from scribe.release_notes import version as versions
from scribe.release_notes.version import Version

log = get_logger("scribe.release_notes")

MAX_SECTION_CHARS = 3500
TELEGRAM_MAX_CHARS = 4096
BLOCK_SEPARATOR = "\n\n"
_SECTION = re.compile(r"^##\s+(\S.*?)\s*$")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_VERSION_LIKE = re.compile(r"^v?\d")  # a file meant to be notes (``0.2.md``), not README.md


class NotesError(ValueError):
    """An invalid notes file; the message names the file when it comes from :func:`load_notes`."""


@dataclass(frozen=True)
class ReleaseNotes:
    version: Version
    texts: dict[str, str]  # "en" / "ru" -> text; only non-empty sections
    warnings: list[str] = field(default_factory=list)

    @property
    def has_text(self) -> bool:
        return bool(self.texts)

    def text_for(self, lang: str) -> str | None:
        """The text in ``lang``, else in the other language, else ``None`` (FR-011)."""
        wanted = normalize_lang(lang)
        if wanted in self.texts:
            return self.texts[wanted]
        for other in LANGUAGES:
            if other in self.texts:
                return self.texts[other]
        return None


def parse_notes(version: Version, text: str) -> ReleaseNotes:
    """Parse one file's content; raises :class:`NotesError` on the errors of the contract."""
    preamble: list[str] = []
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        header = _SECTION.match(line)
        if header is not None:
            name = header.group(1)
            if name not in LANGUAGES:
                raise NotesError(f"unknown section '## {name}' (only ## en and ## ru)")
            if name in sections:
                raise NotesError(f"duplicate section '## {name}'")
            current = sections[name] = []
            continue
        (preamble if current is None else current).append(line)
    if _COMMENT.sub("", "\n".join(preamble)).strip():
        raise NotesError("text before the first section (only blank lines and <!-- comments -->)")

    texts: dict[str, str] = {}
    for lang in LANGUAGES:
        body = _strip_blank_lines(sections.get(lang, []))
        if len(body) > MAX_SECTION_CHARS:
            raise NotesError(
                f"section '## {lang}' has {len(body)} characters (at most {MAX_SECTION_CHARS})"
            )
        if body:
            texts[lang] = body
    warnings: list[str] = []
    if not texts:
        warnings.append("no en or ru text: the version is treated as having no notes")
    else:
        for lang in LANGUAGES:
            if lang not in texts:
                other = next(code for code in LANGUAGES if code != lang)
                warnings.append(f"missing {lang} section: {lang} users get the {other} text")
    return ReleaseNotes(version, texts, warnings)


def _strip_blank_lines(lines: list[str]) -> str:
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[start:end])


def notes_path(directory: Path, version: Version) -> Path:
    return directory / f"{versions.format(version)}.md"


def read_notes(path: Path) -> ReleaseNotes:
    """One file; the version comes from its name. Errors name the file."""
    try:
        version = versions.parse(path.name.removesuffix(".md"))
    except ValueError:
        raise NotesError(f"{path.name}: file name must be X.Y.Z.md") from None
    try:
        return parse_notes(version, path.read_text(encoding="utf-8"))
    except NotesError as exc:
        raise NotesError(f"{path.name}: {exc}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise NotesError(f"{path.name}: cannot be read ({type(exc).__name__})") from None


def load_notes(directory: Path) -> dict[Version, ReleaseNotes]:
    """Every ``X.Y.Z.md`` file of ``directory``; ``{}`` when the directory does not exist.

    Other names (``README.md``) are skipped. A ``.md`` file whose name starts like a version
    but is not ``X.Y.Z`` (``0.2.md``, ``v0.2.0.md``) is an error, so a typo is not silently
    ignored.
    """
    if not directory.is_dir():
        log.warning("release_notes_dir_missing", directory=str(directory))
        return {}
    notes: dict[Version, ReleaseNotes] = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix != ".md" or not _VERSION_LIKE.match(path.name):
            continue
        parsed = read_notes(path)
        notes[parsed.version] = parsed
    return notes


# --- snapshot stored with an announcement (research R5) --------------------------------------


def snapshot(notes: Iterable[ReleaseNotes]) -> dict[str, dict[str, str]]:
    """``{"0.2.0": {"en": ..., "ru": ...}}`` for ``release_announcements.notes_json``."""
    return {versions.format(item.version): dict(item.texts) for item in notes}


def from_snapshot(data: Mapping[str, Any]) -> dict[Version, ReleaseNotes]:
    result: dict[Version, ReleaseNotes] = {}
    for raw_version, texts in data.items():
        version = versions.parse(raw_version)
        kept: dict[str, str] = {
            lang: texts[lang] for lang in LANGUAGES if isinstance(texts.get(lang), str)
        }
        result[version] = ReleaseNotes(version, {k: v for k, v in kept.items() if v})
    return result


# --- the message to a user --------------------------------------------------------------------


def render_block(version: Version, text: str, lang: str) -> str:
    heading = Msg("release_notes.heading", version=versions.format(version)).render(lang)
    return f"{heading}\n\n{text}"


def build_parts(
    notes_by_version: Mapping[Version, ReleaseNotes], versions_: Iterable[Version], lang: str
) -> list[str]:
    """The messages of one user's digest: oldest version first, packed greedily into parts of
    at most :data:`TELEGRAM_MAX_CHARS` characters, split only between versions. Versions with
    no text (or not in ``notes_by_version``) are left out; no text at all gives ``[]``."""
    parts: list[str] = []
    for version in sorted(set(versions_)):
        notes = notes_by_version.get(version)
        text = notes.text_for(lang) if notes is not None else None
        if not text:
            continue
        block = render_block(version, text, lang)
        if parts and len(parts[-1]) + len(BLOCK_SEPARATOR) + len(block) <= TELEGRAM_MAX_CHARS:
            parts[-1] = f"{parts[-1]}{BLOCK_SEPARATOR}{block}"
        else:
            parts.append(block)
    return parts
