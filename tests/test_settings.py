"""Tests for persistent GUI settings."""

import json
from pathlib import Path

import pytest

import auto_scan.settings as settings_mod
from auto_scan.settings import DEFAULTS, load, save, update


@pytest.fixture(autouse=True)
def isolate_settings(tmp_path, monkeypatch):
    """Redirect settings file to a temp directory and return the path."""
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr("auto_scan.settings._settings_path", lambda: settings_file)
    return settings_file


# ── Load ──────────────────────────────────────────────────────────

class TestLoad:
    """Test loading settings from disk."""

    def test_returns_defaults_when_no_file(self):
        settings = load()
        assert settings == DEFAULTS

    def test_merges_with_defaults(self, isolate_settings):
        # Write a partial settings file — missing keys should come from defaults
        path = isolate_settings
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"resolution": "600"}))
        settings = load()
        assert settings["resolution"] == "600"
        assert settings["color_mode"] == "RGB24"  # From defaults

    def test_preserves_unknown_keys(self, isolate_settings):
        path = isolate_settings
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"future_setting": "value"}))
        settings = load()
        assert settings["future_setting"] == "value"

    def test_handles_corrupt_file(self, isolate_settings):
        path = isolate_settings
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not valid json!!!")
        settings = load()
        assert settings == DEFAULTS


# ── Save ──────────────────────────────────────────────────────────

class TestSave:
    """Test persisting settings to disk."""

    def test_saves_json(self, isolate_settings):
        save({"resolution": "600"})
        data = json.loads(isolate_settings.read_text())
        assert data["resolution"] == "600"

    def test_file_permissions(self, isolate_settings):
        save(DEFAULTS)
        mode = oct(isolate_settings.stat().st_mode)[-3:]
        assert mode == "600"

    def test_creates_parent_dirs(self, isolate_settings):
        save(DEFAULTS)
        assert isolate_settings.exists()


# ── Update ────────────────────────────────────────────────────────

class TestUpdate:
    """Test atomic merge-and-save."""

    def test_updates_known_keys(self):
        result = update({"resolution": "600"})
        assert result["resolution"] == "600"
        # Verify it was persisted
        assert load()["resolution"] == "600"

    def test_ignores_unknown_keys(self):
        result = update({"evil_key": "value"})
        assert "evil_key" not in result

    def test_preserves_unmodified_keys(self):
        update({"resolution": "600"})
        result = update({"color_mode": "Grayscale8"})
        assert result["resolution"] == "600"
        assert result["color_mode"] == "Grayscale8"
