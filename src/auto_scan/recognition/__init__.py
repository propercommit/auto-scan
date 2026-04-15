"""Document recognition: AI-powered classification and sorting.

This package separates the model instructions (prompts) from the
execution logic (engine), making it easy to tune prompts without
touching code.

Modules:
    prompts  — All text sent to the AI model (categories, instructions)
    engine   — API calls, image preparation, JSON parsing, verification
"""

from auto_scan.recognition.prompts import ALL_CATEGORIES
from auto_scan.recognition.engine import (
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
