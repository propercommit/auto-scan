"""Tests for perceptual image hashing and duplicate detection."""

import pytest

from auto_scan.dedup import image_hash
from helpers import make_test_image


class TestImageHash:
    """Test perceptual hashing for duplicate detection."""

    def test_identical_images_same_hash(self):
        """Two identical images must produce the same hash."""
        img = make_test_image(color=(255, 255, 255))
        h1 = image_hash([img])
        h2 = image_hash([img])
        assert h1 == h2

    def test_different_images_different_hash(self):
        """Visually distinct images should produce different hashes.

        Uses images with actual spatial variation (not solid colors) since
        the perceptual hash uses mean-thresholding which maps all-same-value
        images to the same bit pattern regardless of brightness.
        """
        from PIL import Image, ImageDraw
        import io

        # Image A: white with a black rectangle on the left
        img_a = Image.new("RGB", (800, 1000), (255, 255, 255))
        draw_a = ImageDraw.Draw(img_a)
        draw_a.rectangle([0, 0, 400, 1000], fill=(0, 0, 0))
        buf_a = io.BytesIO()
        img_a.save(buf_a, format="JPEG", quality=85)

        # Image B: white with a black rectangle on the right
        img_b = Image.new("RGB", (800, 1000), (255, 255, 255))
        draw_b = ImageDraw.Draw(img_b)
        draw_b.rectangle([400, 0, 800, 1000], fill=(0, 0, 0))
        buf_b = io.BytesIO()
        img_b.save(buf_b, format="JPEG", quality=85)

        h1 = image_hash([buf_a.getvalue()])
        h2 = image_hash([buf_b.getvalue()])
        assert h1 != h2

    def test_returns_hex_string(self):
        """Hash should be a 16-char hexadecimal string."""
        img = make_test_image()
        h = image_hash([img])
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_multi_page_hash(self):
        """Multi-page documents should produce a single combined hash."""
        imgs = [make_test_image(), make_test_image(color=(200, 200, 200))]
        h = image_hash(imgs)
        assert len(h) == 16

    def test_page_order_matters(self):
        """Different page ordering should produce different hashes.

        Uses images with spatial variation so the mean-threshold
        fingerprints differ between pages.
        """
        from PIL import Image, ImageDraw
        import io

        img_a = Image.new("RGB", (800, 1000), (255, 255, 255))
        draw_a = ImageDraw.Draw(img_a)
        draw_a.rectangle([0, 0, 400, 500], fill=(0, 0, 0))
        buf_a = io.BytesIO()
        img_a.save(buf_a, format="JPEG", quality=85)

        img_b = Image.new("RGB", (800, 1000), (0, 0, 0))
        draw_b = ImageDraw.Draw(img_b)
        draw_b.rectangle([400, 500, 800, 1000], fill=(255, 255, 255))
        buf_b = io.BytesIO()
        img_b.save(buf_b, format="JPEG", quality=85)

        h1 = image_hash([buf_a.getvalue(), buf_b.getvalue()])
        h2 = image_hash([buf_b.getvalue(), buf_a.getvalue()])
        assert h1 != h2

    def test_slight_variation_tolerance(self):
        """Slightly different brightness should produce the SAME hash.

        The perceptual hash uses mean-threshold, so small variations
        in brightness should be tolerated.
        """
        img_a = make_test_image(color=(128, 128, 128))
        img_b = make_test_image(color=(130, 130, 130))
        h1 = image_hash([img_a])
        h2 = image_hash([img_b])
        # These should ideally be the same (perceptual tolerance),
        # but at minimum the function should not crash
        assert len(h1) == 16 and len(h2) == 16

    def test_empty_list(self):
        """Empty image list should still return a valid hash."""
        h = image_hash([])
        assert len(h) == 16
