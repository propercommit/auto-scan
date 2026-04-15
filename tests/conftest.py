"""Shared pytest fixtures for auto-scan test suite."""

import pytest


@pytest.fixture
def tmp_output_dir(tmp_path):
    """Provide a temporary output directory for scan results."""
    out = tmp_path / "scans"
    out.mkdir()
    return out


@pytest.fixture
def env_with_api_key(monkeypatch):
    """Set a dummy API key in the environment."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-for-unit-tests")


@pytest.fixture
def clean_env(monkeypatch):
    """Remove all auto-scan env vars to ensure a clean slate."""
    for var in [
        "ANTHROPIC_API_KEY", "SCANNER_IP", "OUTPUT_DIR",
        "SCAN_RESOLUTION", "SCAN_COLOR_MODE", "SCAN_SOURCE",
        "SCAN_FORMAT", "CLAUDE_MODEL",
    ]:
        monkeypatch.delenv(var, raising=False)
