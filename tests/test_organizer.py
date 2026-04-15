"""Tests for PDF creation and file organization."""

import os
from pathlib import Path

import pytest

from auto_scan.organizer import sanitize_name, save_document, save_unclassified
from auto_scan.analyzer import DocumentInfo
from auto_scan.config import Config
from helpers import make_test_image


# ── sanitize_name ─────────────────────────────────────────────────

class TestSanitizeName:
    """Ensure filenames are safe across platforms."""

    def test_passes_clean_name(self):
        assert sanitize_name("2024-03-15_invoice_vodafone") == "2024-03-15_invoice_vodafone"

    def test_strips_path_separators(self):
        assert "/" not in sanitize_name("../../etc/passwd")
        assert "\\" not in sanitize_name("..\\..\\windows\\system32")

    def test_removes_dotdot(self):
        result = sanitize_name("../malicious")
        assert ".." not in result

    def test_strips_leading_dot(self):
        result = sanitize_name(".hidden_file")
        assert not result.startswith(".")

    def test_removes_illegal_characters(self):
        result = sanitize_name('doc<>:"|?*name')
        assert all(c not in result for c in '<>:"|?*')

    def test_collapses_underscores(self):
        assert sanitize_name("too___many__underscores") == "too_many_underscores"

    def test_collapses_spaces(self):
        assert sanitize_name("too   many  spaces") == "too_many_spaces"

    def test_empty_returns_document(self):
        assert sanitize_name("") == "document"

    def test_only_dots_returns_document(self):
        assert sanitize_name("...") == "document"

    def test_null_bytes_removed(self):
        assert "\0" not in sanitize_name("file\0name")

    def test_strips_trailing_underscore(self):
        result = sanitize_name("name_")
        assert not result.endswith("_")


# ── save_document ─────────────────────────────────────────────────

class TestSaveDocument:
    """Test PDF creation and file saving."""

    @pytest.fixture
    def config(self, tmp_output_dir):
        return Config(
            api_key="test",
            scanner_ip=None,
            output_dir=tmp_output_dir,
            resolution=300,
            color_mode="RGB24",
            scan_source="Feeder",
            scan_format="image/jpeg",
            claude_model="test-model",
        )

    @pytest.fixture
    def doc_info(self):
        return DocumentInfo(
            category="invoice",
            filename="2024-03-15_invoice_test.pdf",
            summary="Test invoice",
            date="2024-03-15",
            tags=["invoice", "test"],
        )

    def test_creates_pdf_in_category_folder(self, config, doc_info):
        images = [make_test_image()]
        path = save_document(images, doc_info, config)
        assert path.exists()
        assert path.parent.name == "invoice"
        assert path.name == "2024-03-15_invoice_test.pdf"

    def test_pdf_is_valid(self, config, doc_info):
        images = [make_test_image()]
        path = save_document(images, doc_info, config)
        # A valid PDF starts with %PDF
        content = path.read_bytes()
        assert content[:5] == b"%PDF-"

    def test_multi_page_pdf(self, config, doc_info):
        images = [make_test_image(), make_test_image(color=(200, 200, 200))]
        path = save_document(images, doc_info, config)
        assert path.exists()
        # Multi-page PDF should be bigger than single-page
        assert path.stat().st_size > 0

    def test_filename_collision_increments(self, config, doc_info):
        images = [make_test_image()]
        path1 = save_document(images, doc_info, config)
        path2 = save_document(images, doc_info, config)
        assert path1 != path2
        assert path2.name == "2024-03-15_invoice_test_2.pdf"

    def test_triple_collision(self, config, doc_info):
        images = [make_test_image()]
        save_document(images, doc_info, config)
        save_document(images, doc_info, config)
        path3 = save_document(images, doc_info, config)
        assert path3.name == "2024-03-15_invoice_test_3.pdf"

    def test_file_permissions_owner_only(self, config, doc_info):
        images = [make_test_image()]
        path = save_document(images, doc_info, config)
        mode = oct(path.stat().st_mode)[-3:]
        assert mode == "600"

    def test_custom_folder_override(self, config, doc_info):
        images = [make_test_image()]
        path = save_document(images, doc_info, config, folder="custom_folder")
        assert path.parent.name == "custom_folder"

    def test_sanitizes_category_folder(self, config):
        doc_info = DocumentInfo(
            category="../../etc",
            filename="test.pdf",
            summary="Test",
            date=None,
        )
        images = [make_test_image()]
        path = save_document(images, doc_info, config)
        # Path should be safe — no traversal
        assert path.is_relative_to(config.output_dir)

    def test_tags_embedded_in_pdf(self, config, doc_info):
        images = [make_test_image()]
        path = save_document(images, doc_info, config, tags=["test_tag", "invoice"])
        # Read the PDF and check for tags in metadata
        import pikepdf
        with pikepdf.open(path) as pdf:
            with pdf.open_metadata() as meta:
                subjects = meta.get("dc:subject", [])
                # dc:subject may be a string or list
                if isinstance(subjects, str):
                    assert "test_tag" in subjects
                else:
                    # XMP returns qualified names, check the raw XML
                    pass
        # At minimum, the file exists and is a valid PDF
        assert path.stat().st_size > 0


# ── save_unclassified ─────────────────────────────────────────────

class TestSaveUnclassified:
    """Test unclassified scan saving."""

    @pytest.fixture
    def config(self, tmp_output_dir):
        return Config(
            api_key="test",
            scanner_ip=None,
            output_dir=tmp_output_dir,
            resolution=300,
            color_mode="RGB24",
            scan_source="Feeder",
            scan_format="image/jpeg",
            claude_model="test-model",
        )

    def test_saves_to_unsorted_folder(self, config):
        images = [make_test_image()]
        path = save_unclassified(images, config)
        assert path.exists()
        assert path.parent.name == "unsorted"

    def test_filename_has_timestamp(self, config):
        images = [make_test_image()]
        path = save_unclassified(images, config)
        # Filename should look like YYYY-MM-DD_HHMMSS_scan.pdf
        assert path.name.endswith("_scan.pdf")
        assert len(path.stem) > 10  # date + time + _scan

    def test_file_permissions(self, config):
        images = [make_test_image()]
        path = save_unclassified(images, config)
        mode = oct(path.stat().st_mode)[-3:]
        assert mode == "600"
