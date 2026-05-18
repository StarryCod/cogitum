#!/usr/bin/env pwsh
# Cogitum Windows installer (PowerShell)
# Usage:
#   iwr https://raw.githubusercontent.com/StarryCod/cogitum/master/scripts/install.ps1 | iex
#
# Prerequisites:
#   - Python 3.11+ in PATH (python or py launcher)
#   - Git in PATH
#   - PowerShell 5.1+ (Windows 10/11 default)
#
# What it does:
#   1. Clones (or updates) the repo into %LOCALAPPDATA%\cogitum
#   2. Creates a venv inside that clone (%LOCALAPPDATA%\cogitum\.venv)
#   3. Installs cogitum + all extras into the venv
#   4. Writes cog.cmd and cogitum.cmd shims into a directory on PATH
#      (preferring %LOCALAPPDATA%\Microsoft\WindowsApps which is on PATH
#       by default on Windows 10/11)

$ErrorActionPreference = "Stop"

$Repo       = "https://github.com/StarryCod/cogitum.git"
$Branch     = "master"   # The Windows-native paths come from master too
$InstallDir = Join-Path $env:LOCALAPPDATA "cogitum"
$VenvDir    = Join-Path $InstallDir ".venv"
$ShimDir    = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps"

function Info  ($msg) { Write-Host "[cogitum] $msg" -ForegroundColor Green }
function Warn  ($msg) { Write-Host "[cogitum] $msg" -ForegroundColor Yellow }
function Error ($msg) { Write-Host "[cogitum] $msg" -ForegroundColor Red }

function Find-Python {
    # Prefer specific 3.11+ entries; fall back to whichever python resolves.
    $candidates = @("python3.13", "python3.12", "python3.11", "python", "py -3")
    foreach ($cmd in $candidates) {
        $parts = $cmd.Split(" ")
        $exe = $parts[0]
        $args = if ($parts.Length -gt 1) { $parts[1..($parts.Length-1)] } else { @() }
        try {
            $version = & $exe @args --version 2>&1
            if ($version -match "Python (\d+)\.(\d+)") {
                $major = [int]$Matches[1]; $minor = [int]$Matches[2]
                if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 11)) {
                    return @{ Cmd = $exe; Args = $args; Version = $version }
                }
            }
        } catch {
            continue
        }
    }
    return $null
}

# 1. Python check
$python = Find-Python
if (-not $python) {
    Error "Python 3.11+ is required but was not found in PATH."
    Error "Install from https://python.org/downloads or via: winget install Python.Python.3.13"
    exit 1
}
Info "Using Python: $($python.Version)"

# 2. Git check
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Error "Git is required but not in PATH. Install from https://git-scm.com or: winget install Git.Git"
    exit 1
}

# 3. Clone or update
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
if (Test-Path (Join-Path $InstallDir ".git")) {
    Info "Existing install found at $InstallDir — pulling latest..."
    git -C $InstallDir fetch --all --quiet
    git -C $InstallDir reset --hard "origin/$Branch" --quiet
} else {
    Info "Cloning repository to $InstallDir..."
    git clone --depth 1 --branch $Branch $Repo $InstallDir
    if ($LASTEXITCODE -ne 0) {
        Error "git clone failed."
        exit 1
    }
}

# 4. Create venv
if (-not (Test-Path $VenvDir)) {
    Info "Creating virtual environment..."
    & $python.Cmd @($python.Args) -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        Error "Failed to create virtual environment."
        exit 1
    }
}
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

# 5. Install dependencies
Info "Upgrading pip..."
& $VenvPython -m pip install --upgrade pip --quiet

Info "Installing cogitum and extras..."
# [all] pulls keyring, cryptography, argon2-cffi, mcp.
& $VenvPython -m pip install -e "$InstallDir[all]" --quiet
if ($LASTEXITCODE -ne 0) {
    Error "pip install failed. See the output above for details."
    exit 1
}

# 6. Write shim scripts
New-Item -ItemType Directory -Force -Path $ShimDir | Out-Null

$ShimContent = @"
@echo off
"$VenvPython" -m cogitum.cli %*
"@

Info "Installing launchers to $ShimDir..."
$CogShim = Join-Path $ShimDir "cog.cmd"
$CogitumShim = Join-Path $ShimDir "cogitum.cmd"
Set-Content -Path $CogShim -Value $ShimContent -Encoding ASCII
Set-Content -Path $CogitumShim -Value $ShimContent -Encoding ASCII

# Sanity-check that shim dir is on PATH.
$pathDirs = $env:Path -split ";" | ForEach-Object { $_.TrimEnd("\") }
if ($pathDirs -notcontains $ShimDir.TrimEnd("\")) {
    Warn "$ShimDir is not on your PATH."
    Warn "Add it via: setx PATH `"%PATH%;$ShimDir`""
    Warn "Or run cogitum directly: $VenvPython -m cogitum.cli"
}

Info "Done!"
Info "Run 'cog' or 'cogitum' to start."
Info "First-time setup: run 'cog setup' to configure providers."
Info ""
Info "For the Emperor!"
