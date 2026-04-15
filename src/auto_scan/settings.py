"""Persistent settings for the GUI.

Stores user preferences (output directory, scanner IP, resolution, etc.)
as JSON on disk at ~/.auto_scan/settings.json. Separate from config.py
which handles environment-based configuration for the CLI.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


# Defaults for every setting the GUI knows about.
# New settings should be added here with sensible defaults.
DEFAULTS = {
    "output_dir": str(Path("~/Documents/Scans").expanduser()),
    "scanner_ip": "",
    "resolution": "300",
    "color_mode": "RGB24",
    "scan_source": "Feeder",
    "mode": "auto",
    "daily_budget": "0",
    "redact_enabled": True,
}


def _settings_path() -> Path:
    """Return the path to the persistent settings JSON file."""
    return Path.home() / ".auto_scan" / "settings.json"


def load() -> dict:
    """Load persistent settings from disk, falling back to defaults.

    Always returns a complete dict with every key from DEFAULTS present.
    Unknown keys from disk are preserved (forward compatibility).
    """
    path = _settings_path()
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return {**DEFAULTS, **data}
        except Exception:
            pass
    return dict(DEFAULTS)


def save(settings: dict) -> None:
    """Persist settings to disk with owner-only permissions."""
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    os.chmod(path, 0o600)


def update(changes: dict) -> dict:
    """Merge changes into current settings and save.

    Only keys that exist in DEFAULTS are updated (prevents injection
    of arbitrary keys from the frontend). Returns the merged result.
    """
    current = load()
    for key in DEFAULTS:
        if key in changes:
            current[key] = changes[key]
    save(current)
    return current
