# Build QwenPaw backend with PyInstaller for Tauri sidecar (Windows)
# Creates an onedir backend bundle with embedded Python runtime
#
# Usage:
#   powershell ./scripts/pack-tauri/build_pyinstaller.ps1
#
# Prerequisites:
#   - Python 3.10+ with virtual environment
#   - PyInstaller 6.0+ (will be installed if not present)

param()

$ErrorActionPreference = "Stop"
$REPO_ROOT = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $REPO_ROOT

$DIST = if ($env:DIST) { $env:DIST } else { "dist" }
if (-not [System.IO.Path]::IsPathRooted($DIST)) {
    $DIST = Join-Path $REPO_ROOT $DIST
}
$VERSION_FILE = "src\qwenpaw\__version__.py"

# Extract version
if (Test-Path $VERSION_FILE) {
    $content = Get-Content $VERSION_FILE -Raw
    if ($content -match '__version__\s*=\s*"([^"]+)"') {
        $VERSION = $Matches[1]
    } else {
        throw "Failed to extract version from $VERSION_FILE"
    }
} else {
    throw "Version file not found: $VERSION_FILE"
}

Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "QwenPaw PyInstaller Build - Windows" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "Version: $VERSION"
Write-Host "Repository: $REPO_ROOT"
Write-Host ""

# Check prerequisites
Write-Host "== Checking prerequisites ==" -ForegroundColor Yellow

$UV_BIN = (Get-Command uv -ErrorAction SilentlyContinue).Source
$PYTHON_BIN = Join-Path $REPO_ROOT ".venv\Scripts\python.exe"
if (-not (Test-Path $PYTHON_BIN)) {
    if ($UV_BIN) {
        Write-Host ".venv not found, creating virtual environment with uv" -ForegroundColor Yellow
        & $UV_BIN venv "$REPO_ROOT\.venv"
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to create virtual environment with uv"
        }
    } else {
        Write-Host ".venv not found, using system Python" -ForegroundColor Yellow
        $PYTHON_BIN = (Get-Command python -ErrorAction SilentlyContinue).Source
    }
    if (-not $PYTHON_BIN -or -not (Test-Path $PYTHON_BIN)) {
        Write-Host "ERROR: Python not found in .venv or PATH" -ForegroundColor Red
        Write-Host "Please create virtual environment first: python -m venv .venv"
        exit 1
    }
}

$pythonVersion = & $PYTHON_BIN --version
Write-Host "Python: $pythonVersion" -ForegroundColor Green

function Test-PythonImport {
    param([string]$Statement)
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $PYTHON_BIN -c $Statement *> $null
        return $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}

function Assert-LastExit {
    param([string]$Message)
    if ($LASTEXITCODE -ne 0) { throw $Message }
}

function Install-PythonPackages {
    param([string[]]$Packages)
    if ($UV_BIN) {
        & $UV_BIN pip install --python $PYTHON_BIN @Packages
    } else {
        & $PYTHON_BIN -m pip install @Packages
    }
    Assert-LastExit "Failed to install Python packages: $($Packages -join ', ')"
}

function Uninstall-PythonPackage {
    param([string]$Package)
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        if ($UV_BIN) {
            & $UV_BIN pip uninstall --python $PYTHON_BIN -y $Package *> $null
        } else {
            & $PYTHON_BIN -m pip uninstall -y $Package *> $null
        }
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}

# Install PyInstaller if not present
Write-Host "== Installing PyInstaller ==" -ForegroundColor Yellow
if (Test-PythonImport "import PyInstaller") {
    Write-Host "PyInstaller already installed" -ForegroundColor Green
} else {
    Write-Host "Installing PyInstaller..."
    Install-PythonPackages -Packages @("pyinstaller>=6.0.0")
    Write-Host "PyInstaller installed" -ForegroundColor Green
}

# Install python-dotenv if not present (required by PyInstaller collect_submodules)
if (Test-PythonImport "import dotenv") {
    Write-Host "python-dotenv already installed" -ForegroundColor Green
} else {
    Write-Host "Installing python-dotenv..."
    Install-PythonPackages -Packages @("python-dotenv")
    Write-Host "python-dotenv installed" -ForegroundColor Green
}

Write-Host ""

# Install project dependencies (ensures ALL runtime deps are importable)
Write-Host "== Installing project dependencies ==" -ForegroundColor Yellow
Install-PythonPackages -Packages @("-e", ".[full]")
Write-Host "Project dependencies installed with full extras" -ForegroundColor Green

# Fix agent-client-protocol namespace collision
# PyPI has an empty 'acp' stub that shadows the real package
if (-not (Test-PythonImport "from acp import Agent")) {
    Write-Host "Fixing agent-client-protocol namespace..."
    Uninstall-PythonPackage "acp"
    Install-PythonPackages -Packages @("agent-client-protocol")
    Write-Host "agent-client-protocol installed" -ForegroundColor Green
}

# ── Download and prepare Python embedded distribution ──────────────
# This standalone Python is bundled into the Tauri package so plugins
# can pip-install dependencies at runtime without touching the user's
# system Python.
Write-Host "== Preparing bundled Python embed ==" -ForegroundColor Yellow

$PY_EMBED_VERSION = "3.13.4"
$PY_EMBED_DIR = Join-Path $DIST "python-embed"
$PY_EMBED_ZIP = Join-Path $DIST "python-embed-download.zip"
$PY_EMBED_URL = "https://www.python.org/ftp/python/${PY_EMBED_VERSION}/python-${PY_EMBED_VERSION}-embed-amd64.zip"

if (Test-Path (Join-Path $PY_EMBED_DIR "python.exe")) {
    Write-Host "Bundled Python embed already prepared at $PY_EMBED_DIR" -ForegroundColor Green
} else {
    Write-Host "Downloading Python ${PY_EMBED_VERSION} embedded distribution..."
    if (Test-Path $PY_EMBED_DIR) { Remove-Item -Recurse -Force $PY_EMBED_DIR }
    New-Item -ItemType Directory -Force -Path $PY_EMBED_DIR | Out-Null

    # Download
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $PY_EMBED_URL -OutFile $PY_EMBED_ZIP -UseBasicParsing
    if (-not (Test-Path $PY_EMBED_ZIP)) {
        throw "Failed to download Python embedded distribution from $PY_EMBED_URL"
    }

    # Extract
    Expand-Archive -Path $PY_EMBED_ZIP -DestinationPath $PY_EMBED_DIR -Force
    Remove-Item -Force $PY_EMBED_ZIP
    Write-Host "Extracted Python embed to $PY_EMBED_DIR" -ForegroundColor Green

    # Enable import site so pip works: uncomment 'import site' in python*._pth
    $pthFiles = Get-ChildItem -Path $PY_EMBED_DIR -Filter "python*._pth"
    foreach ($pthFile in $pthFiles) {
        $content = Get-Content $pthFile.FullName -Raw
        $content = $content -replace '#\s*import site', 'import site'
        # Also add Lib\site-packages so pip-installed packages are importable
        if ($content -notmatch 'Lib\\site-packages') {
            $content = $content.TrimEnd() + "`nLib\site-packages`n"
        }
        Set-Content -Path $pthFile.FullName -Value $content -NoNewline
        Write-Host "  Patched $($pthFile.Name): enabled site + site-packages" -ForegroundColor Gray
    }

    # Install pip via get-pip.py
    Write-Host "Installing pip into bundled Python..."
    $getPipUrl = "https://bootstrap.pypa.io/get-pip.py"
    $getPipPath = Join-Path $PY_EMBED_DIR "get-pip.py"
    Invoke-WebRequest -Uri $getPipUrl -OutFile $getPipPath -UseBasicParsing
    $embedPython = Join-Path $PY_EMBED_DIR "python.exe"
    & $embedPython $getPipPath --no-warn-script-location
    Assert-LastExit "Failed to install pip into bundled Python"
    Remove-Item -Force $getPipPath
    Write-Host "pip installed into bundled Python" -ForegroundColor Green

    # Ensure build tools are available in the bundled Python so pyproject
    # backends like setuptools.build_meta can be imported when installing
    # packages from sdists.
    Write-Host "Upgrading pip, setuptools, and wheel in bundled Python..."
    & $embedPython -m pip install --upgrade pip setuptools wheel --no-warn-script-location
    Assert-LastExit "Failed to upgrade pip/setuptools/wheel in bundled Python"
    Write-Host "pip, setuptools, and wheel upgraded" -ForegroundColor Green

    # Pre-install plugin runtime dependencies so they are available
    # out of the box in the Tauri package.
    Write-Host "Pre-installing plugin dependencies into bundled Python..."
    & $embedPython -m pip install --no-warn-script-location `
        "pyside6-essentials>=6.6" `
        "fastapi>=0.110" `
        "uvicorn>=0.27" `
        "pillow>=10.0" `
        "python-multipart>=0.0.9" `
        "httpx>=0.27" `
        "httpx-sse>=0.4.0" `
        "dashscope>=1.25.16" `
        "iac-code>=0.1.2"
    Assert-LastExit "Failed to pre-install plugin dependencies"
    Write-Host "Plugin dependencies pre-installed" -ForegroundColor Green
}

Write-Host ""

# Run PyInstaller
Write-Host "== Running PyInstaller ==" -ForegroundColor Yellow
Write-Host "Building onedir backend bundle..."

$SPEC_FILE = Join-Path $REPO_ROOT "scripts\pack-tauri\qwenpaw.spec"
if (-not (Test-Path $SPEC_FILE)) {
    Write-Host "ERROR: Spec file not found at $SPEC_FILE" -ForegroundColor Red
    exit 1
}

& $PYTHON_BIN -m PyInstaller $SPEC_FILE `
    --distpath "${DIST}\pyinstaller" `
    --workpath "${DIST}\pyinstaller-build" `
    --clean `
    --noconfirm

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed"
}

Write-Host "PyInstaller build complete" -ForegroundColor Green
Write-Host ""

# Copy bundled Python embed into the PyInstaller output
$BACKEND_DIR = Join-Path $DIST "pyinstaller\qwenpaw-backend"
if (Test-Path $PY_EMBED_DIR) {
    Write-Host "== Copying bundled Python embed into output ==" -ForegroundColor Yellow
    $EMBED_DEST = Join-Path $BACKEND_DIR "python-embed"
    if (Test-Path $EMBED_DEST) { Remove-Item -Recurse -Force $EMBED_DEST }
    Copy-Item -Recurse -Force $PY_EMBED_DIR $EMBED_DEST
    $embedSize = (Get-ChildItem $EMBED_DEST -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB
    Write-Host "Bundled Python embed copied ($([math]::Round($embedSize, 2)) MB)" -ForegroundColor Green
    Write-Host ""
}

# Copy bundled plugins into the PyInstaller output
# These are the built-in plugins that ship with each release.  On startup
# the plugin loader compares versions and auto-upgrades the user's copies.
Write-Host "== Copying bundled plugins into output ==" -ForegroundColor Yellow
$BUNDLED_PLUGINS_SRC = Join-Path $REPO_ROOT "plugins\bundle"
$BUNDLED_PLUGINS_DEST = Join-Path $BACKEND_DIR "bundled-plugins"

if (Test-Path $BUNDLED_PLUGINS_SRC) {
    if (Test-Path $BUNDLED_PLUGINS_DEST) { Remove-Item -Recurse -Force $BUNDLED_PLUGINS_DEST }
    Copy-Item -Recurse -Force $BUNDLED_PLUGINS_SRC $BUNDLED_PLUGINS_DEST
    $pluginCount = (Get-ChildItem $BUNDLED_PLUGINS_DEST -Directory).Count
    $pluginsSize = (Get-ChildItem $BUNDLED_PLUGINS_DEST -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB
    Write-Host "Bundled $pluginCount plugin(s) copied ($([math]::Round($pluginsSize, 2)) MB)" -ForegroundColor Green
} else {
    Write-Host "WARNING: plugins/bundle directory not found at $BUNDLED_PLUGINS_SRC" -ForegroundColor Yellow
}
Write-Host ""

# Verify output
$BACKEND_EXE = Join-Path $BACKEND_DIR "qwenpaw-backend.exe"
$CLI_EXE = Join-Path $BACKEND_DIR "qwenpaw.exe"
if (-not (Test-Path $BACKEND_DIR)) {
    Write-Host "ERROR: Backend bundle directory not found at $BACKEND_DIR" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $BACKEND_EXE)) {
    Write-Host "ERROR: Backend executable not found at $BACKEND_EXE" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $CLI_EXE)) {
    Write-Host "ERROR: CLI executable not found at $CLI_EXE" -ForegroundColor Red
    exit 1
}

Write-Host "Backend bundle created: $BACKEND_DIR" -ForegroundColor Green

# Get size
$bundleSize = (Get-ChildItem $BACKEND_DIR -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB
Write-Host "Bundle size: $([math]::Round($bundleSize, 2)) MB"
Write-Host ""

# Copy to Tauri resources directory
Write-Host "== Copying to Tauri binaries directory ==" -ForegroundColor Yellow
$BINARIES_DIR = Join-Path $REPO_ROOT "console\src-tauri\binaries"
New-Item -ItemType Directory -Force -Path $BINARIES_DIR | Out-Null

$DEST = Join-Path $BINARIES_DIR "qwenpaw-backend"
New-Item -ItemType Directory -Force -Path $DEST | Out-Null
Get-ChildItem -LiteralPath $DEST -Force | Remove-Item -Recurse -Force
Copy-Item -Recurse -Force (Join-Path $BACKEND_DIR "*") $DEST
Write-Host "Copied to: $DEST" -ForegroundColor Green
Write-Host ""

Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "PyInstaller Build Complete!" -ForegroundColor Green
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "Output:"
Write-Host "  Bundle: $BACKEND_DIR"
Write-Host "  Tauri resource: $DEST"
Write-Host ""
