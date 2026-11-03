"""``X.Y.Z`` versions (specs/011-release-notes research R2)."""

from __future__ import annotations

import pytest

from scribe.release_notes import version


def test_parse_compares_numerically() -> None:
    assert version.parse("0.10.0") > version.parse("0.9.0")
    assert version.parse("1.0.0") > version.parse("0.99.99")
    assert version.parse("0.2.0") == (0, 2, 0)


@pytest.mark.parametrize(
    "raw", ["v0.2.0", "0.2", "0.2.0.dev0", "0.2.0-rc1", " 0.2.0", "0.2.0 ", "", "a.b.c", "0..1"]
)
def test_parse_rejects(raw: str) -> None:
    with pytest.raises(ValueError, match="X.Y.Z"):
        version.parse(raw)


def test_format_round_trips() -> None:
    for raw in ("0.2.0", "10.0.3", "0.0.0"):
        assert version.format(version.parse(raw)) == raw


def test_running_version_reads_the_package() -> None:
    assert version.running_version(read=lambda name: "0.3.1") == (0, 3, 1)


def test_running_version_unparsed_is_none() -> None:
    assert version.running_version(read=lambda name: "0.2.0.dev0") is None


def test_running_version_missing_package_is_none() -> None:
    def missing(name: str) -> str:
        import importlib.metadata

        raise importlib.metadata.PackageNotFoundError(name)

    assert version.running_version(read=missing) is None
