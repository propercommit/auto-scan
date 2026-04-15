"""PDF creation and folder organization.

Flow: JPEG pages -> img2pdf -> embed XMP metadata (pikepdf) -> OCR text layer
(ocrmypdf, optional) -> write to category subfolder -> set Finder tags (macOS).
"""

from __future__ import annotations

import io
import os
import platform
import plistlib
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

# Check once at import time whether ocrmypdf is installed. It is optional
# because it pulls in Tesseract (~200 MB); scans still work without it,
# they just won't have a searchable text layer.
_HAS_OCRMYPDF = shutil.which("ocrmypdf") is not None


# ── Path sanitization ────────────────────────────────────────────────
# The AI classifier returns freeform text for filenames and folder names.
# Threat model: a malicious document title could contain "../../../etc/cron.d/..."
# or null bytes to escape the output directory. We strip all path-significant
# characters so the final path is always a child of config.output_dir.

def sanitize_name(name: str) -> str:
    """Sanitize a folder or file name to prevent path traversal and bad characters.

    Strips path separators, .., and characters illegal on common filesystems.
    Returns a safe, non-empty string.
    """
    name = name.replace("/", "_").replace("\\", "_").replace("\0", "")
    name = name.replace("..", "_")
    # Leading dots would create hidden files on Unix; strip them
    name = name.lstrip("._ \t")
    name = re.sub(r'[<>:"|?*]', '_', name)
    name = re.sub(r'[_\s]+', '_', name)
    name = name.strip("_ ")
    return name or "document"


# ── OCR text layer ───────────────────────────────────────────────────

def _ocr_pdf(path: Path) -> None:
    """Add a searchable text layer to a PDF in-place using ocrmypdf.

    --skip-text avoids re-OCRing pages that already have text (e.g. if
    the scanner did its own OCR). Timeout prevents hanging on corrupt files.
    """
    if not _HAS_OCRMYPDF:
        return
    try:
        subprocess.run(
            ["ocrmypdf", "--skip-text", "--quiet", str(path), str(path)],
            check=True, timeout=120,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"  OCR warning: {e}", file=sys.stderr)


# ── PDF metadata embedding ───────────────────────────────────────────

def _embed_tags(pdf_bytes: bytes, tags: list[str], summary: str = "") -> bytes:
    """Write tags into XMP metadata (dc:subject and dc:description).

    These fields are readable by macOS Preview, Adobe Acrobat, and Spotlight,
    making scanned docs searchable by tag without opening them.
    """
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


# ── macOS Finder tags ────────────────────────────────────────────────
# Finder tags are stored as an extended attribute (xattr) on the file.
# This is separate from PDF metadata — it lets Spotlight index and Finder
# filter by tag even for non-PDF files. We write both PDF XMP metadata
# (portable) and Finder xattrs (macOS-native) for maximum discoverability.

def _set_finder_tags(path: Path, tags: list[str]) -> None:
    """Write macOS Finder tags so files are searchable in Spotlight and Finder.

    Sets the com.apple.metadata:_kMDItemUserTags extended attribute using
    a binary plist, which is the format Finder/Spotlight expects.
    """
    if not tags or platform.system() != "Darwin":
        return
    try:
        # Finder tag format: each tag is "name\n0" where 0 = no color label.
        # Colors 1-7 map to Finder's color dots (Red, Orange, etc.) but we
        # don't use them — just the tag name matters for search.
        plist_tags = [f"{t}\n0" for t in tags]
        plist_bytes = plistlib.dumps(plist_tags, fmt=plistlib.FMT_BINARY)
    except Exception:
        return

    # Prefer the xattr Python package (in-process, no fork overhead)
    try:
        import xattr as _xattr_mod
        _xattr_mod.setxattr(str(path), "com.apple.metadata:_kMDItemUserTags", plist_bytes)
        return
    except ImportError:
        pass
    except Exception:
        pass

    # Fall back to the xattr CLI tool (ships with macOS)
    try:
        import binascii
        hex_str = binascii.hexlify(plist_bytes).decode()
        subprocess.run(
            ["xattr", "-wx", "com.apple.metadata:_kMDItemUserTags", hex_str, str(path)],
            check=True, timeout=10,
        )
    except Exception:
        pass  # Non-critical — tags still exist in PDF metadata


# ── Document save pipeline ───────────────────────────────────────────
# Pipeline: images -> PDF bytes -> embed metadata -> write file -> OCR -> Finder tags

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

    # img2pdf wraps raw JPEG bytes in a PDF without re-encoding (lossless)
    pdf_bytes = img2pdf.convert(images)

    embed_tags = tags if tags is not None else [doc_info.category]
    pdf_bytes = _embed_tags(pdf_bytes, embed_tags, doc_info.summary)

    # Sanitize AI-generated filename and resolve collisions with _2, _3, etc.
    filename = sanitize_name(doc_info.filename.removesuffix(".pdf")) + ".pdf"

    output_path = category_dir / filename
    counter = 2
    while output_path.exists():
        stem = filename.removesuffix(".pdf")
        output_path = category_dir / f"{stem}_{counter}.pdf"
        counter += 1

    output_path.write_bytes(pdf_bytes)
    os.chmod(output_path, 0o600)  # owner-only: scanned docs may contain sensitive data
    _ocr_pdf(output_path)         # add searchable text layer (if ocrmypdf available)
    _set_finder_tags(output_path, embed_tags)  # macOS Spotlight integration

    print(f"Saved: {output_path}", file=sys.stderr)
    if embed_tags:
        print(f"  Tags: {', '.join(embed_tags)}", file=sys.stderr)
    return output_path


def save_unclassified(images: list[bytes], config: Config) -> Path:
    """Save a PDF without AI classification (--no-classify mode).

    Uses a timestamp filename and puts everything in "unsorted/" since
    there is no AI-derived category or filename.
    """
    unsorted_dir = config.output_dir / "unsorted"
    unsorted_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"{timestamp}_scan.pdf"
    output_path = unsorted_dir / filename

    pdf_bytes = img2pdf.convert(images)
    output_path.write_bytes(pdf_bytes)
    os.chmod(output_path, 0o600)
    _ocr_pdf(output_path)

    print(f"Saved: {output_path}", file=sys.stderr)
    return output_path
