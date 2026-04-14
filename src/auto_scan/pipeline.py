"""Orchestrates the scan -> analyze -> organize pipeline."""

from __future__ import annotations

import sys
from pathlib import Path

from auto_scan import ScannerBusyError
from auto_scan.analyzer import DocumentInfo, analyze_document
from auto_scan.config import Config
from auto_scan.organizer import save_document, save_unclassified
from auto_scan.scanner.discovery import ScannerInfo, discover_scanner, scanner_info_from_ip
from auto_scan.scanner.escl import ESCLClient, ScanSettings, ScannerStatus


def get_scanner(config: Config) -> ScannerInfo:
    """Discover or connect to the scanner based on config."""
    if config.scanner_ip:
        info = scanner_info_from_ip(config.scanner_ip)
        print(f"Using scanner at {info.ip}", file=sys.stderr)
        return info
    return discover_scanner()


def check_status(client: ESCLClient, config: Config) -> ScannerStatus:
    """Check scanner status and warn about ADF state."""
    status = client.get_status()

    if status.state != "Idle":
        raise ScannerBusyError(f"Scanner is {status.state}. Wait and try again.")

    if config.scan_source == "Feeder" and status.adf_state == "ScannerAdfEmpty":
        print(
            "Warning: ADF appears empty. Load documents or use --flatbed.",
            file=sys.stderr,
        )

    return status


def run_scan(config: Config, classify: bool = True, dry_run: bool = False) -> Path | None:
    """Full pipeline: discover scanner, scan, analyze, save.

    Returns the output file path, or None for dry-run.
    """
    # 1. Find the scanner
    scanner_info = get_scanner(config)

    # 2. Connect and scan
    with ESCLClient(scanner_info.base_url) as client:
        check_status(client, config)

        settings = ScanSettings(
            source=config.scan_source,
            color_mode=config.color_mode,
            resolution=config.resolution,
            document_format=config.scan_format,
        )
        images = client.scan(settings)

    # 3. Classify (or skip)
    if not classify:
        if dry_run:
            print("Dry run: would save as unsorted scan.", file=sys.stderr)
            return None
        return save_unclassified(images, config)

    doc_info = analyze_document(images, config)

    if dry_run:
        print("\nDry run — would save as:", file=sys.stderr)
        print(f"  {config.output_dir / doc_info.category / doc_info.filename}", file=sys.stderr)
        return None

    # 4. Save
    return save_document(images, doc_info, config)


def show_status(config: Config) -> None:
    """Print scanner status and exit."""
    scanner_info = get_scanner(config)

    with ESCLClient(scanner_info.base_url) as client:
        status = client.get_status()
        caps = client.get_capabilities()

    print(f"Scanner:      {scanner_info.name}")
    print(f"Address:      {scanner_info.base_url}")
    print(f"State:        {status.state}")
    print(f"ADF:          {status.adf_state or 'N/A'}")
    print(f"Resolutions:  {caps.resolutions}")
    print(f"Color modes:  {caps.color_modes}")
    print(f"Sources:      {caps.sources}")
    print(f"Formats:      {caps.formats}")


def show_discover(config: Config) -> None:
    """Discover and print scanner info, then exit."""
    scanner_info = get_scanner(config)
    print(f"Scanner:  {scanner_info.name}")
    print(f"IP:       {scanner_info.ip}")
    print(f"Port:     {scanner_info.port}")
    print(f"Base URL: {scanner_info.base_url}")
