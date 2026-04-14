# Auto-Scan

Scan documents from a Canon GX7050 printer/scanner and automatically classify, name, and sort them into folders using AI.

## How It Works

1. Discovers your Canon GX7050 on the local network (or connects via IP)
2. Scans documents from the automatic document feeder (ADF) or flatbed
3. Runs a local OCR privacy check to detect and redact sensitive data (SSN, IBAN, credit cards, etc.) before anything leaves your machine
4. Sends the redacted image to Claude Vision AI for analysis
5. Classifies the document (invoice, receipt, contract, letter, medical, tax, etc.)
6. Generates a descriptive filename based on the document content
7. Saves the original (unredacted) document as a PDF in a categorized folder

## Prerequisites

- **macOS** (tested on macOS Sequoia)
- **Python 3.9 or newer**
- **Canon GX7050** powered on and connected to the same Wi-Fi network as your computer
- **Anthropic API key** — sign up at https://console.anthropic.com to get one
- **Tesseract OCR** (optional, for sensitive data redaction) — `brew install tesseract`

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
- Install Tesseract OCR via Homebrew (for sensitive data redaction)
- Help you set up your API key
- Verify the installation

### Option B: Manual

```bash
git clone git@github.com:propercommit/auto-scan.git
cd auto-scan
brew install tesseract        # required for sensitive data redaction
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
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
3. Click **Scan & Classify** to scan and auto-sort with AI, or **Batch Scan** for multi-document ADF stacks
4. Watch the pipeline timeline as it scans, checks for sensitive data, analyzes with AI, and saves
5. Click **Scan Only** to save without classification

## Privacy & Sensitive Data Redaction

Auto-scan includes a local OCR privacy check that runs **before** any data is sent to the Claude API. It uses Tesseract OCR to detect text in scanned images and matches it against sensitive patterns:

- **SSN** (US Social Security numbers)
- **AHV/AVS** (Swiss social insurance numbers)
- **Credit card numbers**
- **IBAN** (international bank account numbers)
- **Phone numbers**, **email addresses**, **dates of birth**, **passport numbers** (optional)

When sensitive data is detected, the matching regions are blacked out in the image before it is sent to the AI. The original unredacted scan is preserved in the saved PDF.

The pipeline timeline in the GUI shows the OCR step in real time: you can see what was found and confirm before anything is sent. Pages and documents that were redacted are marked with a blue "OCR protected" badge in the batch modal.

### Settings

- **Redact sensitive data** (enabled by default) — toggle in Settings to enable/disable
- **Pattern selection** — choose which patterns to scan for
- **Test OCR** button — verify tesseract is installed and working with a real test image
- **Reckless mode** — skip the OCR preview and send directly to AI (redaction still runs if enabled)

### Requirements

Redaction requires Tesseract OCR installed on your system:

```bash
brew install tesseract
```

The installer (`./install.sh`) handles this automatically. If tesseract is not available, the privacy check step will be skipped and you'll see a warning.

## Batch Scanning

Batch mode scans a full stack from the ADF and uses AI to group pages into separate documents. In the review modal you can:

- Drag and drop pages between documents
- Move pages via a dropdown selector
- Add or remove document groups
- Edit filenames, folders, and tags per document
- See AI confidence scores per page and per document
- See which pages had sensitive data redacted (blue OCR badges)

## Usage Reference

### GUI

```bash
auto-scan-gui
```

The GUI shows a 4-step pipeline during scanning:

1. **Scan** — pages coming in from the scanner
2. **Privacy Check** — local OCR scanning for sensitive data, shows results and asks for confirmation
3. **AI Analysis** — document classification with Claude Vision
4. **Save** — writing files to disk

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

The GUI also has its own persistent settings (saved to `~/.auto_scan/settings.json`):

- **Daily budget** — cap API spending per day (resets at midnight)
- **Redaction toggle and patterns** — control sensitive data redaction
- **Reckless mode** — skip the OCR confirmation step

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

**"Redaction SKIPPED: tesseract is not installed"**
- Install Tesseract OCR: `brew install tesseract`
- Use the **Test OCR** button in Settings to verify it works

**OCR privacy check is slow**
- OCR processing time depends on page count and resolution. At 300 DPI, expect ~0.5-1s per page.
- Lower the resolution to 200 DPI for faster OCR if quality is acceptable
- Enable **Reckless mode** to skip the confirmation step (redaction still runs silently)
