"""Duplicate detection via perceptual image hashing."""

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
            small = img.convert("L").resize((16, 16), Image.LANCZOS)
            pixels = list(small.getdata())
            mean = sum(pixels) / len(pixels)
            bits = "".join("1" if p >= mean else "0" for p in pixels)
            fingerprints.append(bits)
        except Exception as e:
            print(f"  Hash warning: page {i + 1} failed ({e}), using raw fallback", file=sys.stderr)
            fingerprints.append(hashlib.sha256(img_data).hexdigest())

    combined = "|".join(fingerprints)
    return hashlib.sha256(combined.encode()).hexdigest()[:16]
