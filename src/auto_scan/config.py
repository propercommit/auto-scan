"""Configuration loading and validation.

Precedence (highest to lowest):
  1. CLI flags (passed as keyword overrides to load_config)
  2. Environment variables (from shell or .env file)
  3. Hardcoded defaults (in this module)

This means `--resolution 600` beats SCAN_RESOLUTION=300 in .env.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv

# ── Allowed values ───────────────────────────────────────────────────
# These mirror the eSCL spec's supported values. Anything outside these
# sets is rejected early to avoid cryptic HTTP 4xx from the scanner.
VALID_COLOR_MODES = {"RGB24", "Grayscale8", "BlackAndWhite1"}
VALID_SOURCES = {"Feeder", "Platen"}
VALID_FORMATS = {"image/jpeg", "image/png", "application/pdf"}
MIN_RESOLUTION = 75    # below 75 DPI text is unreadable
MAX_RESOLUTION = 1200  # above 1200 DPI files become huge with no OCR benefit


@dataclass
class Config:
    api_key: str
    scanner_ip: str | None
    output_dir: Path
    resolution: int
    color_mode: str
    scan_source: str
    scan_format: str
    claude_model: str


# ── Validation helpers ───────────────────────────────────────────────

def _validate_ip(ip: str) -> bool:
    """Check if string is a plausible IPv4 address or hostname.

    Rejects anything with path separators or special characters to prevent
    URL injection into the eSCL base URL (e.g. "192.168.1.1/../../admin").
    """
    if not ip:
        return True
    # IPv4: each octet 0-255
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
        return all(0 <= int(p) <= 255 for p in ip.split("."))
    # Hostname: restrictive charset — no slashes, no spaces
    return bool(re.match(r"^[a-zA-Z0-9._-]+$", ip))


# ── Config loader ────────────────────────────────────────────────────

def load_config(**overrides) -> Config:
    """Load config from .env file and environment variables.

    Keyword arguments override environment values (used by CLI flags).
    Validates all values and raises ValueError for invalid input.
    """
    # load_dotenv merges .env into os.environ without overwriting existing
    # vars, so real env vars take precedence over the .env file.
    load_dotenv()

    # Each setting follows the same pattern: override > env var > default.
    api_key = overrides.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is required. Set it in .env or as an environment variable."
        )

    output_dir = overrides.get("output_dir") or os.environ.get(
        "OUTPUT_DIR", "~/Documents/Scans"
    )

    scanner_ip = overrides.get("scanner_ip") or os.environ.get("SCANNER_IP")
    if scanner_ip and not _validate_ip(scanner_ip):
        raise ValueError(f"Invalid scanner IP or hostname: {scanner_ip!r}")

    resolution = int(
        overrides.get("resolution") or os.environ.get("SCAN_RESOLUTION", "300")
    )
    if not MIN_RESOLUTION <= resolution <= MAX_RESOLUTION:
        raise ValueError(
            f"Resolution must be {MIN_RESOLUTION}–{MAX_RESOLUTION} DPI, got {resolution}"
        )

    color_mode = (
        overrides.get("color_mode")
        or os.environ.get("SCAN_COLOR_MODE", "RGB24")
    )
    if color_mode not in VALID_COLOR_MODES:
        raise ValueError(
            f"Invalid color mode {color_mode!r}. Valid: {', '.join(sorted(VALID_COLOR_MODES))}"
        )

    scan_source = (
        overrides.get("scan_source")
        or os.environ.get("SCAN_SOURCE", "Feeder")
    )
    if scan_source not in VALID_SOURCES:
        raise ValueError(
            f"Invalid scan source {scan_source!r}. Valid: {', '.join(sorted(VALID_SOURCES))}"
        )

    # scan_format is env-only (no CLI flag) because changing it rarely helps
    # and can cause issues with scanners that ignore the requested format.
    scan_format = os.environ.get("SCAN_FORMAT", "image/jpeg")
    if scan_format not in VALID_FORMATS:
        raise ValueError(
            f"Invalid scan format {scan_format!r}. Valid: {', '.join(sorted(VALID_FORMATS))}"
        )

    return Config(
        api_key=api_key,
        scanner_ip=scanner_ip or None,
        output_dir=Path(output_dir).expanduser(),
        resolution=resolution,
        color_mode=color_mode,
        scan_source=scan_source,
        scan_format=scan_format,
        claude_model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
    )
