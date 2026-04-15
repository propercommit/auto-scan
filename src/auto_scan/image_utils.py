"""Image manipulation utilities shared by the GUI and pipeline.

Pure functions with no external state — safe for concurrent use and
easy to test. All functions accept and return bytes (JPEG) to avoid
coupling callers to PIL internals.
"""

from __future__ import annotations

import io

from PIL import Image


def open_image(image_data: bytes) -> Image.Image:
    """Open image bytes, handling formats PIL can't directly decode.

    Tries PIL first, then falls back to extracting images from PDF
    (some scanners return PDF even when JPEG was requested).

    Returns a loaded PIL Image. Raises ValueError on failure.
    """
    try:
        img = Image.open(io.BytesIO(image_data))
        img.load()
        return img
    except Exception:
        pass

    # Scanner may have returned PDF — extract first page image
    if image_data[:5] == b"%PDF-":
        try:
            import pikepdf
            pdf = pikepdf.open(io.BytesIO(image_data))
            page = pdf.pages[0]
            for image_key in page.images:
                pil_img = page.images[image_key].as_pil_image()
                pdf.close()
                return pil_img
            pdf.close()
        except Exception:
            pass

    raise ValueError("Cannot read scanned image — unsupported format from scanner.")


def make_thumbnail(image_data: bytes, max_dim: int = 800) -> bytes:
    """Downscale an image for web preview, capping the longest edge.

    Args:
        image_data: Source JPEG bytes.
        max_dim: Maximum width or height in pixels.

    Returns:
        JPEG bytes at reduced quality (75) for fast delivery.
    """
    img = open_image(image_data)
    w, h = img.size
    if w > max_dim or h > max_dim:
        scale = max_dim / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=75)
    return buf.getvalue()


def rotate_image(image_data: bytes, degrees: int) -> bytes:
    """Rotate an image clockwise by the given degrees (90, 180, 270).

    Returns high-quality JPEG bytes.
    """
    if degrees not in (90, 180, 270):
        raise ValueError(f"Degrees must be 90, 180, or 270 — got {degrees}")
    img = open_image(image_data)
    # PIL rotate() is counter-clockwise, negate for clockwise
    rotated = img.rotate(-degrees, expand=True)
    buf = io.BytesIO()
    rotated.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def crop_image(image_data: bytes, left: float, top: float, right: float, bottom: float) -> bytes:
    """Crop an image using fractional coordinates (0.0–1.0).

    Args:
        image_data: Source JPEG bytes.
        left, top, right, bottom: Crop box as fractions of image dimensions.

    Returns:
        Cropped JPEG bytes. Raises ValueError for invalid coordinates.
    """
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ValueError(f"Invalid crop box: ({left}, {top}, {right}, {bottom})")
    img = open_image(image_data)
    w, h = img.size
    box = (int(left * w), int(top * h), int(right * w), int(bottom * h))
    cropped = img.crop(box)
    buf = io.BytesIO()
    cropped.save(buf, format="JPEG", quality=95)
    return buf.getvalue()
