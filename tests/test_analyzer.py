"""Tests for document analyzer utilities (image processing, JSON parsing, similarity).

Does NOT test API calls — only pure functions that can run without network access.
"""

import io
import json

import pytest
from PIL import Image

from auto_scan.recognition.engine import (
    DocumentInfo,
    _open_image,
    _resize_for_api,
    _label_page,
    _region_histogram_similarity,
    _region_pixel_similarity,
    _page_similarity,
    _detect_page_numbers,
    _repair_truncated_json,
    _parse_batch_results,
)
from auto_scan.recognition.prompts import ALL_CATEGORIES
from auto_scan import AnalysisError
from helpers import make_test_image, make_test_image_with_header


# ── _open_image ───────────────────────────────────────────────────

class TestOpenImage:
    """Test image opening with format detection."""

    def test_opens_jpeg(self):
        data = make_test_image()
        img = _open_image(data)
        assert isinstance(img, Image.Image)

    def test_opens_png(self):
        img = Image.new("RGB", (100, 100), (0, 255, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        result = _open_image(buf.getvalue())
        assert isinstance(result, Image.Image)

    def test_invalid_data_raises(self):
        with pytest.raises(AnalysisError, match="Cannot read"):
            _open_image(b"not an image")


# ── _resize_for_api ───────────────────────────────────────────────

class TestResizeForApi:
    """Test image resizing and metadata stripping."""

    def test_small_image_not_upscaled(self):
        data = make_test_image(width=500, height=700)
        result = _resize_for_api(data, max_dim=1568)
        img = Image.open(io.BytesIO(result))
        # Should not be upscaled
        assert img.width <= 500
        assert img.height <= 700

    def test_large_image_downscaled(self):
        data = make_test_image(width=4000, height=3000)
        result = _resize_for_api(data, max_dim=1568)
        img = Image.open(io.BytesIO(result))
        assert max(img.width, img.height) <= 1568

    def test_preserves_aspect_ratio(self):
        data = make_test_image(width=4000, height=2000)
        result = _resize_for_api(data, max_dim=1568)
        img = Image.open(io.BytesIO(result))
        ratio = img.width / img.height
        assert abs(ratio - 2.0) < 0.05  # Original was 2:1

    def test_output_is_jpeg(self):
        data = make_test_image()
        result = _resize_for_api(data)
        assert result[:2] == b"\xff\xd8"  # JPEG magic bytes

    def test_custom_max_dim(self):
        data = make_test_image(width=2000, height=2000)
        result = _resize_for_api(data, max_dim=800)
        img = Image.open(io.BytesIO(result))
        assert max(img.width, img.height) <= 800


# ── _label_page ───────────────────────────────────────────────────

class TestLabelPage:
    """Test page number labeling burned into images."""

    def test_returns_jpeg(self):
        data = make_test_image()
        result = _label_page(data, page_num=1)
        assert result[:2] == b"\xff\xd8"

    def test_different_pages_different_output(self):
        data = make_test_image()
        page1 = _label_page(data, page_num=1)
        page2 = _label_page(data, page_num=2)
        # Labels are different, so bytes should differ
        assert page1 != page2

    def test_resizes_large_image(self):
        data = make_test_image(width=5000, height=5000)
        result = _label_page(data, page_num=1, max_dim=1568)
        img = Image.open(io.BytesIO(result))
        assert max(img.width, img.height) <= 1568


# ── Document boundary detection (similarity) ─────────────────────

class TestRegionHistogramSimilarity:
    """Test color histogram comparison for document boundaries."""

    def test_identical_images_high_similarity(self):
        img = Image.new("RGB", (800, 1000), (255, 255, 255))
        score = _region_histogram_similarity(img, img, 0.0, 0.15)
        assert score >= 99.0

    def test_different_headers_low_similarity(self):
        img_a = Image.new("RGB", (800, 1000), (255, 255, 255))
        img_b = Image.new("RGB", (800, 1000), (255, 255, 255))
        # Paint different colored headers
        from PIL import ImageDraw
        draw_a = ImageDraw.Draw(img_a)
        draw_b = ImageDraw.Draw(img_b)
        draw_a.rectangle([0, 0, 800, 150], fill=(0, 0, 128))  # Blue header
        draw_b.rectangle([0, 0, 800, 150], fill=(200, 0, 0))  # Red header
        score = _region_histogram_similarity(img_a, img_b, 0.0, 0.15)
        assert score < 80

    def test_returns_0_to_100(self):
        img_a = Image.new("RGB", (800, 1000), (0, 0, 0))
        img_b = Image.new("RGB", (800, 1000), (255, 255, 255))
        score = _region_histogram_similarity(img_a, img_b, 0.0, 0.15)
        assert 0 <= score <= 100


class TestRegionPixelSimilarity:
    """Test pixel-level comparison for layout differences."""

    def test_identical_regions_high_similarity(self):
        img = Image.new("RGB", (800, 1000), (200, 200, 200))
        score = _region_pixel_similarity(img, img, 0.0, 0.15)
        assert score >= 99.0

    def test_opposite_regions_low_similarity(self):
        img_a = Image.new("RGB", (800, 1000), (0, 0, 0))
        img_b = Image.new("RGB", (800, 1000), (255, 255, 255))
        score = _region_pixel_similarity(img_a, img_b, 0.0, 0.15)
        assert score < 20


class TestPageSimilarity:
    """Test combined page similarity scoring."""

    def test_identical_pages_100(self):
        img = Image.new("RGB", (800, 1000), (255, 255, 255))
        score = _page_similarity(img, img)
        assert score >= 99.0

    def test_different_headers_low_score(self):
        img_a = Image.new("RGB", (800, 1000), (255, 255, 255))
        img_b = Image.new("RGB", (800, 1000), (255, 255, 255))
        from PIL import ImageDraw
        draw_a = ImageDraw.Draw(img_a)
        draw_b = ImageDraw.Draw(img_b)
        draw_a.rectangle([0, 0, 800, 150], fill=(0, 0, 128))
        draw_b.rectangle([0, 0, 800, 150], fill=(200, 0, 0))
        score = _page_similarity(img_a, img_b)
        assert score < 80

    def test_same_header_different_footer(self):
        img_a = Image.new("RGB", (800, 1000), (255, 255, 255))
        img_b = Image.new("RGB", (800, 1000), (255, 255, 255))
        from PIL import ImageDraw
        # Same blue header
        draw_a = ImageDraw.Draw(img_a)
        draw_b = ImageDraw.Draw(img_b)
        draw_a.rectangle([0, 0, 800, 150], fill=(0, 0, 128))
        draw_b.rectangle([0, 0, 800, 150], fill=(0, 0, 128))
        # Different footers
        draw_a.rectangle([0, 880, 800, 1000], fill=(0, 128, 0))
        draw_b.rectangle([0, 880, 800, 1000], fill=(128, 0, 0))
        score = _page_similarity(img_a, img_b)
        # Header matches (70%), footer differs (30%) — score should be moderate
        assert 50 < score < 90


# ── Page number detection ─────────────────────────────────────────

class TestDetectPageNumbers:
    """Test OCR page number pattern detection."""

    def test_page_x_of_y(self):
        found = _detect_page_numbers("Page 2 of 4")
        assert any("2" in f and "4" in f for f in found)

    def test_seite_von(self):
        found = _detect_page_numbers("Seite 3 von 5")
        assert len(found) > 0

    def test_slash_format(self):
        found = _detect_page_numbers("2/4")
        assert len(found) > 0

    def test_dash_format(self):
        found = _detect_page_numbers("- 3 -")
        assert len(found) > 0

    def test_no_page_numbers(self):
        found = _detect_page_numbers("This is a regular paragraph with no page numbers.")
        assert len(found) == 0


# ── JSON repair ───────────────────────────────────────────────────

class TestRepairTruncatedJson:
    """Test recovery from truncated API responses."""

    def test_complete_json_unchanged(self):
        valid = '[{"pages": [1], "category": "invoice"}]'
        assert json.loads(_repair_truncated_json(valid)) == json.loads(valid)

    def test_repairs_truncated_array(self):
        truncated = '[{"pages": [1], "category": "invoice"}, {"pages": [2], "categ'
        repaired = _repair_truncated_json(truncated)
        data = json.loads(repaired)
        assert isinstance(data, list)
        assert len(data) == 1  # Second object was incomplete

    def test_hopeless_returns_original(self):
        garbage = "not json at all"
        assert _repair_truncated_json(garbage) == garbage


# ── Batch result parsing ──────────────────────────────────────────

class TestParseBatchResults:
    """Test conversion from raw JSON to typed results."""

    def test_single_document(self):
        data = [{
            "pages": [1, 2],
            "page_confidence": {"1": 95, "2": 88},
            "confidence": 91,
            "category": "invoice",
            "filename": "2024-03-15_invoice.pdf",
            "summary": "Test invoice",
            "date": "2024-03-15",
            "tags": ["invoice", "test"],
        }]
        results = _parse_batch_results(data)
        assert len(results) == 1
        pages, doc_info = results[0]
        # Pages should be 0-indexed
        assert pages == [0, 1]
        assert doc_info.category == "invoice"
        assert doc_info.confidence == 91
        assert doc_info.page_confidence == {1: 95, 2: 88}

    def test_multiple_documents(self):
        data = [
            {"pages": [1], "confidence": 95, "category": "invoice",
             "filename": "a.pdf", "summary": "A", "date": None},
            {"pages": [2, 3], "confidence": 80, "category": "contract",
             "filename": "b.pdf", "summary": "B", "date": None},
        ]
        results = _parse_batch_results(data)
        assert len(results) == 2
        assert results[0][0] == [0]
        assert results[1][0] == [1, 2]

    def test_missing_fields_have_defaults(self):
        data = [{"pages": [1]}]
        results = _parse_batch_results(data)
        doc_info = results[0][1]
        assert doc_info.category == "other"
        assert doc_info.confidence == 100
        assert doc_info.tags == []
        assert doc_info.risks == []


# ── DocumentInfo dataclass ────────────────────────────────────────

class TestDocumentInfo:
    """Test DocumentInfo construction and defaults."""

    def test_defaults(self):
        doc = DocumentInfo(
            category="invoice",
            filename="test.pdf",
            summary="Test",
            date=None,
        )
        assert doc.key_fields == {}
        assert doc.tags == []
        assert doc.risks == []
        assert doc.risk_level == "none"
        assert doc.confidence == 100

    def test_all_fields(self):
        doc = DocumentInfo(
            category="invoice",
            filename="test.pdf",
            summary="Test",
            date="2024-01-01",
            key_fields={"amount": "100"},
            suggested_categories=["invoice", "receipt"],
            tags=["test"],
            risk_level="low",
            risks=["review fees"],
            confidence=85,
            page_confidence={1: 90, 2: 80},
        )
        assert doc.confidence == 85
        assert doc.page_confidence[1] == 90


# ── Categories constant ───────────────────────────────────────────

class TestCategories:
    """Verify category list completeness."""

    def test_has_other(self):
        assert "other" in ALL_CATEGORIES

    def test_common_categories_present(self):
        for cat in ["invoice", "receipt", "contract", "letter", "medical",
                     "tax", "insurance", "bank", "government"]:
            assert cat in ALL_CATEGORIES

    def test_no_duplicates(self):
        assert len(ALL_CATEGORIES) == len(set(ALL_CATEGORIES))
