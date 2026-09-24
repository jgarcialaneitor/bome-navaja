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
a leading ``~`` in an override expands to ``home``. The home directory is
only looked up when the chosen rule needs it; if it cannot be determined a
:class:`~bome_navaja.models.BomeStorageError` asks for ``BOME_NAVAJA_DATA_DIR``.
Absoluteness is judged by the HOST filesystem (``Path(value).is_absolute()``),
because these are real paths on the machine running the server: on Windows
``/srv/bome`` has no drive and is therefore relative, even when ``platform``
is injected as ``"linux"``. The injected ``platform`` only selects WHICH rule
applies (``LOCALAPPDATA`` vs ``Library`` vs XDG). Rules:

* ``~`` or ``~/x`` (also ``~\\x``) is joined to home and used as is: it is
  anchored at home, never passed through ``abspath`` (it is absolute iff
  home is).
* Any other host-relative ``BOME_NAVAJA_*`` override is resolved against the
  working directory at resolution time, and the reason says so.
* A host-relative ``XDG_DATA_HOME`` is ignored, as the XDG spec requires
  (a ``~`` value counts as anchored and is accepted).
* ``LOCALAPPDATA`` is taken as the OS gives it (no absoluteness check).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from .models import BomeStorageError

APP_NAME = "bome-navaja"

DATA_DIR_ENV = "BOME_NAVAJA_DATA_DIR"
PDF_DIR_ENV = "BOME_NAVAJA_PDF_DIR"
INDEX_FILENAME = "sumarios.sqlite3"
PDF_SUBDIR = "pdfs"


RELATIVE_NOTE = " (relative, resolved against the working directory)"


def _env(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name, "")
    return value.strip() or None


class _Home:
    """The home directory, resolved only when a rule actually needs it."""

    def __init__(self, home: Path | None) -> None:
        self._home = home

    def __call__(self) -> Path:
        if self._home is None:
            try:
                self._home = Path.home()
            except (RuntimeError, KeyError, OSError) as exc:
                raise BomeStorageError(
                    f"cannot determine the user's home directory ({exc}); set "
                    f"{DATA_DIR_ENV} (and optionally {PDF_DIR_ENV}) to an absolute path",
                    path="~",
                ) from exc
        return self._home


def _expand(value: str, home: _Home) -> tuple[Path, bool]:
    """``(path, anchored)``: ``~`` / ``~/x`` are joined to home and count as anchored."""
    if value == "~":
        return home(), True
    if value.startswith(("~/", "~\\")):
        return home() / value[2:], True
    path = Path(value)
    return path, path.is_absolute()


def _override(value: str, name: str, home: _Home) -> tuple[Path, str]:
    """An explicit ``BOME_NAVAJA_*`` path: ``~`` expanded, host-relative made absolute."""
    path, anchored = _expand(value, home)
    if anchored:
        return path, name
    return Path(os.path.abspath(path)), name + RELATIVE_NOTE


def _context(
    platform: str | None, environ: Mapping[str, str] | None, home: Path | None
) -> tuple[str, Mapping[str, str], _Home]:
    return (
        platform if platform is not None else sys.platform,
        environ if environ is not None else os.environ,
        _Home(home),
    )


def data_dir(
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> tuple[Path, str]:
    """Directory holding every bome-navaja file, and why it was chosen.

    Raises :class:`BomeStorageError` only when the chosen rule needs the home
    directory and it cannot be determined.
    """
    platform, environ, lazy_home = _context(platform, environ, home)
    override = _env(environ, DATA_DIR_ENV)
    if override:
        return _override(override, DATA_DIR_ENV, lazy_home)
    if platform.startswith("win"):
        local = _env(environ, "LOCALAPPDATA")
        if local:
            return Path(local) / APP_NAME, "LOCALAPPDATA"
        return lazy_home() / "AppData" / "Local" / APP_NAME, "Windows default"
    if platform == "darwin":
        return lazy_home() / "Library" / "Application Support" / APP_NAME, "macOS default"
    xdg = _env(environ, "XDG_DATA_HOME")
    if xdg:
        xdg_path, anchored = _expand(xdg, lazy_home)
        # The XDG spec: a relative path in these variables is invalid; ignore it.
        if anchored:
            return xdg_path / APP_NAME, "XDG_DATA_HOME"
    return lazy_home() / ".local" / "share" / APP_NAME, "XDG default"


def pdf_dir(
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> tuple[Path, str]:
    """Directory for downloaded PDFs: ``BOME_NAVAJA_PDF_DIR`` or ``data_dir()/pdfs``."""
    platform, environ, lazy_home = _context(platform, environ, home)
    override = _env(environ, PDF_DIR_ENV)
    if override:
        return _override(override, PDF_DIR_ENV, lazy_home)
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
