#!/usr/bin/env bash
# Build the bome-navaja .mcpb bundle: stage inputs, validate the manifest, pack.
#
# Usage: scripts/build_mcpb.sh [staging-dir] [output.mcpb]
# Requires: uv, and node/npx on PATH (the mcpb CLI is fetched on demand via npx).
# Windows equivalent: scripts/build_mcpb.ps1
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

VERSION="$(uv run python scripts/build_mcpb.py --print-version)"
OUT_BUNDLE="${1:-build/mcpb}"
OUT_MCPB="${2:-dist/bome-navaja-${VERSION}.mcpb}"

echo "==> staging bundle inputs into $OUT_BUNDLE"
uv run python scripts/build_mcpb.py --out "$OUT_BUNDLE"

echo "==> validating manifest"
npx -y @anthropic-ai/mcpb validate "$OUT_BUNDLE/manifest.json"

echo "==> packing"
mkdir -p "$(dirname "$OUT_MCPB")"
npx -y @anthropic-ai/mcpb pack "$OUT_BUNDLE" "$OUT_MCPB"

echo "==> done: $OUT_MCPB"
