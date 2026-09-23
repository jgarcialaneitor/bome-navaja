"""Cross-platform data paths, tested for every platform from any host."""

from __future__ import annotations

from pathlib import Path

import pytest

from bome_navaja.paths import APP_NAME, data_dir, index_path, pdf_dir

HOME = Path("/home/ana")
"""Only joined to, never checked for absoluteness, so a POSIX literal works on every host."""

ROOT = Path(Path(__file__).resolve().anchor)
"""Host filesystem root ("/" on Linux/macOS, e.g. "D:\\" on Windows): paths built from it
are absolute on the host, which is what the override and XDG rules check."""


def host_abs(*parts: str) -> Path:
    return ROOT.joinpath(*parts)


def test_app_name() -> None:
    assert APP_NAME == "bome-navaja"


# --------------------------------------------------------------------------- data_dir


def test_linux_default() -> None:
    path, reason = data_dir(platform="linux", environ={}, home=HOME)
    assert path == HOME / ".local" / "share" / "bome-navaja"
    assert reason == "XDG default"


def test_linux_xdg_data_home() -> None:
    xdg = host_abs("data", "xdg")
    path, reason = data_dir(platform="linux", environ={"XDG_DATA_HOME": str(xdg)}, home=HOME)
    assert path == xdg / "bome-navaja"
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
        "BOME_NAVAJA_DATA_DIR": str(host_abs("srv", "bome")),
        "LOCALAPPDATA": "C:/x",
        "XDG_DATA_HOME": "/x",
    }
    path, reason = data_dir(platform=platform, environ=environ, home=HOME)
    assert path == host_abs("srv", "bome")
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
    environ = {"BOME_NAVAJA_PDF_DIR": str(host_abs("pdfs")), "BOME_NAVAJA_DATA_DIR": str(host_abs("data"))}
    path, reason = pdf_dir(platform="win32", environ=environ, home=HOME)
    assert path == host_abs("pdfs")
    assert reason == "BOME_NAVAJA_PDF_DIR"


def test_pdf_dir_follows_data_dir_override() -> None:
    path, reason = pdf_dir(platform="darwin", environ={"BOME_NAVAJA_DATA_DIR": str(host_abs("data"))}, home=HOME)
    assert path == host_abs("data", "pdfs")
    assert reason == "data dir (BOME_NAVAJA_DATA_DIR)"


def test_index_path() -> None:
    environ = {"LOCALAPPDATA": r"C:\L"}
    path, reason = index_path(platform="win32", environ=environ, home=HOME)
    assert path == Path(r"C:\L") / "bome-navaja" / "sumarios.sqlite3"
    assert reason == "data dir (LOCALAPPDATA)"


# --------------------------------------------------------------------------- task 6 advisories


def _no_home() -> Path:
    raise RuntimeError("Could not determine home directory.")


def test_home_is_not_needed_when_an_absolute_override_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(_no_home))
    path, reason = data_dir(platform="linux", environ={"BOME_NAVAJA_DATA_DIR": str(host_abs("srv", "bome"))})
    assert (path, reason) == (host_abs("srv", "bome"), "BOME_NAVAJA_DATA_DIR")
    pdfs, _ = pdf_dir(platform="linux", environ={"BOME_NAVAJA_PDF_DIR": str(host_abs("pdfs"))})
    assert pdfs == host_abs("pdfs")
    windows, _ = data_dir(platform="win32", environ={"LOCALAPPDATA": r"C:\L"})
    assert windows == Path(r"C:\L") / "bome-navaja"


def test_missing_home_is_a_clear_bome_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from bome_navaja.models import BomeStorageError

    monkeypatch.setattr(Path, "home", staticmethod(_no_home))
    for platform in ("linux", "darwin", "win32"):
        with pytest.raises(BomeStorageError, match="BOME_NAVAJA_DATA_DIR"):
            data_dir(platform=platform, environ={})
    with pytest.raises(BomeStorageError):
        data_dir(platform="linux", environ={"BOME_NAVAJA_DATA_DIR": "~/bome"})


def test_relative_overrides_are_made_absolute(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    path, reason = data_dir(platform="linux", environ={"BOME_NAVAJA_DATA_DIR": "datos"}, home=HOME)
    assert path == tmp_path / "datos"
    assert path.is_absolute()
    assert reason == "BOME_NAVAJA_DATA_DIR (relative, resolved against the working directory)"
    pdfs, pdf_reason = pdf_dir(platform="linux", environ={"BOME_NAVAJA_PDF_DIR": "./pdfs"}, home=HOME)
    assert pdfs == tmp_path / "pdfs"
    assert pdf_reason.startswith("BOME_NAVAJA_PDF_DIR (relative")
    # The XDG spec says a relative XDG_DATA_HOME is invalid and must be ignored.
    xdg, xdg_reason = data_dir(platform="linux", environ={"XDG_DATA_HOME": "xdg"}, home=HOME)
    assert (xdg, xdg_reason) == (HOME / ".local" / "share" / "bome-navaja", "XDG default")


# --------------------------------------------------------------------------- Windows CI: host absoluteness


def test_tilde_override_is_anchored_at_home_never_at_the_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    relative_home = Path("perfil")  # absolute iff home is: here it is not
    path, reason = data_dir(platform="linux", environ={"BOME_NAVAJA_DATA_DIR": "~/bome"}, home=relative_home)
    assert (path, reason) == (relative_home / "bome", "BOME_NAVAJA_DATA_DIR")
    pdfs, pdf_reason = pdf_dir(platform="win32", environ={"BOME_NAVAJA_PDF_DIR": "~"}, home=relative_home)
    assert (pdfs, pdf_reason) == (relative_home, "BOME_NAVAJA_PDF_DIR")


def test_tilde_xdg_data_home_is_accepted() -> None:
    path, reason = data_dir(platform="linux", environ={"XDG_DATA_HOME": "~/xdg"}, home=Path("perfil"))
    assert (path, reason) == (Path("perfil") / "xdg" / "bome-navaja", "XDG_DATA_HOME")
