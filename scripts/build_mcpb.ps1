# Build the bome-navaja .mcpb bundle on Windows: stage inputs, validate the manifest, pack.
#
# Usage: powershell -File scripts\build_mcpb.ps1 [-OutBundle build\mcpb] [-OutMcpb dist\bome-navaja-<version>.mcpb]
# Requires: uv, and node/npx on PATH (the mcpb CLI is fetched on demand via npx).
# Linux/macOS equivalent: scripts/build_mcpb.sh
param(
    [string]$OutBundle = 'build\mcpb',
    [string]$OutMcpb = ''
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$Version = (& uv run python scripts/build_mcpb.py --print-version)
if ($LASTEXITCODE -ne 0) { throw "reading the version failed (exit $LASTEXITCODE)" }
$Version = "$Version".Trim()
if (-not $OutMcpb) { $OutMcpb = "dist\bome-navaja-$Version.mcpb" }

Write-Host "==> staging bundle inputs into $OutBundle"
& uv run python scripts/build_mcpb.py --out $OutBundle
if ($LASTEXITCODE -ne 0) { throw "staging failed (exit $LASTEXITCODE)" }

Write-Host '==> validating manifest'
& npx -y @anthropic-ai/mcpb validate (Join-Path $OutBundle 'manifest.json')
if ($LASTEXITCODE -ne 0) { throw "mcpb validate failed (exit $LASTEXITCODE)" }

Write-Host '==> packing'
$OutDir = Split-Path -Parent $OutMcpb
if ($OutDir) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }
& npx -y @anthropic-ai/mcpb pack $OutBundle $OutMcpb
if ($LASTEXITCODE -ne 0) { throw "mcpb pack failed (exit $LASTEXITCODE)" }

Write-Host "==> done: $OutMcpb"
