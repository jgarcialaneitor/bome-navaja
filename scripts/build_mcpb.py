"""Stage the bome-navaja .mcpb bundle and generate its manifest.json.

Pure stdlib and pathlib, so it runs the same on Linux, macOS and Windows. It
assembles ``build/mcpb/`` from the minimum set of inputs uv needs to install
and run the server, then leaves the actual ``.mcpb`` packing to
``npx @anthropic-ai/mcpb pack`` (see ``build_mcpb.sh`` / ``build_mcpb.ps1``).

Ported from navaja's ``scripts/build_mcpb.py``, including its safety guards.
Like navaja, ``uv.lock`` is not staged: uv resolves the dependencies from
``pyproject.toml`` on first launch.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = PROJECT_ROOT / "mcpb" / "manifest.template.json"
LAUNCHER = "bome_navaja_mcpb.py"

# The MCPB runtime substitutes ``${__dirname}`` (the installed bundle
# directory) and ``${user_config.<key>}`` for keys the manifest declares.
RUNTIME_PLACEHOLDERS = {"${__dirname}"}

_PLACEHOLDER_RE = re.compile(r"\$\{[^}]+\}")

STAGED_SOURCE_DIRECTORIES = ("src", "mcpb")
_SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")


def _pyproject_version() -> str:
    """Read the project version from ``pyproject.toml``."""
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["project"]["version"]


def generate_manifest(version: str) -> dict:
    """Load the manifest template, inject *version*, and return the dict.

    Any ``${...}`` placeholder left after injection other than the runtime
    ones (``${__dirname}`` and ``${user_config.<declared key>}``) raises
    :class:`ValueError`, so the manifest cannot ship with an unresolved
    build-time variable.
    """
    rendered = TEMPLATE_PATH.read_text(encoding="utf-8").replace("${VERSION}", version)
    manifest = json.loads(rendered)
    allowed = RUNTIME_PLACEHOLDERS | {
        f"${{user_config.{key}}}" for key in manifest.get("user_config", {})
    }
    for placeholder in _PLACEHOLDER_RE.findall(rendered):
        if placeholder not in allowed:
            raise ValueError(f"Unresolved manifest placeholder: {placeholder}")
    return manifest


def _ensure_safe_staging_target(bundle_root: Path) -> None:
    """Refuse destructive staging targets before anything is deleted.

    ``stage`` replaces the target directory wholesale, so a mistaken ``--out``
    must never delete existing data. Refused targets:

    * the project root itself, or any ancestor of it (deleting it would
      delete the checkout);
    * any staged source directory (``src/``, ``mcpb/``);
    * an existing non-empty directory that does not look like a previous
      staging output (``manifest.json`` plus the launcher).
    """
    if bundle_root == PROJECT_ROOT or bundle_root in PROJECT_ROOT.parents:
        raise ValueError(f"staging target {bundle_root} contains the project sources")
    for name in STAGED_SOURCE_DIRECTORIES:
        source = PROJECT_ROOT / name
        if source == bundle_root:
            raise ValueError(f"staging target {bundle_root} would replace staged source {source}")

    if bundle_root.exists() and any(bundle_root.iterdir()):
        owned = (bundle_root / "manifest.json").is_file() and (bundle_root / LAUNCHER).is_file()
        if not owned:
            raise ValueError(
                f"refusing to replace non-bundle directory {bundle_root}; point --out at "
                "a fresh directory or a previous staging output"
            )


def stage(bundle_root: Path) -> None:
    """Stage the bundle inputs into *bundle_root* and write ``manifest.json``.

    Copies ``pyproject.toml``, ``README.md`` and ``LICENSE`` (pyproject
    declares them, and hatchling refuses to build without them), the ``src/``
    tree without caches, the launcher and ``.mcpbignore``. Previous staged
    contents are removed first so repeated builds are deterministic; unsafe
    targets are refused with :class:`ValueError` before anything is deleted.
    """
    bundle_root = Path(bundle_root)
    _ensure_safe_staging_target(bundle_root)
    version = _pyproject_version()

    if bundle_root.exists():
        shutil.rmtree(bundle_root)
    bundle_root.mkdir(parents=True)

    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(PROJECT_ROOT / name, bundle_root / name)
    shutil.copytree(PROJECT_ROOT / "src", bundle_root / "src", ignore=_SKIP)
    shutil.copy2(PROJECT_ROOT / "mcpb" / LAUNCHER, bundle_root / LAUNCHER)
    shutil.copy2(PROJECT_ROOT / "mcpb" / ".mcpbignore", bundle_root / ".mcpbignore")

    manifest = generate_manifest(version)
    (bundle_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Stage the bome-navaja .mcpb bundle inputs.")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("build/mcpb"),
        help="Output directory for the staged bundle (default: build/mcpb).",
    )
    parser.add_argument(
        "--print-version",
        action="store_true",
        help="Print the project version (used to name the .mcpb file) and exit.",
    )
    args = parser.parse_args(argv)
    if args.print_version:
        print(_pyproject_version())
        return 0
    bundle_root = args.out.resolve()
    stage(bundle_root)
    print(bundle_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
