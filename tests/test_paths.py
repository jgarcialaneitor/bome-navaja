"""Cross-platform data paths, tested for every platform from any host."""

from __future__ import annotations

from pathlib import Path

import pytest

from bome_navaja.paths import APP_NAME, data_dir, index_path, pdf_dir

HOME = Path("/home/ana")


def test_app_name() -> None:
    assert APP_NAME == "bome-navaja"


# --------------------------------------------------------------------------- data_dir


def test_linux_default() -> None:
    path, reason = data_dir(platform="linux", environ={}, home=HOME)
    assert path == HOME / ".local" / "share" / "bome-navaja"
    assert reason == "XDG default"


def test_linux_xdg_data_home() -> None:
    path, reason = data_dir(platform="linux", environ={"XDG_DATA_HOME": "/data/xdg"}, home=HOME)
    assert path == Path("/data/xdg/bome-navaja")
    assert reason == "XDG_DATA_HOME"


def test_empty_env_values_are_ignored() -> None:
    environ = {"XDG_DATA_HOME": "", "BOME_NAVAJA_DATA_DIR": "  "}
    path, reason = data_dir(platform="linux", environ=environ, home=HOME)
    assert path == HOME / ".local" / "share" / "bome-navaja"
    assert reason == "XDG default"


def test_other_unix_uses_xdg() -> None:
    path, _ = data_dir(platform="freebsd14", environ={}, home=HOME)
    assert path == HOME / ".local" / "share" / "bome-navaja"


def test_macos_default_ignores_xdg() -> None:
    path, reason = data_dir(platform="darwin", environ={"XDG_DATA_HOME": "/x"}, home=HOME)
    assert path == HOME / "Library" / "Application Support" / "bome-navaja"
    assert reason == "macOS default"


def test_windows_localappdata() -> None:
    environ = {"LOCALAPPDATA": r"C:\Users\ana\AppData\Local"}
    path, reason = data_dir(platform="win32", environ=environ, home=HOME)
    assert path == Path(r"C:\Users\ana\AppData\Local") / "bome-navaja"
    assert reason == "LOCALAPPDATA"


def test_windows_without_localappdata() -> None:
    path, reason = data_dir(platform="win32", environ={}, home=HOME)
    assert path == HOME / "AppData" / "Local" / "bome-navaja"
    assert reason == "Windows default"


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_data_dir_env_override_wins_everywhere(platform: str) -> None:
    environ = {
        "BOME_NAVAJA_DATA_DIR": "/srv/bome",
        "LOCALAPPDATA": "C:/x",
        "XDG_DATA_HOME": "/x",
    }
    path, reason = data_dir(platform=platform, environ=environ, home=HOME)
    assert path == Path("/srv/bome")
    assert reason == "BOME_NAVAJA_DATA_DIR"


def test_env_override_expands_tilde_with_injected_home() -> None:
    path, _ = data_dir(platform="linux", environ={"BOME_NAVAJA_DATA_DIR": "~/bome"}, home=HOME)
    assert path == HOME / "bome"


def test_defaults_use_real_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BOME_NAVAJA_DATA_DIR", str(tmp_path))
    assert data_dir() == (tmp_path, "BOME_NAVAJA_DATA_DIR")


def test_functions_do_not_create_directories(tmp_path: Path) -> None:
    environ = {"BOME_NAVAJA_DATA_DIR": str(tmp_path / "nope")}
    data_dir(environ=environ)
    pdf_dir(environ=environ)
    index_path(environ=environ)
    assert not (tmp_path / "nope").exists()


# --------------------------------------------------------------------------- pdf_dir / index_path


def test_pdf_dir_defaults_under_data_dir() -> None:
    path, reason = pdf_dir(platform="linux", environ={}, home=HOME)
    assert path == HOME / ".local" / "share" / "bome-navaja" / "pdfs"
    assert reason == "data dir (XDG default)"


def test_pdf_dir_env_override() -> None:
    environ = {"BOME_NAVAJA_PDF_DIR": "/pdfs", "BOME_NAVAJA_DATA_DIR": "/data"}
    path, reason = pdf_dir(platform="win32", environ=environ, home=HOME)
    assert path == Path("/pdfs")
    assert reason == "BOME_NAVAJA_PDF_DIR"


def test_pdf_dir_follows_data_dir_override() -> None:
    path, reason = pdf_dir(platform="darwin", environ={"BOME_NAVAJA_DATA_DIR": "/data"}, home=HOME)
    assert path == Path("/data/pdfs")
    assert reason == "data dir (BOME_NAVAJA_DATA_DIR)"


def test_index_path() -> None:
    environ = {"LOCALAPPDATA": r"C:\L"}
    path, reason = index_path(platform="win32", environ=environ, home=HOME)
    assert path == Path(r"C:\L") / "bome-navaja" / "sumarios.sqlite3"
    assert reason == "data dir (LOCALAPPDATA)"
