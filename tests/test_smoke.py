"""Smoke tests: the package imports and the captured fixtures are present."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

import bome_navaja

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

EXPECTED_FIXTURES = [
    "home.html",
    "b6416.html",
    "bx41.html",
    "b5092.html",
    "art1051.html",
    "art2014.html",
    "sum6416.html",
    "s_pe.html",
    "q_pe.html",
    "cal.json",
    "cons1.json",
    "org38.json",
    "y2005.html",
]


def test_version_matches_pyproject() -> None:
    with PYPROJECT.open("rb") as fh:
        project = tomllib.load(fh)["project"]
    assert bome_navaja.__version__ == project["version"]


@pytest.mark.parametrize("name", EXPECTED_FIXTURES)
def test_fixture_exists_and_is_not_empty(fixtures_dir: Path, name: str) -> None:
    path = fixtures_dir / name
    assert path.is_file(), f"missing fixture: {name}"
    assert path.stat().st_size > 0, f"empty fixture: {name}"


def test_read_fixture_returns_text(read_fixture) -> None:
    assert read_fixture("home.html").strip()
