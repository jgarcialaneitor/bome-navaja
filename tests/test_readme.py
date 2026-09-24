"""The README stays in sync with the code: tools, env vars and repository paths."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

# Backticked repository paths such as `scripts/build_mcpb.sh` or `scripts\build_mcpb.ps1`.
_REPO_PATH = re.compile(r"`((?:scripts|src|mcpb|tests|\.github)[/\\][^`\s]+|LICENSE|pyproject\.toml)`")
# Relative Markdown links: [text](target) that are neither URLs nor in-page anchors.
_RELATIVE_LINK = re.compile(r"\]\((?!https?://|#|mailto:)([^)\s]+)\)")


def test_every_registered_tool_is_documented() -> None:
    from bome_navaja.server import server

    names = [tool.name for tool in asyncio.run(server.list_tools())]
    assert len(names) == 17
    missing = [name for name in names if f"`{name}`" not in README]
    assert not missing, f"tools missing from README.md: {missing}"


def test_every_environment_variable_the_code_reads_is_documented() -> None:
    sources = [*(REPO_ROOT / "src").rglob("*.py"), REPO_ROOT / "tests" / "conftest.py"]
    read_by_code = {
        name
        for path in sources
        for name in re.findall(r"BOME_NAVAJA_[A-Z_]+", path.read_text(encoding="utf-8"))
    }
    assert {"BOME_NAVAJA_DATA_DIR", "BOME_NAVAJA_PDF_DIR", "BOME_NAVAJA_LIVE"} <= read_by_code
    missing = sorted(name for name in read_by_code if name not in README)
    assert not missing, f"env vars missing from README.md: {missing}"


def test_no_under_construction_leftovers() -> None:
    assert "en construcción" not in README.lower()


def test_every_repository_path_and_relative_link_exists() -> None:
    mentioned = {match.replace("\\", "/") for match in _REPO_PATH.findall(README)}
    mentioned |= {link.split("#", 1)[0] for link in _RELATIVE_LINK.findall(README)}
    mentioned.discard("")
    assert "scripts/build_mcpb.sh" in mentioned and "LICENSE" in mentioned
    missing = sorted(path for path in mentioned if not (REPO_ROOT / path).exists())
    assert not missing, f"README.md mentions paths that do not exist: {missing}"


def test_in_page_anchors_point_to_headings() -> None:
    def slug(heading: str) -> str:
        text = re.sub(r"[^\w\s-]", "", heading.strip().lower())
        return "#" + re.sub(r"\s", "-", text)

    prose = re.sub(r"^```.*?^```", "", README, flags=re.MULTILINE | re.DOTALL)
    headings = {slug(line.lstrip("#")) for line in prose.splitlines() if line.startswith("#")}
    assert "#-índice-local-de-sumarios" in headings
    anchors = set(re.findall(r"\]\((#[^)\s]+)\)", README))
    missing = sorted(anchors - headings)
    assert not missing, f"README.md anchors without a heading: {missing}"
