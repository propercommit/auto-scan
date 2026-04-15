#
# Build Auto Scan as a Windows .exe
#
# Prerequisites:
#   - Windows 10/11 with Python 3.9+ installed
#   - Run from the project root: powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#
# Output:
#   dist\Auto Scan\Auto Scan.exe  — standalone Windows application
#   dist\AutoScan-Setup.exe       — (optional, if Inno Setup installed)
#
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$BuildDir = Join-Path $ProjectDir "build"
$DistDir = Join-Path $ProjectDir "dist"
$VenvDir = Join-Path $BuildDir "venv-build"

$AppName = "Auto Scan"
$Version = (Select-String -Path (Join-Path $ProjectDir "pyproject.toml") -Pattern 'version\s*=\s*"([^"]+)"').Matches.Groups[1].Value

Write-Host "=== Building Auto Scan v$Version for Windows ===" -ForegroundColor Cyan
Write-Host ""

# ── Step 1: Clean previous build ────────────────────────────────
Write-Host "Step 1: Cleaning previous build..." -ForegroundColor Yellow
if (Test-Path (Join-Path $DistDir $AppName)) { Remove-Item -Recurse -Force (Join-Path $DistDir $AppName) }

# ── Step 2: Create build virtualenv ─────────────────────────────
Write-Host "Step 2: Setting up build environment..." -ForegroundColor Yellow
if (-not (Test-Path $VenvDir)) {
    python -m venv $VenvDir
}
& "$VenvDir\Scripts\Activate.ps1"
pip install --upgrade pip -q
pip install -e $ProjectDir -q
pip install "pyinstaller>=6,<7" -q

# ── Step 3: Generate icons ──────────────────────────────────────
Write-Host "Step 3: Generating icons..." -ForegroundColor Yellow
$IconIco = Join-Path $ScriptDir "icon.ico"
if (-not (Test-Path $IconIco)) {
    python (Join-Path $ScriptDir "icon_gen.py")
}

# ── Step 4: PyInstaller build ───────────────────────────────────
Write-Host "Step 4: Building with PyInstaller..." -ForegroundColor Yellow

$entrypoint = Join-Path $ScriptDir "entrypoint.py"

pyinstaller `
    --name $AppName `
    --windowed `
    --icon $IconIco `
    --noconfirm `
    --clean `
    --distpath $DistDir `
    --workpath $BuildDir `
    --specpath $BuildDir `
    `
    --hidden-import auto_scan `
    --hidden-import auto_scan.gui `
    --hidden-import auto_scan.cli `
    --hidden-import auto_scan.config `
    --hidden-import auto_scan.pipeline `
    --hidden-import auto_scan.organizer `
    --hidden-import auto_scan.recognition `
    --hidden-import auto_scan.recognition.engine `
    --hidden-import auto_scan.recognition.prompts `
    --hidden-import auto_scan.scanner `
    --hidden-import auto_scan.scanner.discovery `
    --hidden-import auto_scan.scanner.escl `
    --hidden-import auto_scan.redactor `
    --hidden-import auto_scan.dedup `
    --hidden-import auto_scan.history `
    --hidden-import auto_scan.image_utils `
    --hidden-import auto_scan.settings `
    --hidden-import auto_scan.usage `
    --hidden-import auto_scan.analyzer `
    `
    --hidden-import flask `
    --hidden-import flask.json `
    --hidden-import jinja2 `
    --hidden-import markupsafe `
    --hidden-import werkzeug `
    --hidden-import anthropic `
    --hidden-import httpx `
    --hidden-import httpcore `
    --hidden-import h11 `
    --hidden-import h2 `
    --hidden-import hpack `
    --hidden-import hyperframe `
    --hidden-import anyio `
    --hidden-import sniffio `
    --hidden-import certifi `
    --hidden-import idna `
    --hidden-import zeroconf `
    --hidden-import PIL `
    --hidden-import PIL.Image `
    --hidden-import PIL.ImageDraw `
    --hidden-import PIL.ImageFont `
    --hidden-import img2pdf `
    --hidden-import pikepdf `
    --hidden-import dotenv `
    --hidden-import pytesseract `
    --hidden-import pydantic `
    --hidden-import tokenizers `
    `
    --collect-all anthropic `
    --collect-all zeroconf `
    --collect-all pikepdf `
    `
    $entrypoint

$ExePath = Join-Path $DistDir "$AppName\$AppName.exe"
Write-Host "  Built: $ExePath" -ForegroundColor Green

# ── Step 5: Create version info ─────────────────────────────────
Write-Host ""
Write-Host "=== Build complete ===" -ForegroundColor Cyan
Write-Host "  Exe: $ExePath"
Write-Host ""
Write-Host "To test: Start-Process '$ExePath'"
Write-Host ""
Write-Host "Note: Users need to set ANTHROPIC_API_KEY in the app settings"
Write-Host "      on first launch (or in %USERPROFILE%\.auto_scan\.env)."

# ── Optional: Inno Setup installer ──────────────────────────────
$InnoCompiler = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
$InnoScript = Join-Path $ScriptDir "installer.iss"
if ((Test-Path $InnoCompiler) -and (Test-Path $InnoScript)) {
    Write-Host ""
    Write-Host "Step 5: Creating installer with Inno Setup..." -ForegroundColor Yellow
    & $InnoCompiler /DAppVersion=$Version $InnoScript
    Write-Host "  Created: $DistDir\AutoScan-${Version}-Setup.exe" -ForegroundColor Green
}
