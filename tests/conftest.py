"""Shared pytest fixtures and the opt-in gate for live tests."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"

LIVE_ENV_VAR = "BOME_NAVAJA_LIVE"


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip tests marked ``live`` unless BOME_NAVAJA_LIVE is set.

    Live tests hit the real bomemelilla.es site; they must never run in CI.
    """
    if os.environ.get(LIVE_ENV_VAR):
        return
    skip_live = pytest.mark.skip(
        reason=f"live test: set {LIVE_ENV_VAR}=1 to hit the real bomemelilla.es site"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture
def fixtures_dir() -> Path:
    """Directory holding real captured samples from bomemelilla.es."""
    return FIXTURES_DIR


@pytest.fixture
def read_fixture(fixtures_dir: Path) -> Callable[[str], str]:
    """Return a callable that reads a fixture file by name as UTF-8 text."""

    def _read(name: str) -> str:
        return (fixtures_dir / name).read_text(encoding="utf-8")

    return _read
