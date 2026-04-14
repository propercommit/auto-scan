"""PDF creation and folder organization."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import img2pdf

from auto_scan.analyzer import DocumentInfo
from auto_scan.config import Config


def save_document(
    images: list[bytes], doc_info: DocumentInfo, config: Config
) -> Path:
    """Create a PDF from scanned images and save it in the categorized folder."""
    category_dir = config.output_dir / doc_info.category
    category_dir.mkdir(parents=True, exist_ok=True)

    # Generate PDF
    pdf_bytes = img2pdf.convert(images)

    # Resolve filename collisions
    filename = doc_info.filename
    if not filename.endswith(".pdf"):
        filename += ".pdf"

    output_path = category_dir / filename
    counter = 2
    while output_path.exists():
        stem = filename.removesuffix(".pdf")
        output_path = category_dir / f"{stem}_{counter}.pdf"
        counter += 1

    output_path.write_bytes(pdf_bytes)

    print(f"Saved: {output_path}", file=sys.stderr)
    return output_path


def save_unclassified(images: list[bytes], config: Config) -> Path:
    """Save a PDF without AI classification (--no-classify mode)."""
    unsorted_dir = config.output_dir / "unsorted"
    unsorted_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"{timestamp}_scan.pdf"
    output_path = unsorted_dir / filename

    pdf_bytes = img2pdf.convert(images)
    output_path.write_bytes(pdf_bytes)

    print(f"Saved: {output_path}", file=sys.stderr)
    return output_path
