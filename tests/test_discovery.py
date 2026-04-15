"""Tests for scanner discovery (ScannerInfo construction and base_url)."""

from unittest.mock import patch, MagicMock

import httpx
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

    def test_explicit_port_skips_probing(self):
        """When port is given explicitly, no network probing occurs."""
        info = scanner_info_from_ip("192.168.1.50", port=8080)
        assert info.port == 8080
        assert info.ip == "192.168.1.50"

    def test_explicit_port_default_root_path(self):
        info = scanner_info_from_ip("10.0.0.1", port=443)
        assert info.root_path == "/eSCL"

    def test_name_from_ip(self):
        info = scanner_info_from_ip("192.168.1.50", port=443)
        assert "192.168.1.50" in info.name

    def test_no_port_probes_https_first(self):
        """Without explicit port, probes HTTPS 443 first."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp

        with patch("httpx.Client", return_value=mock_client):
            info = scanner_info_from_ip("192.168.1.50")

        assert info.port == 443
        # First probe should be HTTPS on 443
        call_url = mock_client.get.call_args_list[0][0][0]
        assert "https" in call_url
        assert "443" in call_url

    def test_no_port_falls_back_to_http_80(self):
        """Falls back to HTTP 80 when HTTPS 443 fails."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        def mock_get(url, **kwargs):
            if "443" in url:
                raise httpx.ConnectError("TLS failed")
            return mock_resp

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = mock_get

        with patch("httpx.Client", return_value=mock_client):
            info = scanner_info_from_ip("192.168.1.50")

        assert info.port == 80

    def test_no_port_falls_back_to_8080(self):
        """Falls back to port 8080 when both 443 and 80 fail."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        def mock_get(url, **kwargs):
            if "443" in url or ":80/" in url:
                raise httpx.ConnectError("Connection refused")
            return mock_resp

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = mock_get

        with patch("httpx.Client", return_value=mock_client):
            info = scanner_info_from_ip("192.168.1.50")

        assert info.port == 8080

    def test_no_port_defaults_443_when_all_fail(self):
        """Defaults to 443 when no port responds (lets ESCLClient handle errors)."""
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = httpx.ConnectError("refused")

        with patch("httpx.Client", return_value=mock_client):
            info = scanner_info_from_ip("192.168.1.50")

        assert info.port == 443
