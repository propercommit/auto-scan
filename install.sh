#!/usr/bin/env bash
set -euo pipefail

# ── Colors ───────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${BLUE}==>${NC} ${BOLD}$*${NC}"; }
success() { echo -e "${GREEN}==>${NC} ${BOLD}$*${NC}"; }
warn()    { echo -e "${YELLOW}==>${NC} $*"; }
error()   { echo -e "${RED}==>${NC} $*" >&2; }

# ── Check Python ─────────────────────────────────────────────────────
info "Checking Python version..."

PYTHON=""
for cmd in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        major=$("$cmd" -c 'import sys; print(sys.version_info.major)')
        minor=$("$cmd" -c 'import sys; print(sys.version_info.minor)')
        if [ "$major" -ge 3 ] && [ "$minor" -ge 9 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    error "Python 3.9 or newer is required but not found."
    echo ""
    echo "Install Python with Homebrew:"
    echo "  brew install python@3.12"
    echo ""
    echo "Or download from https://www.python.org/downloads/"
    exit 1
fi

success "Found $PYTHON ($version)"

# ── Create virtual environment ───────────────────────────────────────
info "Creating virtual environment..."

if [ -d ".venv" ]; then
    warn "Virtual environment already exists, recreating..."
    rm -rf .venv
fi

"$PYTHON" -m venv .venv
source .venv/bin/activate

# ── Install dependencies ─────────────────────────────────────────────
info "Upgrading pip..."
pip install --upgrade pip --quiet

info "Installing auto-scan with GUI..."
pip install -e ".[gui]" --quiet

success "All dependencies installed"

# ── Set up configuration ─────────────────────────────────────────────
if [ ! -f ".env" ]; then
    info "Setting up configuration..."
    cp .env.example .env

    echo ""
    echo -e "${BOLD}An Anthropic API key is required for AI document classification.${NC}"
    echo "Get one at: https://console.anthropic.com"
    echo ""
    read -rp "Enter your Anthropic API key (or press Enter to skip): " api_key

    if [ -n "$api_key" ]; then
        sed -i '' "s|ANTHROPIC_API_KEY=sk-ant-...|ANTHROPIC_API_KEY=$api_key|" .env
        success "API key saved to .env"
    else
        warn "Skipped. Edit .env later to add your API key."
    fi

    echo ""
    read -rp "Enter your scanner's IP address (or press Enter for auto-discover): " scanner_ip

    if [ -n "$scanner_ip" ]; then
        sed -i '' "s|# SCANNER_IP=192.168.1.100|SCANNER_IP=$scanner_ip|" .env
        success "Scanner IP saved to .env"
    else
        warn "Will auto-discover the scanner on the network."
    fi
else
    warn ".env already exists, skipping configuration setup"
fi

# ── Verify installation ──────────────────────────────────────────────
info "Verifying installation..."

if command -v auto-scan &>/dev/null && command -v auto-scan-gui &>/dev/null; then
    success "Installation complete!"
else
    error "Installation verification failed. Try running: pip install -e '.[gui]'"
    exit 1
fi

# ── Create convenience launcher scripts ──────────────────────────────
info "Creating launcher scripts..."

cat > run-gui.command << 'LAUNCHER'
#!/usr/bin/env bash
cd "$(dirname "$0")"
source .venv/bin/activate
auto-scan-gui
LAUNCHER
chmod +x run-gui.command

cat > run-scan.command << 'LAUNCHER'
#!/usr/bin/env bash
cd "$(dirname "$0")"
source .venv/bin/activate
auto-scan "$@"
LAUNCHER
chmod +x run-scan.command

# ── Done ─────────────────────────────────────────────────────────────
echo ""
echo "================================================"
success "Auto-Scan is ready!"
echo "================================================"
echo ""
echo "  Quick start:"
echo ""
echo "    Open the GUI:"
echo "      Double-click run-gui.command in Finder"
echo "      Or run: source .venv/bin/activate && auto-scan-gui"
echo ""
echo "    Scan from terminal:"
echo "      source .venv/bin/activate"
echo "      auto-scan --discover    # find your scanner"
echo "      auto-scan --status      # check scanner status"
echo "      auto-scan               # scan & classify a document"
echo ""
echo "    Scanned files saved to:"
echo "      ~/Documents/Scans/"
echo ""
echo "  See README.md for full documentation."
echo ""
