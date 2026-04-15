"""Tests for scanner discovery (ScannerInfo construction and base_url)."""

import pytest

from auto_scan.scanner.discovery import ScannerInfo, scanner_info_from_ip


# ── ScannerInfo ───────────────────────────────────────────────────

class TestScannerInfo:
    """Test ScannerInfo dataclass and base_url property."""

    def test_base_url_https(self):
        info = ScannerInfo(ip="192.168.1.50", port=443, root_path="/eSCL", name="Canon")
        assert info.base_url == "https://192.168.1.50:443/eSCL"

    def test_base_url_http(self):
        info = ScannerInfo(ip="192.168.1.50", port=8080, root_path="/eSCL", name="Canon")
        assert info.base_url == "http://192.168.1.50:8080/eSCL"

    def test_base_url_no_leading_slash(self):
        """root_path without leading slash should still produce valid URL."""
        info = ScannerInfo(ip="10.0.0.1", port=80, root_path="eSCL", name="HP")
        assert info.base_url == "http://10.0.0.1:80/eSCL"

    def test_base_url_root_path_with_slash(self):
        info = ScannerInfo(ip="10.0.0.1", port=80, root_path="/eSCL", name="HP")
        assert info.base_url == "http://10.0.0.1:80/eSCL"


# ── scanner_info_from_ip ──────────────────────────────────────────

class TestScannerInfoFromIp:
    """Test direct IP construction (skip discovery)."""

    def test_default_port(self):
        info = scanner_info_from_ip("192.168.1.50")
        assert info.port == 443
        assert info.ip == "192.168.1.50"

    def test_custom_port(self):
        info = scanner_info_from_ip("192.168.1.50", port=8080)
        assert info.port == 8080

    def test_default_root_path(self):
        info = scanner_info_from_ip("10.0.0.1")
        assert info.root_path == "/eSCL"

    def test_name_from_ip(self):
        info = scanner_info_from_ip("192.168.1.50")
        assert "192.168.1.50" in info.name
