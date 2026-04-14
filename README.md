# Auto-Scan

Scan documents from a Canon GX7050 printer/scanner and automatically classify, name, and sort them into folders using AI.

## How It Works

1. Discovers your Canon GX7050 on the local network (or connects via IP)
2. Scans documents from the automatic document feeder (ADF) or flatbed
3. Sends the scanned image to Claude Vision AI for analysis
4. Classifies the document (invoice, receipt, contract, letter, medical, tax, etc.)
5. Generates a descriptive filename based on the document content
6. Saves the document as a PDF in a categorized folder

## Prerequisites

- **macOS** (tested on macOS Sequoia)
- **Python 3.9 or newer**
- **Canon GX7050** powered on and connected to the same Wi-Fi network as your computer
- **Anthropic API key** — sign up at https://console.anthropic.com to get one

## Installation

### Option A: Automatic (Recommended)

Run the installer script:

```bash
git clone git@github.com:propercommit/auto-scan.git
cd auto-scan
./install.sh
```

The installer will:
- Check that Python 3.9+ is available
- Create a virtual environment
- Install all dependencies (including the GUI)
- Help you set up your API key
- Verify the installation

### Option B: Manual

```bash
git clone git@github.com:propercommit/auto-scan.git
cd auto-scan
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[gui]"
cp .env.example .env
```

Then edit `.env` with a text editor and set your API key:

```
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

## Getting Started

### Step 1: Activate the environment

Every time you open a new terminal, activate the virtual environment first:

```bash
cd auto-scan
source .venv/bin/activate
```

### Step 2: Connect to your scanner

Make sure your Canon GX7050 is turned on and connected to Wi-Fi, then test the connection:

```bash
auto-scan --discover
```

You should see something like:

```
Searching for Canon scanner on the network...
Found: Canon MAXIFY GX7050 at 192.168.1.42
Scanner:  Canon MAXIFY GX7050
IP:       192.168.1.42
Port:     443
Base URL: https://192.168.1.42:443/eSCL
```

If auto-discovery doesn't work, you can set the scanner's IP address manually in `.env`:

```
SCANNER_IP=192.168.1.42
```

(Find the IP on the printer's LCD screen under Network Settings.)

### Step 3: Check scanner status

```bash
auto-scan --status
```

This shows the scanner state, ADF status, supported resolutions, and color modes.

### Step 4: Scan your first document

Place a document in the automatic document feeder (ADF) on top of the printer, then run:

```bash
auto-scan
```

The program will:
1. Connect to the scanner
2. Scan all pages from the feeder
3. Send the scanned images to Claude AI for analysis
4. Save the classified PDF

You'll see output like:

```
Searching for Canon scanner on the network...
Found: Canon MAXIFY GX7050 at 192.168.1.42
Scanning...
  Page 1 scanned
  Page 2 scanned
Scan complete: 2 page(s)
Analyzing document with AI...
  Category: invoice
  Filename: 2025-03-15_invoice_acme_corp.pdf
  Summary:  Invoice #1234 from Acme Corp for consulting services
Saved: /Users/you/Documents/Scans/invoice/2025-03-15_invoice_acme_corp.pdf

/Users/you/Documents/Scans/invoice/2025-03-15_invoice_acme_corp.pdf
```

### Step 5: Try the GUI

For a graphical interface:

```bash
auto-scan-gui
```

In the GUI:
1. Click **Connect** (or enter the scanner IP first)
2. Choose your settings (source, resolution, color)
3. Click **Scan & Classify** to scan and auto-sort with AI
4. Click **Scan Only** to save without classification

## Usage Reference

### GUI

```bash
auto-scan-gui
```

### CLI Commands

```bash
# Scan from ADF and auto-classify (default)
auto-scan

# Scan from flatbed glass
auto-scan --flatbed

# Scan in grayscale
auto-scan --grayscale

# Scan at 600 DPI
auto-scan --resolution 600

# Scan without AI classification (saves to unsorted/ folder)
auto-scan --no-classify

# Preview what would happen without saving
auto-scan --dry-run

# Check scanner status
auto-scan --status

# Discover scanner on network
auto-scan --discover

# Save to a custom directory
auto-scan --output-dir /path/to/folder
```

## Output Folder Structure

Scanned documents are saved to `~/Documents/Scans/` by default, organized by category:

```
~/Documents/Scans/
  invoice/
    2025-03-15_invoice_acme_corp.pdf
    2025-03-10_invoice_electric_company.pdf
  receipt/
    2025-03-12_receipt_amazon_order.pdf
  contract/
    2025-02-01_contract_apartment_lease.pdf
  letter/
  medical/
  tax/
  insurance/
  bank/
  government/
  personal/
  manual/
  other/
  unsorted/        (used by --no-classify mode)
```

## Configuration

All settings can be configured in the `.env` file:

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | (required) | Your Anthropic API key |
| `SCANNER_IP` | (auto-discover) | Scanner IP address, skips network discovery |
| `OUTPUT_DIR` | `~/Documents/Scans` | Where to save scanned PDFs |
| `SCAN_RESOLUTION` | `300` | Scan resolution in DPI |
| `SCAN_COLOR_MODE` | `RGB24` | `RGB24` for color, `Grayscale8` for grayscale |
| `SCAN_SOURCE` | `Feeder` | `Feeder` for ADF, `Platen` for flatbed |
| `CLAUDE_MODEL` | `claude-sonnet-4-20250514` | Claude model to use for analysis |

## Troubleshooting

**"Could not find a Canon scanner on the network"**
- Make sure the printer is turned on and connected to Wi-Fi
- Check that your computer is on the same network
- Try setting `SCANNER_IP` manually in `.env`

**"ANTHROPIC_API_KEY is required"**
- Edit `.env` and add your API key from https://console.anthropic.com

**"Scanner is Processing"**
- The scanner is busy with another job. Wait a moment and try again.

**"ADF appears empty"**
- Load documents face-up in the top feeder tray
- Or use `--flatbed` to scan from the glass

**GUI doesn't open**
- Make sure you installed with `pip install -e ".[gui]"`
- On macOS, you may need to allow the terminal app access in System Preferences > Privacy & Security
