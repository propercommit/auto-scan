"""Tests for the OCR-based redaction module.

Tests pattern matching and Luhn validation without requiring tesseract.
The actual OCR + bounding-box redaction path requires tesseract, tested
separately when available.
"""

import pytest

from auto_scan.redactor import (
    PATTERNS,
    DEFAULT_ENABLED,
    _luhn_check,
    RedactionResult,
    _has_tesseract,
    redact_image,
)
from helpers import make_test_image


# ── Luhn check ────────────────────────────────────────────────────

class TestLuhnCheck:
    """Validate the Luhn algorithm used to filter credit card false positives."""

    def test_valid_visa(self):
        assert _luhn_check("4111111111111111") is True

    def test_valid_mastercard(self):
        assert _luhn_check("5500000000000004") is True

    def test_valid_amex(self):
        assert _luhn_check("378282246310005") is True

    def test_invalid_number(self):
        assert _luhn_check("1234567890123456") is False

    def test_too_short(self):
        assert _luhn_check("123456789") is False

    def test_too_long(self):
        assert _luhn_check("12345678901234567890") is False

    def test_with_spaces(self):
        assert _luhn_check("4111 1111 1111 1111") is True

    def test_with_dashes(self):
        assert _luhn_check("4111-1111-1111-1111") is True


# ── Pattern matching ──────────────────────────────────────────────

class TestPatternSSN:
    """US Social Security Number patterns."""

    def test_matches_standard_ssn(self):
        assert PATTERNS["ssn"].search("123-45-6789")

    def test_matches_no_separators(self):
        assert PATTERNS["ssn"].search("123456789")

    def test_rejects_invalid_area_000(self):
        assert not PATTERNS["ssn"].search("000-12-3456")

    def test_rejects_invalid_area_666(self):
        assert not PATTERNS["ssn"].search("666-12-3456")

    def test_rejects_9xx_area(self):
        assert not PATTERNS["ssn"].search("900-12-3456")


class TestPatternAHV:
    """Swiss AHV/AVS number patterns."""

    def test_matches_standard_ahv(self):
        assert PATTERNS["ahv"].search("756.1234.5678.90")

    def test_matches_ahv_with_spaces(self):
        # OCR often inserts spaces around dots
        assert PATTERNS["ahv"].search("756 . 1234 . 5678 . 90")

    def test_no_match_wrong_prefix(self):
        assert not PATTERNS["ahv"].search("757.1234.5678.90")


class TestPatternCreditCard:
    """Credit card number patterns (regex match, Luhn validated separately)."""

    def test_matches_grouped_16_digit(self):
        assert PATTERNS["credit_card"].search("4111 1111 1111 1111")

    def test_matches_dashed_16_digit(self):
        assert PATTERNS["credit_card"].search("4111-1111-1111-1111")

    def test_matches_continuous_16_digit(self):
        assert PATTERNS["credit_card"].search("4111111111111111")

    def test_matches_amex_uniform_groups(self):
        # AMEX uses 4-6-5 grouping which the regex may not match.
        # Verify it matches when formatted as uniform 4-digit groups.
        assert PATTERNS["credit_card"].search("3782 8224 6310 005")


class TestPatternIBAN:
    """IBAN patterns."""

    def test_matches_german_iban(self):
        assert PATTERNS["iban"].search("DE89 3704 0044 0532 0130 00")

    def test_matches_swiss_iban(self):
        assert PATTERNS["iban"].search("CH93 0076 2011 6238 5295 7")

    def test_matches_french_iban(self):
        assert PATTERNS["iban"].search("FR76 3000 6000 0112 3456 7890 189")


class TestPatternPhone:
    """Phone number patterns."""

    def test_matches_international(self):
        assert PATTERNS["phone"].search("+41 44 123 45 67")

    def test_matches_us_format(self):
        assert PATTERNS["phone"].search("(555) 123-4567")

    def test_matches_european(self):
        # German landline (fits the pattern's digit group requirements)
        assert PATTERNS["phone"].search("+49 30 123 4567")


class TestPatternEmail:
    """Email address patterns."""

    def test_matches_standard_email(self):
        assert PATTERNS["email"].search("user@example.com")

    def test_matches_email_with_dots(self):
        assert PATTERNS["email"].search("first.last@company.co.uk")

    def test_matches_ocr_spaces(self):
        # OCR may insert spaces around @
        assert PATTERNS["email"].search("user @ example.com")


class TestPatternDOB:
    """Date of birth patterns."""

    def test_matches_dd_mm_yyyy(self):
        assert PATTERNS["dob"].search("15/03/1990")

    def test_matches_dd_dot_mm_dot_yyyy(self):
        assert PATTERNS["dob"].search("15.03.1990")


class TestPatternPassport:
    """Passport number patterns."""

    def test_matches_standard_format(self):
        assert PATTERNS["passport"].search("X12345678")

    def test_no_match_lowercase(self):
        # Pattern requires uppercase letter start
        assert not PATTERNS["passport"].search("x12345678")


class TestPatternAddress:
    """Postal/mailing address patterns."""

    # ── French addresses ──
    def test_matches_french_rue(self):
        assert PATTERNS["address"].search("12 rue de la Paix")

    def test_matches_french_avenue(self):
        assert PATTERNS["address"].search("42 bis avenue des Champs")

    def test_matches_french_boulevard(self):
        assert PATTERNS["address"].search("8 boulevard Saint-Germain")

    def test_matches_french_cedex(self):
        assert PATTERNS["address"].search("75008 Paris cedex")

    # ── German addresses ──
    def test_matches_german_strasse(self):
        assert PATTERNS["address"].search("Musterstrasse 42")

    def test_matches_german_strasse_eszett(self):
        assert PATTERNS["address"].search("Musterstraße 42")

    def test_matches_german_weg(self):
        assert PATTERNS["address"].search("Feldweg 7")

    def test_matches_german_platz(self):
        assert PATTERNS["address"].search("Marktplatz 1")

    def test_matches_german_compound(self):
        assert PATTERNS["address"].search("Berliner Straße 42")

    # ── English addresses ──
    def test_matches_english_street(self):
        assert PATTERNS["address"].search("123 Main Street")

    def test_matches_english_avenue(self):
        assert PATTERNS["address"].search("456 Oak Avenue")

    def test_matches_english_road(self):
        assert PATTERNS["address"].search("789 Park Road")

    def test_matches_english_drive(self):
        assert PATTERNS["address"].search("321 Cedar Drive")

    # ── PO Box ──
    def test_matches_po_box(self):
        assert PATTERNS["address"].search("P.O. Box 1234")

    def test_matches_postfach(self):
        assert PATTERNS["address"].search("Postfach 5678")

    def test_matches_boite_postale(self):
        assert PATTERNS["address"].search("Boîte Postale 90")

    # ── Postal codes ──
    def test_matches_swiss_postal_code(self):
        assert PATTERNS["address"].search("CH-8001 Zurich")

    def test_matches_uk_postal_code(self):
        assert PATTERNS["address"].search("SW1A 1AA")

    def test_matches_us_city_state_zip(self):
        assert PATTERNS["address"].search("Springfield, IL 62704")


# ── Default enabled set ───────────────────────────────────────────

class TestDefaultEnabled:
    """Verify all pattern types are enabled by default."""

    def test_all_patterns_enabled(self):
        assert DEFAULT_ENABLED == {
            "ssn", "ahv", "credit_card", "iban",
            "phone", "email", "dob", "passport", "address",
        }

    def test_default_covers_all_patterns(self):
        assert DEFAULT_ENABLED == set(PATTERNS.keys())


# ── Redact image (skip/fallback paths) ────────────────────────────

class TestRedactImageFallbacks:
    """Test redact_image when tesseract is unavailable."""

    def test_returns_original_when_no_tesseract(self, monkeypatch):
        # Force _has_tesseract to return False
        monkeypatch.setattr("auto_scan.redactor._has_tesseract", lambda: False)
        img_data = make_test_image()
        result = redact_image(img_data)
        assert result.skipped is True
        assert result.redacted_image == img_data
        assert "tesseract" in result.skip_reason

    def test_no_active_patterns_returns_original(self, monkeypatch):
        monkeypatch.setattr("auto_scan.redactor._has_tesseract", lambda: True)
        # pytesseract must be importable for this path
        try:
            import pytesseract  # noqa: F401
        except ImportError:
            pytest.skip("pytesseract not installed")
        img_data = make_test_image()
        result = redact_image(img_data, enabled_patterns=set())
        assert result.skipped is False
        assert result.redaction_count == 0
