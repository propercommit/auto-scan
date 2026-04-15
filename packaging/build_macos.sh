#!/usr/bin/env bash
#
# Build Auto Scan as a macOS .app bundle inside a .dmg
#
# Prerequisites:
#   - macOS with Xcode command line tools
#   - Python 3.9+ with pip
#   - Run from the project root: ./packaging/build_macos.sh
#
# Output:
#   dist/Auto Scan.app     — standalone macOS application
#   dist/AutoScan.dmg      — distributable disk image
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD_DIR="$PROJECT_DIR/build"
DIST_DIR="$PROJECT_DIR/dist"
VENV_DIR="$BUILD_DIR/venv-build"

APP_NAME="Auto Scan"
DMG_NAME="AutoScan"
VERSION=$(python3 -c "
import re
text = open('$PROJECT_DIR/pyproject.toml').read()
print(re.search(r'version\s*=\s*\"([^\"]+)\"', text).group(1))
")

echo "=== Building Auto Scan v${VERSION} for macOS ==="
echo ""

# ── Step 1: Clean previous build ────────────────────────────────
echo "Step 1: Cleaning previous build..."
rm -rf "$DIST_DIR/${APP_NAME}.app" "$DIST_DIR/${DMG_NAME}.dmg"
rm -rf "$BUILD_DIR/auto_scan_gui"

# ── Step 2: Create build virtualenv ─────────────────────────────
echo "Step 2: Setting up build environment..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
pip install --upgrade pip -q
pip install -e "$PROJECT_DIR" -q
pip install "pyinstaller>=6,<7" -q

# ── Step 3: Generate icons ──────────────────────────────────────
echo "Step 3: Generating icons..."
ICON_ICNS="$SCRIPT_DIR/icon.icns"
if [ ! -f "$ICON_ICNS" ]; then
    python "$SCRIPT_DIR/icon_gen.py"
fi

# ── Step 4: PyInstaller build ───────────────────────────────────
echo "Step 4: Building with PyInstaller..."

pyinstaller \
    --name "$APP_NAME" \
    --windowed \
    --icon "$ICON_ICNS" \
    --noconfirm \
    --clean \
    --distpath "$DIST_DIR" \
    --workpath "$BUILD_DIR" \
    --specpath "$BUILD_DIR" \
    \
    --hidden-import auto_scan \
    --hidden-import auto_scan.gui \
    --hidden-import auto_scan.cli \
    --hidden-import auto_scan.config \
    --hidden-import auto_scan.pipeline \
    --hidden-import auto_scan.organizer \
    --hidden-import auto_scan.recognition \
    --hidden-import auto_scan.recognition.engine \
    --hidden-import auto_scan.recognition.prompts \
    --hidden-import auto_scan.scanner \
    --hidden-import auto_scan.scanner.discovery \
    --hidden-import auto_scan.scanner.escl \
    --hidden-import auto_scan.redactor \
    --hidden-import auto_scan.dedup \
    --hidden-import auto_scan.history \
    --hidden-import auto_scan.image_utils \
    --hidden-import auto_scan.settings \
    --hidden-import auto_scan.usage \
    --hidden-import auto_scan.analyzer \
    \
    --hidden-import flask \
    --hidden-import flask.json \
    --hidden-import jinja2 \
    --hidden-import markupsafe \
    --hidden-import werkzeug \
    --hidden-import anthropic \
    --hidden-import httpx \
    --hidden-import httpcore \
    --hidden-import h11 \
    --hidden-import h2 \
    --hidden-import hpack \
    --hidden-import hyperframe \
    --hidden-import anyio \
    --hidden-import sniffio \
    --hidden-import certifi \
    --hidden-import idna \
    --hidden-import zeroconf \
    --hidden-import PIL \
    --hidden-import PIL.Image \
    --hidden-import PIL.ImageDraw \
    --hidden-import PIL.ImageFont \
    --hidden-import img2pdf \
    --hidden-import pikepdf \
    --hidden-import dotenv \
    --hidden-import pytesseract \
    --hidden-import pydantic \
    --hidden-import tokenizers \
    \
    --collect-all anthropic \
    --collect-all zeroconf \
    --collect-all pikepdf \
    \
    "$SCRIPT_DIR/entrypoint.py"

echo "  Built: $DIST_DIR/${APP_NAME}.app"

# ── Step 5: Set proper Info.plist values ────────────────────────
echo "Step 5: Updating Info.plist..."
PLIST="$DIST_DIR/${APP_NAME}.app/Contents/Info.plist"
if [ -f "$PLIST" ]; then
    /usr/libexec/PlistBuddy -c "Set :CFBundleName 'Auto Scan'" "$PLIST" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName 'Auto Scan'" "$PLIST" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString '$VERSION'" "$PLIST" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Set :CFBundleVersion '$VERSION'" "$PLIST" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier 'com.autoscan.app'" "$PLIST" 2>/dev/null || true
    # Allow network access (scanner discovery + API calls)
    /usr/libexec/PlistBuddy -c "Add :NSLocalNetworkUsageDescription string 'Auto Scan needs network access to discover and communicate with your scanner.'" "$PLIST" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Add :NSAppTransportSecurity:NSAllowsArbitraryLoads bool true" "$PLIST" 2>/dev/null || true
fi

# ── Step 6: Create .dmg ─────────────────────────────────────────
echo "Step 6: Creating DMG..."
DMG_PATH="$DIST_DIR/${DMG_NAME}-${VERSION}.dmg"
DMG_TEMP="$BUILD_DIR/dmg_temp"

rm -rf "$DMG_TEMP"
mkdir -p "$DMG_TEMP"
cp -R "$DIST_DIR/${APP_NAME}.app" "$DMG_TEMP/"

# Create Applications symlink for drag-to-install
ln -sf /Applications "$DMG_TEMP/Applications"

# Create the DMG
hdiutil create \
    -volname "$APP_NAME" \
    -srcfolder "$DMG_TEMP" \
    -ov \
    -format UDZO \
    "$DMG_PATH"

rm -rf "$DMG_TEMP"
echo "  Created: $DMG_PATH"

# ── Done ────────────────────────────────────────────────────────
echo ""
echo "=== Build complete ==="
echo "  App:  $DIST_DIR/${APP_NAME}.app"
echo "  DMG:  $DMG_PATH"
echo ""
echo "To test: open \"$DIST_DIR/${APP_NAME}.app\""
echo ""
echo "Note: Users need to set ANTHROPIC_API_KEY in the app settings"
echo "      on first launch (or in ~/.auto_scan/.env)."
