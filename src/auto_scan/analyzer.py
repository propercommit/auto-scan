"""Backward-compatibility shim — all logic now lives in recognition/.

Import from auto_scan.recognition directly for new code.
This module re-exports everything so existing imports keep working.
"""

from auto_scan.recognition.prompts import ALL_CATEGORIES  # noqa: F401
from auto_scan.recognition.engine import (                 # noqa: F401
    DocumentInfo,
    analyze_document,
    analyze_batch,
)

__all__ = [
    "ALL_CATEGORIES",
    "DocumentInfo",
    "analyze_document",
    "analyze_batch",
]
