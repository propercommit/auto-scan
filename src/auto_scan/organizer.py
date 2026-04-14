"""PDF creation and folder organization."""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import img2pdf
import pikepdf

from auto_scan.analyzer import DocumentInfo
from auto_scan.config import Config

_HAS_OCRMYPDF = shutil.which("ocrmypdf") is not None


def sanitize_name(name: str) -> str:
    """Sanitize a folder or file name to prevent path traversal and bad characters.

    Strips path separators, .., and characters illegal on common filesystems.
    Returns a safe, non-empty string.
    """
    # Remove any path separators and null bytes
    name = name.replace("/", "_").replace("\\", "_").replace("\0", "")
    # Remove .. components
    name = name.replace("..", "_")
    # Strip leading dots (hidden files), underscores, and whitespace
    name = name.lstrip("._ \t")
    # Remove characters illegal on Windows/macOS: < > : " | ? *
    name = re.sub(r'[<>:"|?*]', '_', name)
    # Collapse runs of underscores/spaces
    name = re.sub(r'[_\s]+', '_', name)
    name = name.strip("_ ")
    return name or "document"


def _ocr_pdf(path: Path) -> None:
    """Add a searchable text layer to a PDF in-place using ocrmypdf."""
    if not _HAS_OCRMYPDF:
        return
    try:
        subprocess.run(
            ["ocrmypdf", "--skip-text", "--quiet", str(path), str(path)],
            check=True, timeout=120,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"  OCR warning: {e}", file=sys.stderr)


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
    folder_name = sanitize_name(folder or doc_info.category)
    category_dir = config.output_dir / folder_name
    category_dir.mkdir(parents=True, exist_ok=True)

    pdf_bytes = img2pdf.convert(images)

    embed_tags = tags if tags is not None else [doc_info.category]
    pdf_bytes = _embed_tags(pdf_bytes, embed_tags, doc_info.summary)

    # Sanitize and resolve filename collisions
    filename = sanitize_name(doc_info.filename.removesuffix(".pdf")) + ".pdf"

    output_path = category_dir / filename
    counter = 2
    while output_path.exists():
        stem = filename.removesuffix(".pdf")
        output_path = category_dir / f"{stem}_{counter}.pdf"
        counter += 1

    output_path.write_bytes(pdf_bytes)
    _ocr_pdf(output_path)

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
    _ocr_pdf(output_path)

    print(f"Saved: {output_path}", file=sys.stderr)
    return output_path
