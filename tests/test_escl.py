"""Tests for the eSCL protocol client (XML generation, URL handling, format conversion)."""

import xml.etree.ElementTree as ET

import pytest

from auto_scan.scanner.escl import (
    ScanSettings,
    ScannerCapabilities,
    ScannerStatus,
    _ensure_jpeg,
    PWG_NS,
    SCAN_NS,
)
from auto_scan import ScanError
from helpers import make_test_image


# ── ScanSettings XML generation ───────────────────────────────────

class TestScanSettingsXml:
    """Verify the XML body sent to create scan jobs."""

    def test_default_settings_valid_xml(self):
        settings = ScanSettings()
        xml_str = settings.to_xml()
        # Should parse without error
        root = ET.fromstring(xml_str)
        assert root.tag == f"{{{SCAN_NS}}}ScanSettings"

    def test_contains_required_elements(self):
        settings = ScanSettings(source="Feeder", resolution=300, color_mode="RGB24")
        xml_str = settings.to_xml()
        root = ET.fromstring(xml_str)
        # Check key child elements exist
        ns = {"pwg": PWG_NS, "scan": SCAN_NS}
        assert root.find("pwg:InputSource", ns).text == "Feeder"
        assert root.find("scan:XResolution", ns).text == "300"
        assert root.find("scan:YResolution", ns).text == "300"
        assert root.find("scan:ColorMode", ns).text == "RGB24"

    def test_flatbed_source(self):
        settings = ScanSettings(source="Platen")
        xml_str = settings.to_xml()
        root = ET.fromstring(xml_str)
        ns = {"pwg": PWG_NS, "scan": SCAN_NS}
        assert root.find("pwg:InputSource", ns).text == "Platen"

    def test_grayscale_mode(self):
        settings = ScanSettings(color_mode="Grayscale8")
        xml_str = settings.to_xml()
        assert "Grayscale8" in xml_str

    def test_custom_resolution(self):
        settings = ScanSettings(resolution=600)
        xml_str = settings.to_xml()
        root = ET.fromstring(xml_str)
        ns = {"pwg": PWG_NS, "scan": SCAN_NS}
        assert root.find("scan:XResolution", ns).text == "600"

    def test_document_format(self):
        settings = ScanSettings(document_format="image/jpeg")
        xml_str = settings.to_xml()
        root = ET.fromstring(xml_str)
        ns = {"pwg": PWG_NS, "scan": SCAN_NS}
        assert root.find("pwg:DocumentFormat", ns).text == "image/jpeg"

    def test_xml_declaration_present(self):
        settings = ScanSettings()
        xml_str = settings.to_xml()
        assert xml_str.startswith('<?xml version="1.0"')

    def test_injection_safe_source(self):
        """Field values should not break XML structure."""
        settings = ScanSettings(source='Feeder"><evil/><x a="')
        xml_str = settings.to_xml()
        # Should still parse as valid XML (ET escapes the content)
        root = ET.fromstring(xml_str)
        # The evil tag should NOT exist as a separate element
        assert root.find("evil") is None


# ── _ensure_jpeg ──────────────────────────────────────────────────

class TestEnsureJpeg:
    """Test format conversion to JPEG."""

    def test_jpeg_passthrough(self):
        """JPEG input should be returned unchanged."""
        jpeg_data = make_test_image()
        assert jpeg_data[:2] == b"\xff\xd8"  # JPEG magic bytes
        result = _ensure_jpeg(jpeg_data)
        assert result == jpeg_data

    def test_png_to_jpeg(self):
        """PNG input should be converted to JPEG."""
        import io
        from PIL import Image
        img = Image.new("RGB", (100, 100), (255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_data = buf.getvalue()
        assert png_data[:4] == b"\x89PNG"

        result = _ensure_jpeg(png_data)
        assert result[:2] == b"\xff\xd8"  # Now JPEG

    def test_unrecognized_format_raises(self):
        with pytest.raises(ScanError, match="unrecognized"):
            _ensure_jpeg(b"not an image at all")

    def test_empty_data_raises(self):
        with pytest.raises(ScanError):
            _ensure_jpeg(b"")


# ── Dataclass construction ────────────────────────────────────────

class TestScannerDataclasses:
    """Verify dataclass defaults and properties."""

    def test_scanner_capabilities(self):
        caps = ScannerCapabilities(
            resolutions=[150, 300, 600],
            color_modes=["RGB24", "Grayscale8"],
            sources=["Platen", "Feeder"],
            formats=["image/jpeg"],
        )
        assert 300 in caps.resolutions
        assert "Feeder" in caps.sources

    def test_scanner_status_idle(self):
        status = ScannerStatus(state="Idle", adf_state="ScannerAdfLoaded")
        assert status.state == "Idle"
        assert status.adf_state == "ScannerAdfLoaded"

    def test_scanner_status_no_adf(self):
        status = ScannerStatus(state="Idle", adf_state=None)
        assert status.adf_state is None
