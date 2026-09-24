"""Entry point for the bome-navaja .mcpb bundle (Claude Desktop, uv runtime).

This file is staged into the bundle root by ``scripts/build_mcpb.py`` and is
executed by ``uv run --directory <bundle> bome_navaja_mcpb.py`` after uv has
installed the project from ``pyproject.toml``.
"""

from bome_navaja.server import main

main()
