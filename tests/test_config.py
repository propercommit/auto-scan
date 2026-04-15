"""Tests for configuration loading and validation."""

import pytest

from auto_scan.config import (
    Config,
    _validate_ip,
    load_config,
    VALID_COLOR_MODES,
    VALID_SOURCES,
    VALID_FORMATS,
    MIN_RESOLUTION,
    MAX_RESOLUTION,
)


# ── IP validation ─────────────────────────────────────────────────

class TestValidateIp:
    """Tests for _validate_ip helper."""

    def test_empty_string_is_valid(self):
        assert _validate_ip("") is True

    def test_valid_ipv4(self):
        assert _validate_ip("192.168.1.1") is True
        assert _validate_ip("10.0.0.1") is True
        assert _validate_ip("255.255.255.255") is True
        assert _validate_ip("0.0.0.0") is True

    def test_invalid_ipv4_octet_out_of_range(self):
        assert _validate_ip("256.1.1.1") is False
        assert _validate_ip("1.1.1.300") is False

    def test_valid_hostname(self):
        assert _validate_ip("scanner.local") is True
        assert _validate_ip("my-printer") is True
        assert _validate_ip("canon-gx7050.lan") is True

    def test_invalid_hostname_special_chars(self):
        assert _validate_ip("scanner; rm -rf") is False
        assert _validate_ip("host/path") is False
        assert _validate_ip("host:8080") is False


# ── Config loading ────────────────────────────────────────────────

class TestLoadConfig:
    """Tests for load_config()."""

    def test_missing_api_key_raises(self, clean_env, monkeypatch):
        # Prevent load_dotenv from injecting the real .env API key
        monkeypatch.setattr("auto_scan.config.load_dotenv", lambda: None)
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            load_config()

    def test_loads_defaults(self, env_with_api_key):
        config = load_config()
        assert config.api_key == "sk-ant-test-key-for-unit-tests"
        assert config.resolution == 300
        assert config.color_mode == "RGB24"
        assert config.scan_source == "Feeder"
        assert config.scanner_ip is None

    def test_override_resolution(self, env_with_api_key):
        config = load_config(resolution=600)
        assert config.resolution == 600

    def test_override_color_mode(self, env_with_api_key):
        config = load_config(color_mode="Grayscale8")
        assert config.color_mode == "Grayscale8"

    def test_override_scan_source(self, env_with_api_key):
        config = load_config(scan_source="Platen")
        assert config.scan_source == "Platen"

    def test_override_scanner_ip(self, env_with_api_key):
        config = load_config(scanner_ip="192.168.1.50")
        assert config.scanner_ip == "192.168.1.50"

    def test_override_output_dir(self, env_with_api_key, tmp_path):
        config = load_config(output_dir=str(tmp_path / "out"))
        assert config.output_dir == tmp_path / "out"

    def test_invalid_resolution_too_low(self, env_with_api_key):
        with pytest.raises(ValueError, match="Resolution"):
            load_config(resolution=10)

    def test_invalid_resolution_too_high(self, env_with_api_key):
        with pytest.raises(ValueError, match="Resolution"):
            load_config(resolution=9999)

    def test_invalid_color_mode(self, env_with_api_key):
        with pytest.raises(ValueError, match="color mode"):
            load_config(color_mode="CMYK")

    def test_invalid_scan_source(self, env_with_api_key):
        with pytest.raises(ValueError, match="scan source"):
            load_config(scan_source="Tray3")

    def test_invalid_scanner_ip(self, env_with_api_key):
        with pytest.raises(ValueError, match="Invalid scanner IP"):
            load_config(scanner_ip="192.168.1.1; rm -rf /")

    def test_output_dir_expands_tilde(self, env_with_api_key):
        config = load_config()
        # Default is ~/Documents/Scans — should not start with ~
        assert "~" not in str(config.output_dir)

    def test_env_var_resolution(self, env_with_api_key, monkeypatch):
        monkeypatch.setenv("SCAN_RESOLUTION", "600")
        config = load_config()
        assert config.resolution == 600

    def test_env_var_scanner_ip(self, env_with_api_key, monkeypatch):
        monkeypatch.setenv("SCANNER_IP", "10.0.0.5")
        config = load_config()
        assert config.scanner_ip == "10.0.0.5"
