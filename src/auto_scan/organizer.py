"""PDF creation and folder organization."""

from __future__ import annotations

import io
import sys
from datetime import datetime
from pathlib import Path

import img2pdf
import pikepdf

from auto_scan.analyzer import DocumentInfo
from auto_scan.config import Config


def _embed_tags(pdf_bytes: bytes, tags: list[str], summary: str = "") -> bytes:
    """Write tags into PDF metadata Keywords field."""
    if not tags:
        return pdf_bytes
    buf = io.BytesIO(pdf_bytes)
    with pikepdf.open(buf) as pdf:
        with pdf.open_metadata() as meta:
            meta["dc:subject"] = tags
            if summary:
                meta["dc:description"] = summary
        out = io.BytesIO()
        pdf.save(out)
        return out.getvalue()


def save_document(
    images: list[bytes],
    doc_info: DocumentInfo,
    config: Config,
    folder: str | None = None,
    tags: list[str] | None = None,
) -> Path:
    """Create a PDF from scanned images and save it in the chosen folder.

    Args:
        folder: Subfolder name inside output_dir.  Falls back to doc_info.category.
        tags: Metadata tags to embed in the PDF.  Falls back to [doc_info.category].
    """
    folder_name = folder or doc_info.category
    category_dir = config.output_dir / folder_name
    category_dir.mkdir(parents=True, exist_ok=True)

    pdf_bytes = img2pdf.convert(images)

    embed_tags = tags if tags is not None else [doc_info.category]
    pdf_bytes = _embed_tags(pdf_bytes, embed_tags, doc_info.summary)

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
    if embed_tags:
        print(f"  Tags: {', '.join(embed_tags)}", file=sys.stderr)
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
