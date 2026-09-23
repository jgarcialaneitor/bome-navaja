"""Offline tests for the .mcpb bundle inputs, the staging script and its wrappers."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import re
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MCPB_DIR = REPO_ROOT / "mcpb"
SCRIPTS = REPO_ROOT / "scripts"
BUILD_SCRIPT = SCRIPTS / "build_mcpb.py"
TEMPLATE = MCPB_DIR / "manifest.template.json"


def _load_build_script():
    """Load scripts/build_mcpb.py as a module (it is not part of a package)."""
    spec = importlib.util.spec_from_file_location("build_mcpb", BUILD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_mcpb"] = module
    spec.loader.exec_module(module)
    return module


def _pyproject() -> dict:
    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)


def _template() -> dict:
    return json.loads(TEMPLATE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- manifest template


def test_manifest_template_is_valid_json_and_complete() -> None:
    manifest = _template()
    assert manifest["manifest_version"] == "0.4"
    assert manifest["name"] == "bome-navaja"
    assert manifest["version"] == "${VERSION}"
    assert manifest["display_name"] and manifest["description"]
    assert manifest["author"]["name"] == "jgarcialaneitor"
    assert manifest["license"] == "MIT"
    assert manifest["keywords"]

    server = manifest["server"]
    assert server["type"] == "uv"
    assert server["entry_point"] == "bome_navaja_mcpb.py"
    assert server["mcp_config"]["command"] == "uv"
    assert server["mcp_config"]["args"] == ["run", "--directory", "${__dirname}", "bome_navaja_mcpb.py"]

    assert set(manifest["compatibility"]["platforms"]) == {"darwin", "linux", "win32"}
    assert manifest["compatibility"]["runtimes"]["python"] == ">=3.12"
    assert manifest["privacy_policies"] == ["https://bomemelilla.es/politica-cookies"]


def test_manifest_tools_match_registered_server_tools() -> None:
    from bome_navaja import server as server_module

    registered = [tool.name for tool in asyncio.run(server_module.server.list_tools())]
    declared = [tool["name"] for tool in _template()["tools"]]
    assert len(declared) == len(set(declared)) == len(registered) == 17
    assert set(declared) == set(registered), (
        f"manifest tool list drift: declared={sorted(declared)} registered={sorted(registered)}"
    )
    for tool in _template()["tools"]:
        description = tool["description"]
        assert description and "\n" not in description, tool["name"]


def test_user_config_data_directory_is_optional() -> None:
    manifest = _template()
    setting = manifest["user_config"]["directorio_datos"]
    assert setting["type"] == "directory"
    assert setting["required"] is False
    # mcpb only substitutes ${user_config.X} when X has a value or a default: without
    # this empty default an unset setting would reach the server as the literal
    # "${user_config.directorio_datos}" (a relative path). Blank means unset in paths.py.
    assert setting["default"] == ""
    assert manifest["server"]["mcp_config"]["env"] == {
        "BOME_NAVAJA_DATA_DIR": "${user_config.directorio_datos}"
    }


# --------------------------------------------------------------------------- generate_manifest


def test_generate_manifest_injects_pyproject_version() -> None:
    build = _load_build_script()
    version = _pyproject()["project"]["version"]
    manifest = build.generate_manifest(version)
    assert manifest["version"] == version
    leftovers = set(re.findall(r"\$\{[^}]+\}", json.dumps(manifest)))
    assert "${VERSION}" not in leftovers
    declared = {f"${{user_config.{key}}}" for key in manifest.get("user_config", {})}
    assert leftovers <= build.RUNTIME_PLACEHOLDERS | declared


def test_generate_manifest_rejects_unknown_placeholders(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    build = _load_build_script()
    bad = tmp_path / "manifest.template.json"
    bad.write_text('{"version": "${VERSION}", "x": "${OOPS}"}', encoding="utf-8")
    monkeypatch.setattr(build, "TEMPLATE_PATH", bad)
    with pytest.raises(ValueError, match=r"\$\{OOPS\}"):
        build.generate_manifest("1.2.3")


# --------------------------------------------------------------------------- launcher & ignore file


def test_launcher_imports_and_calls_server_main() -> None:
    tree = ast.parse((MCPB_DIR / "bome_navaja_mcpb.py").read_text(encoding="utf-8"))
    imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "bome_navaja.server"
    ]
    assert imports and "main" in {alias.name for alias in imports[0].names}
    assert any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "main"
        for node in ast.walk(tree)
    )


def test_mcpbignore_excludes_caches_tests_and_outputs() -> None:
    lines = {line.strip() for line in (MCPB_DIR / ".mcpbignore").read_text(encoding="utf-8").splitlines()}
    for pattern in (".venv/", "__pycache__/", "*.pyc", ".pytest_cache/", "tests/", "build/", "dist/"):
        assert pattern in lines, pattern


# --------------------------------------------------------------------------- staging


def test_staging_includes_files_required_by_pyproject(tmp_path: Path) -> None:
    build = _load_build_script()
    out = tmp_path / "mcpb"
    build.stage(out)

    project = _pyproject()
    required = [
        "pyproject.toml",
        project["project"]["readme"],
        *project["project"]["license-files"],
        "manifest.json",
        "bome_navaja_mcpb.py",
        ".mcpbignore",
    ]
    for name in required:
        assert (out / name).is_file(), f"staged bundle is missing {name}"
    for package in project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]:
        assert (out / package / "__init__.py").is_file(), package
    assert (out / "src" / "bome_navaja" / "server.py").is_file()
    # Nothing from tests, fixtures or local caches is staged.
    staged = {path.relative_to(out).as_posix() for path in out.rglob("*")}
    assert not any(part.startswith(("tests", ".venv")) for part in staged)
    assert not any("__pycache__" in part or part.endswith(".pyc") for part in staged)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == project["project"]["version"]


def test_staging_refuses_to_replace_populated_non_bundle_dir(tmp_path: Path) -> None:
    build = _load_build_script()
    target = tmp_path / "precious"
    target.mkdir()
    (target / "sentinel.txt").write_text("keep me", encoding="utf-8")
    with pytest.raises(ValueError, match="non-bundle directory"):
        build.stage(target)
    assert (target / "sentinel.txt").read_text(encoding="utf-8") == "keep me"
    assert not (target / "manifest.json").exists()


def test_staging_refuses_targets_containing_project_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build = _load_build_script()
    project_root = tmp_path / "bome-navaja"
    (project_root / "src").mkdir(parents=True)
    (project_root / "mcpb").mkdir()
    monkeypatch.setattr(build, "PROJECT_ROOT", project_root)
    for target in (project_root, tmp_path, project_root / "src", project_root / "mcpb"):
        with pytest.raises(ValueError, match="project sources|staged source"):
            build.stage(target)
    assert (project_root / "src").is_dir() and (project_root / "mcpb").is_dir()


def test_staging_replaces_previous_staging_output(tmp_path: Path) -> None:
    build = _load_build_script()
    out = tmp_path / "mcpb"
    build.stage(out)
    (out / "stale-file.txt").write_text("stale", encoding="utf-8")
    build.stage(out)
    assert (out / "manifest.json").is_file()
    assert not (out / "stale-file.txt").exists()


def test_cli_prints_version_and_stages(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    build = _load_build_script()
    assert build.main(["--print-version"]) == 0
    assert capsys.readouterr().out.strip() == _pyproject()["project"]["version"]
    assert build.main(["--out", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "manifest.json").is_file()


# --------------------------------------------------------------------------- wrappers & CI


def test_shell_and_powershell_wrappers_do_the_same_steps() -> None:
    sh = (SCRIPTS / "build_mcpb.sh").read_text(encoding="utf-8")
    ps1 = (SCRIPTS / "build_mcpb.ps1").read_text(encoding="utf-8")
    for script in (sh, ps1):
        assert "scripts/build_mcpb.py" in script.replace("\\", "/")
        assert "--print-version" in script
        assert "@anthropic-ai/mcpb validate" in script
        assert "@anthropic-ai/mcpb pack" in script
        assert "bome-navaja-" in script and ".mcpb" in script
    assert "set -euo pipefail" in sh
    assert "$ErrorActionPreference = 'Stop'" in ps1
    native_calls = len(re.findall(r"& (?:uv|npx) ", ps1))
    assert native_calls >= 4
    assert ps1.count("$LASTEXITCODE") >= native_calls


def test_gitignore_ignores_build_outputs() -> None:
    lines = {line.strip() for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert {"build/", "dist/"} <= lines


def test_ci_builds_the_bundle_without_touching_the_site() -> None:
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert re.search(r"^  bundle:\n\s+runs-on: ubuntu-latest", ci, flags=re.MULTILINE)
    bundle = ci.split("\n  bundle:\n", 1)[1]
    for needle in ("actions/checkout", "astral-sh/setup-uv", "actions/setup-node", "scripts/build_mcpb.sh",
                   "actions/upload-artifact", "dist/*.mcpb"):
        assert needle in bundle, needle
    assert "BOME_NAVAJA_LIVE" not in bundle
    # The existing test jobs keep running the plain offline suite.
    assert ci.count("run: uv run pytest") == 2
