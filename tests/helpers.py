"""Shared test helpers for creating synthetic images."""

import io

from PIL import Image, ImageDraw


def make_test_image(width=800, height=1000, color=(255, 255, 255), text=None):
    """Create a simple test JPEG image in memory.

    Args:
        width: Image width in pixels.
        height: Image height in pixels.
        color: RGB background color tuple.
        text: Optional text to draw (best-effort, no font dependency).

    Returns:
        JPEG bytes.
    """
    img = Image.new("RGB", (width, height), color)
    if text:
        try:
            draw = ImageDraw.Draw(img)
            draw.text((50, 50), text, fill=(0, 0, 0))
        except Exception:
            pass
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def make_test_image_with_header(
    header_color=(0, 0, 128), body_color=(255, 255, 255),
    width=800, height=1000,
):
    """Create a test image with a colored header region (top 15%).

    Useful for testing document boundary detection.
    """
    img = Image.new("RGB", (width, height), body_color)
    draw = ImageDraw.Draw(img)
    header_height = int(height * 0.15)
    draw.rectangle([0, 0, width, header_height], fill=header_color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()
