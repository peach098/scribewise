"""``X.Y.Z`` versions (specs/011-release-notes research R2).

The running version is the installed package's version (``importlib.metadata``), the same value
``pyproject.toml`` holds. Versions are compared as integer tuples, so ``0.10.0 > 0.9.0``. Only
three non-negative integers are accepted: no leading ``v``, no suffixes.
"""

from __future__ import annotations

import importlib.metadata
import re
from collections.abc import Callable

from scribe.logging import get_logger

log = get_logger("scribe.release_notes")

Version = tuple[int, int, int]
PACKAGE = "scribe"
_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def parse(text: str) -> Version:
    """``"0.2.0"`` -> ``(0, 2, 0)``; anything that is not ``X.Y.Z`` raises ``ValueError``."""
    match = _PATTERN.fullmatch(text)
    if match is None:
        raise ValueError(f"version must be X.Y.Z, got {text!r}")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def format(version: Version) -> str:  # noqa: A001 - module-level helper, used as version.format
    return ".".join(str(part) for part in version)


def running_version(
    read: Callable[[str], str] = importlib.metadata.version,
) -> Version | None:
    """The installed package's version, or ``None`` (logged) when it is not ``X.Y.Z``."""
    try:
        raw = read(PACKAGE)
    except importlib.metadata.PackageNotFoundError:
        log.warning("release_notes_version_unparsed", version=None)
        return None
    try:
        return parse(raw)
    except ValueError:
        log.warning("release_notes_version_unparsed", version=raw)
        return None
