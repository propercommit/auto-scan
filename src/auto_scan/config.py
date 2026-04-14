from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


@dataclass
class Config:
    api_key: str
    scanner_ip: str | None
    output_dir: Path
    resolution: int
    color_mode: str
    scan_source: str
    scan_format: str
    claude_model: str


def load_config(**overrides) -> Config:
    """Load config from .env file and environment variables.

    Keyword arguments override environment values (used by CLI flags).
    """
    load_dotenv()

    api_key = overrides.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is required. Set it in .env or as an environment variable."
        )

    output_dir = overrides.get("output_dir") or os.environ.get(
        "OUTPUT_DIR", "~/Documents/Scans"
    )

    return Config(
        api_key=api_key,
        scanner_ip=overrides.get("scanner_ip") or os.environ.get("SCANNER_IP"),
        output_dir=Path(output_dir).expanduser(),
        resolution=int(
            overrides.get("resolution") or os.environ.get("SCAN_RESOLUTION", "300")
        ),
        color_mode=overrides.get("color_mode")
        or os.environ.get("SCAN_COLOR_MODE", "RGB24"),
        scan_source=overrides.get("scan_source")
        or os.environ.get("SCAN_SOURCE", "Feeder"),
        scan_format=os.environ.get("SCAN_FORMAT", "image/jpeg"),
        claude_model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
    )
