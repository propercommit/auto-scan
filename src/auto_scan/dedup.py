"""Duplicate detection via perceptual image hashing.

Uses a mean-threshold approach (a simplified "aHash") rather than DCT-based
pHash because scan images are already low-frequency (text on paper). The
simpler algorithm is faster and sufficient to catch re-scans of the same
document with slightly different brightness or alignment.
"""

from __future__ import annotations

import hashlib
import io
import sys

from PIL import Image


def image_hash(images: list[bytes]) -> str:
    """Generate a perceptual hash for a list of page images.

    Downscales each page to 16x16 grayscale, takes the mean-threshold
    fingerprint, and combines all pages into one SHA-256 digest.
    This is tolerant of minor scan variations (brightness, slight skew)
    while catching true duplicates.

    Returns a 16-char hex digest, or a fallback raw-bytes hash if any
    image fails to process.
    """
    fingerprints = []
    for i, img_data in enumerate(images):
        try:
            img = Image.open(io.BytesIO(img_data))
            # Reduce to 16x16 grayscale: this removes high-frequency detail
            # (text content, noise) and keeps only the document's overall
            # brightness structure — enough to identify the same physical page.
            small = img.convert("L").resize((16, 16), Image.LANCZOS)
            pixels = list(small.getdata())
            # Mean-threshold: each pixel becomes 1 if >= mean, 0 otherwise.
            # This makes the fingerprint invariant to global brightness shifts
            # (e.g. scanning the same page on different days).
            mean = sum(pixels) / len(pixels)
            bits = "".join("1" if p >= mean else "0" for p in pixels)
            fingerprints.append(bits)
        except Exception as e:
            # Fallback to cryptographic hash of raw bytes — won't match
            # perceptual duplicates, but at least catches byte-identical re-scans.
            print(f"  Hash warning: page {i + 1} failed ({e}), using raw fallback", file=sys.stderr)
            fingerprints.append(hashlib.sha256(img_data).hexdigest())

    # Combine per-page fingerprints with a separator so page count matters
    # (a 2-page doc won't collide with its first page alone), then hash
    # down to a compact 16-char hex string for storage in SQLite.
    combined = "|".join(fingerprints)
    return hashlib.sha256(combined.encode()).hexdigest()[:16]
