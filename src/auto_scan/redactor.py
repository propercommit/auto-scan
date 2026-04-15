"""Redact sensitive information from scanned images before sending to AI.

Uses local OCR (pytesseract) to detect text regions, matches sensitive
patterns via regex, and blacks out matching areas in the image.
The redacted image is sent to the API; the original is kept for PDF output.
"""

from __future__ import annotations

import io
import re
import shutil
import sys
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFilter

# ── Sensitive patterns ──────────────────────────────────────────────

PATTERNS: dict[str, re.Pattern] = {
    # Social Security Numbers (US) — exclude invalid ranges (000/666/9xx area, 00 group, 0000 serial)
    "ssn": re.compile(r"\b(?!000|666|9\d\d)\d{3}[-.\s]?(?!00)\d{2}[-.\s]?(?!0000)\d{4}\b"),
    # Swiss AHV/AVS number: 756.XXXX.XXXX.XX — tolerate OCR-inserted spaces around dots
    "ahv": re.compile(r"\b756\s?[.\s]\s?\d{4}\s?[.\s]\s?\d{4}\s?[.\s]\s?\d{2}\b"),
    # Credit card numbers (13-19 digits, optionally grouped) — Luhn validated in _luhn_check
    "credit_card": re.compile(
        r"\b(?:\d{4}[-\s]?){3,4}\d{1,4}\b"
    ),
    # IBAN (2-letter country + 2 check digits + up to 30 alphanumeric)
    "iban": re.compile(r"\b[A-Z]{2}\d{2}[\s]?[\dA-Z]{4}[\s]?(?:[\dA-Z]{4}[\s]?){1,7}[\dA-Z]{1,4}\b"),
    # Phone numbers (international or local with various separators)
    "phone": re.compile(r"\b(?:\+\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{2,4}[-.\s]?\d{0,4}\b"),
    # Email addresses — tolerate OCR-inserted spaces around @
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+\s?@\s?[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    # Date of birth patterns (DD/MM/YYYY, DD.MM.YYYY, etc.)
    "dob": re.compile(r"\b\d{1,2}[./]\d{1,2}[./]\d{4}\b"),
    # Passport numbers (common formats)
    "passport": re.compile(r"\b[A-Z]\d{7,8}\b"),
    # Postal/mailing addresses — street lines and distinctive postal code formats
    "address": re.compile(
        r"(?:"
        # ── French streets: "12 rue de la Paix", "42 bis avenue des Champs-Élysées" ──
        r"\b\d{1,5}\s*,?\s*(?:bis|ter)?\s*,?\s*"
        r"(?:rue|avenue|av|boulevard|blvd|all[ée]e|all|chemin|ch|"
        r"place|pl|impasse|imp|passage|pass|cours|quai|"
        r"route|rte|voie|faubourg|fg|r[ée]sidence|r[ée]s|"
        r"lotissement|hameau|lieu[- ]?dit)\b"
        r"(?:\s+\S+){0,5}"
        r"|"
        # ── P.O. Box / Boîte Postale / Postfach ──
        r"\b(?:b\.?p\.?|bo[îi]te\s+postale|p\.?o\.?\s*box|postfach|casier\s+postal)"
        r"\s*\d+"
        r"|"
        # ── German streets: "Musterstraße 42", "Berliner Straße 42" ──
        # Note: stra(?:ß|ss)e handles both "Straße" (ß=1 char) and "Strasse" (ss=2 chars)
        r"\b(?:"
        r"[A-Za-z\u00C0-\u024F]+(?:stra(?:ß|ss)e|str\.?|gasse|weg|platz|allee|ring|damm|ufer)"
        r"|"
        r"(?:[A-Za-z\u00C0-\u024F]+\s+){1,2}"
        r"(?:Stra(?:ß|ss)e|Gasse|Weg|Platz|Allee|Ring|Damm|Ufer)"
        r")\s+\d{1,5}[a-z]?\b"
        r"|"
        # ── English streets: "123 Main Street", "456 Oak Hill Road" ──
        r"\b\d{1,5}\s+(?:[A-Za-z\u00C0-\u024F]+\.?\s+){1,3}"
        r"(?:street|st|avenue|ave|boulevard|blvd|road|rd|drive|dr|"
        r"lane|ln|court|ct|circle|cir|way|terrace|ter|"
        r"parkway|pkwy|highway|hwy|square|sq)\b"
        r"(?:[\s,]+(?:apt|suite|unit|ste|fl|floor|bldg|building)\.?\s*\w+)?"
        r"|"
        # ── Postal code + city (distinctive formats only) ──
        # French CEDEX
        r"\b\d{5}\s+\S+(?:\s+\S+)*\s+cedex(?:\s+\d+)?\b"
        r"|"
        # Swiss: CH-1234 City
        r"\bCH[-\s]?\d{4}\s+\S{3,}"
        r"|"
        # UK postal codes: SW1A 1AA, EC2R 8AH, W1D 4FA
        r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b"
        r"|"
        # US: City, ST 12345(-6789)
        r"\b[A-Za-z]+(?:\s+[A-Za-z]+)*,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?"
        r")",
        re.IGNORECASE,
    ),
}

# Default: redact all patterns.
DEFAULT_ENABLED = {"ssn", "ahv", "credit_card", "iban", "phone", "email", "dob", "passport", "address"}


def _luhn_check(num_str: str) -> bool:
    """Validate a number string with the Luhn algorithm (used by all major card networks)."""
    digits = [int(d) for d in num_str if d.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass
class RedactionResult:
    """Result of redacting an image."""
    redacted_image: bytes
    redaction_count: int = 0
    redacted_types: list[str] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""


def _has_tesseract() -> bool:
    """Check if tesseract OCR is available on the system."""
    return shutil.which("tesseract") is not None


def redact_image(
    image_data: bytes,
    enabled_patterns: set[str] | None = None,
) -> RedactionResult:
    """Redact sensitive information from an image.

    Args:
        image_data: Raw image bytes (JPEG).
        enabled_patterns: Set of pattern names to redact. None = defaults.

    Returns:
        RedactionResult with redacted image bytes and stats.
    """
    if not _has_tesseract():
        return RedactionResult(
            redacted_image=image_data,
            skipped=True,
            skip_reason="tesseract is not installed (brew install tesseract)",
        )

    try:
        import pytesseract
    except ImportError:
        return RedactionResult(
            redacted_image=image_data,
            skipped=True,
            skip_reason="pytesseract Python package is not installed (pip install pytesseract)",
        )

    patterns = enabled_patterns or DEFAULT_ENABLED
    active = {name: pat for name, pat in PATTERNS.items() if name in patterns}

    if not active:
        return RedactionResult(redacted_image=image_data)

    img = Image.open(io.BytesIO(image_data))
    if img.mode != "RGB":
        img = img.convert("RGB")

    # Get word-level bounding boxes from tesseract
    try:
        ocr_data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    except Exception as e:
        print(f"  Redaction OCR failed: {e}", file=sys.stderr)
        return RedactionResult(
            redacted_image=image_data,
            skipped=True,
            skip_reason=f"OCR failed: {e}",
        )

    # Build text spans with positions: group consecutive words into lines
    n = len(ocr_data["text"])
    words = []
    for i in range(n):
        text = ocr_data["text"][i].strip()
        if not text:
            continue
        words.append({
            "text": text,
            "left": ocr_data["left"][i],
            "top": ocr_data["top"][i],
            "width": ocr_data["width"][i],
            "height": ocr_data["height"][i],
            "line_num": ocr_data["line_num"][i],
            "block_num": ocr_data["block_num"][i],
        })

    # Group words into lines for multi-word pattern matching
    lines: dict[tuple[int, int], list[dict]] = {}
    for w in words:
        key = (w["block_num"], w["line_num"])
        lines.setdefault(key, []).append(w)

    # Find sensitive matches and collect bounding boxes to redact
    redact_boxes = []
    found_types = set()

    for line_words in lines.values():
        line_text = " ".join(w["text"] for w in line_words)
        for name, pattern in active.items():
            for match in pattern.finditer(line_text):
                # Credit card: require Luhn checksum to reduce false positives
                if name == "credit_card" and not _luhn_check(match.group()):
                    continue
                # Map character positions back to word bounding boxes
                char_pos = 0
                match_start, match_end = match.start(), match.end()
                for w in line_words:
                    word_start = char_pos
                    word_end = char_pos + len(w["text"])
                    # If this word overlaps with the match, redact it
                    if word_end > match_start and word_start < match_end:
                        # Scale padding with word height to handle different DPIs
                        pad = max(4, w["height"] // 4)
                        redact_boxes.append((
                            w["left"] - pad,
                            w["top"] - pad,
                            w["left"] + w["width"] + pad,
                            w["top"] + w["height"] + pad,
                        ))
                        found_types.add(name)
                    char_pos = word_end + 1  # +1 for the space

    if not redact_boxes:
        return RedactionResult(redacted_image=image_data)

    # Draw black rectangles over sensitive regions
    draw = ImageDraw.Draw(img)
    for box in redact_boxes:
        draw.rectangle(box, fill=(0, 0, 0))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    redacted_bytes = buf.getvalue()

    print(
        f"  Redacted {len(redact_boxes)} region(s) [{', '.join(sorted(found_types))}]",
        file=sys.stderr,
    )
    return RedactionResult(
        redacted_image=redacted_bytes,
        redaction_count=len(redact_boxes),
        redacted_types=sorted(found_types),
    )
