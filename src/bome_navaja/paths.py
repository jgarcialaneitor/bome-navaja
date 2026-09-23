"""Cross-platform locations for bome-navaja data (PDF cache, sumario index).

No third-party dependency: the rules are small and explicit.

``data_dir()`` resolution order:

1. ``BOME_NAVAJA_DATA_DIR`` environment variable.
2. Windows: ``%LOCALAPPDATA%\\bome-navaja``, else ``~\\AppData\\Local\\bome-navaja``.
3. macOS: ``~/Library/Application Support/bome-navaja``.
4. Anything else: ``$XDG_DATA_HOME/bome-navaja``, else ``~/.local/share/bome-navaja``.

Every function returns ``(path, reason)``, where ``reason`` names the rule
that decided, and accepts injectable ``platform`` (``sys.platform`` values),
``environ`` and ``home`` so every platform can be tested from any host.
Nothing here creates directories. Empty or blank variables count as unset;
a leading ``~`` in an override expands to ``home``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

APP_NAME = "bome-navaja"

DATA_DIR_ENV = "BOME_NAVAJA_DATA_DIR"
PDF_DIR_ENV = "BOME_NAVAJA_PDF_DIR"
INDEX_FILENAME = "sumarios.sqlite3"
PDF_SUBDIR = "pdfs"


def _env(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name, "")
    return value.strip() or None


def _expand(value: str, home: Path) -> Path:
    if value == "~" or value.startswith(("~/", "~\\")):
        return home / value[2:] if len(value) > 1 else home
    return Path(value)


def _context(
    platform: str | None, environ: Mapping[str, str] | None, home: Path | None
) -> tuple[str, Mapping[str, str], Path]:
    return (
        platform if platform is not None else sys.platform,
        environ if environ is not None else os.environ,
        home if home is not None else Path.home(),
    )


def data_dir(
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> tuple[Path, str]:
    """Directory holding every bome-navaja file, and why it was chosen."""
    platform, environ, home = _context(platform, environ, home)
    override = _env(environ, DATA_DIR_ENV)
    if override:
        return _expand(override, home), DATA_DIR_ENV
    if platform.startswith("win"):
        local = _env(environ, "LOCALAPPDATA")
        if local:
            return Path(local) / APP_NAME, "LOCALAPPDATA"
        return home / "AppData" / "Local" / APP_NAME, "Windows default"
    if platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME, "macOS default"
    xdg = _env(environ, "XDG_DATA_HOME")
    if xdg:
        return _expand(xdg, home) / APP_NAME, "XDG_DATA_HOME"
    return home / ".local" / "share" / APP_NAME, "XDG default"


def pdf_dir(
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> tuple[Path, str]:
    """Directory for downloaded PDFs: ``BOME_NAVAJA_PDF_DIR`` or ``data_dir()/pdfs``."""
    platform, environ, home = _context(platform, environ, home)
    override = _env(environ, PDF_DIR_ENV)
    if override:
        return _expand(override, home), PDF_DIR_ENV
    base, reason = data_dir(platform=platform, environ=environ, home=home)
    return base / PDF_SUBDIR, f"data dir ({reason})"


def index_path(
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> tuple[Path, str]:
    """SQLite file of the local sumario index: ``data_dir()/sumarios.sqlite3``."""
    base, reason = data_dir(platform=platform, environ=environ, home=home)
    return base / INDEX_FILENAME, f"data dir ({reason})"
