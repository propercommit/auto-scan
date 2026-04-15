"""Tests for image manipulation utilities."""

import io

import pytest
from PIL import Image

from auto_scan.image_utils import (
    open_image,
    make_thumbnail,
    rotate_image,
    crop_image,
)
from helpers import make_test_image


# ── open_image ────────────────────────────────────────────────────

class TestOpenImage:
    """Test multi-format image opening."""

    def test_opens_jpeg(self):
        img = open_image(make_test_image())
        assert isinstance(img, Image.Image)
        assert img.size == (800, 1000)

    def test_opens_png(self):
        buf = io.BytesIO()
        Image.new("RGB", (200, 200), (0, 255, 0)).save(buf, format="PNG")
        img = open_image(buf.getvalue())
        assert img.size == (200, 200)

    def test_invalid_data_raises(self):
        with pytest.raises(ValueError, match="Cannot read"):
            open_image(b"not an image")

    def test_empty_data_raises(self):
        with pytest.raises(ValueError):
            open_image(b"")


# ── make_thumbnail ────────────────────────────────────────────────

class TestMakeThumbnail:
    """Test thumbnail generation."""

    def test_large_image_downscaled(self):
        data = make_test_image(width=2000, height=3000)
        thumb = make_thumbnail(data, max_dim=800)
        img = Image.open(io.BytesIO(thumb))
        assert max(img.width, img.height) <= 800

    def test_small_image_not_upscaled(self):
        data = make_test_image(width=400, height=400)
        thumb = make_thumbnail(data, max_dim=800)
        img = Image.open(io.BytesIO(thumb))
        assert img.width <= 400

    def test_output_is_jpeg(self):
        thumb = make_thumbnail(make_test_image())
        assert thumb[:2] == b"\xff\xd8"

    def test_preserves_aspect_ratio(self):
        data = make_test_image(width=2000, height=1000)
        thumb = make_thumbnail(data, max_dim=800)
        img = Image.open(io.BytesIO(thumb))
        ratio = img.width / img.height
        assert abs(ratio - 2.0) < 0.1


# ── rotate_image ──────────────────────────────────────────────────

class TestRotateImage:
    """Test image rotation."""

    def test_rotate_90_swaps_dimensions(self):
        data = make_test_image(width=800, height=1000)
        rotated = rotate_image(data, 90)
        img = Image.open(io.BytesIO(rotated))
        # After 90° clockwise rotation, 800x1000 → 1000x800
        assert img.width == 1000
        assert img.height == 800

    def test_rotate_180_preserves_dimensions(self):
        data = make_test_image(width=800, height=1000)
        rotated = rotate_image(data, 180)
        img = Image.open(io.BytesIO(rotated))
        assert img.width == 800
        assert img.height == 1000

    def test_rotate_270(self):
        data = make_test_image(width=800, height=1000)
        rotated = rotate_image(data, 270)
        img = Image.open(io.BytesIO(rotated))
        assert img.width == 1000
        assert img.height == 800

    def test_invalid_degrees_raises(self):
        with pytest.raises(ValueError, match="Degrees must be"):
            rotate_image(make_test_image(), 45)

    def test_output_is_jpeg(self):
        result = rotate_image(make_test_image(), 90)
        assert result[:2] == b"\xff\xd8"


# ── crop_image ────────────────────────────────────────────────────

class TestCropImage:
    """Test fractional image cropping."""

    def test_crop_half(self):
        data = make_test_image(width=800, height=1000)
        cropped = crop_image(data, 0.0, 0.0, 0.5, 0.5)
        img = Image.open(io.BytesIO(cropped))
        assert img.width == 400
        assert img.height == 500

    def test_crop_full_is_same_size(self):
        data = make_test_image(width=800, height=1000)
        cropped = crop_image(data, 0.0, 0.0, 1.0, 1.0)
        img = Image.open(io.BytesIO(cropped))
        assert img.width == 800
        assert img.height == 1000

    def test_invalid_box_raises(self):
        data = make_test_image()
        with pytest.raises(ValueError, match="Invalid crop box"):
            crop_image(data, 0.5, 0.0, 0.3, 1.0)  # left > right

    def test_negative_raises(self):
        data = make_test_image()
        with pytest.raises(ValueError, match="Invalid crop box"):
            crop_image(data, -0.1, 0.0, 1.0, 1.0)

    def test_output_is_jpeg(self):
        result = crop_image(make_test_image(), 0.1, 0.1, 0.9, 0.9)
        assert result[:2] == b"\xff\xd8"
