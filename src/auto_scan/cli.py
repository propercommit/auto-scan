"""CLI entry point for auto-scan."""

from __future__ import annotations

import argparse
import sys

from auto_scan import AutoScanError
from auto_scan.config import load_config
from auto_scan.pipeline import run_scan, show_discover, show_status


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="auto-scan",
        description="Scan documents from Canon GX7050 and auto-classify with AI",
    )

    # Scan options
    parser.add_argument(
        "--flatbed",
        action="store_true",
        help="scan from flatbed instead of ADF",
    )
    parser.add_argument(
        "--grayscale",
        action="store_true",
        help="scan in grayscale",
    )
    parser.add_argument(
        "--resolution",
        type=int,
        help="scan resolution in DPI (default: 300)",
    )
    parser.add_argument(
        "--output-dir",
        help="override output directory",
    )

    # Mode flags
    parser.add_argument(
        "--no-classify",
        action="store_true",
        help="save scan without AI classification",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="scan and analyze but don't save",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="show scanner status and exit",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="discover scanner on network and exit",
    )

    args = parser.parse_args()

    # Build config overrides from CLI args
    overrides: dict = {}
    if args.flatbed:
        overrides["scan_source"] = "Platen"
    if args.grayscale:
        overrides["color_mode"] = "Grayscale8"
    if args.resolution:
        overrides["resolution"] = args.resolution
    if args.output_dir:
        overrides["output_dir"] = args.output_dir

    try:
        config = load_config(**overrides)

        if args.discover:
            show_discover(config)
        elif args.status:
            show_status(config)
        else:
            result = run_scan(
                config,
                classify=not args.no_classify,
                dry_run=args.dry_run,
            )
            if result:
                print(f"\n{result}")
    except AutoScanError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
