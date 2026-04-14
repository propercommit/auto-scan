"""Generate realistic fake documents for testing OCR redaction.

Each generator creates a PIL Image simulating a scanned document at ~300 DPI
with text large enough for Tesseract to read reliably. Uses system fonts
when available, falling back to PIL's default at a scaled size.

Credit card numbers use valid Luhn checksums so they pass validation.
"""

from __future__ import annotations

import io
import random

from PIL import Image, ImageDraw, ImageFont

# ── Font loading ──────────────────────────────────────────────────

_FONT_CACHE: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}

# Standard Luhn-valid test card numbers
_VISA_TEST = "4111 1111 1111 1111"
_MC_TEST = "5500 0000 0000 0004"


def _get_font(size: int = 36) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a readable font at the given size. Caches for reuse."""
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    # Try common system fonts in order of preference
    candidates = [
        "/System/Library/Fonts/Helvetica.ttc",      # macOS
        "/System/Library/Fonts/SFNSMono.ttf",        # macOS
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # Linux
        "/usr/share/fonts/TTF/DejaVuSans.ttf",       # Arch Linux
        "arial.ttf",   # Windows
        "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        try:
            font = ImageFont.truetype(path, size)
            _FONT_CACHE[size] = font
            return font
        except (OSError, IOError):
            continue
    # Fallback: default font (small but functional)
    font = ImageFont.load_default()
    _FONT_CACHE[size] = font
    return font


# ── Canvas helpers ────────────────────────────────────────────────

# Simulate a scanned A4 page at ~200 DPI (enough for Tesseract, not too huge)
DOC_WIDTH = 1654
DOC_HEIGHT = 2340
BODY_SIZE = 36
HEADER_SIZE = 48
LINE_HEIGHT = 52
MARGIN = 80


def _draw_doc(
    width: int = DOC_WIDTH,
    height: int = DOC_HEIGHT,
) -> tuple[Image.Image, ImageDraw.Draw, ImageFont.FreeTypeFont | ImageFont.ImageFont, ImageFont.FreeTypeFont | ImageFont.ImageFont]:
    """Create a blank white document canvas with header and body fonts."""
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    return img, draw, _get_font(HEADER_SIZE), _get_font(BODY_SIZE)


def _text(draw: ImageDraw.Draw, x: int, y: int, text: str,
          font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> None:
    """Draw black text at position."""
    draw.text((x, y), text, fill="black", font=font)


def _to_jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


# ── Document generators ──────────────────────────────────────────


def invoice_with_iban() -> tuple[bytes, str, list[str]]:
    """Invoice with IBAN and total amount."""
    img, d, hf, bf = _draw_doc()
    y = MARGIN
    _text(d, MARGIN, y, "INVOICE #INV-2025-0847", hf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Schneider Consulting GmbH", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Bahnhofstrasse 42, 8001 Zurich", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Date: 15.03.2025", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Due: 15.04.2025", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Amount: CHF 4,350.00", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Please transfer to:", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "IBAN: CH93 0076 2011 6238 5295 7", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "BIC: UBSWCHZH80A", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Reference: Project Alpha Q1-2025", bf)
    return _to_jpeg(img), "Invoice with IBAN", ["iban"]


def medical_form_with_ssn() -> tuple[bytes, str, list[str]]:
    """US medical intake form with SSN."""
    img, d, hf, bf = _draw_doc()
    y = MARGIN
    _text(d, MARGIN, y, "PATIENT INTAKE FORM", hf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Mercy General Hospital", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Name: John A. Smith", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Date of Birth: 03/15/1985", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "SSN: 284-73-9164", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Address: 742 Evergreen Terrace", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Springfield, IL 62704", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Insurance: BlueCross BlueShield", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Policy: BCB-8847291", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Emergency Contact: (555) 234-5678", bf)
    return _to_jpeg(img), "Medical form with SSN", ["ssn", "dob", "phone"]


def credit_card_receipt() -> tuple[bytes, str, list[str]]:
    """Store receipt showing credit card number."""
    img, d, hf, bf = _draw_doc()
    y = MARGIN
    _text(d, MARGIN, y, "ELECTRONIC RECEIPT", hf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "TechStore Pro - Zurich Airport", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Date: 2025-03-20  14:32", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "MacBook Pro 14 M4       CHF 2,499.00", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "AppleCare+ 3yr          CHF   349.00", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Total:                  CHF 2,848.00", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Payment: VISA", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, f"Card: {_VISA_TEST}", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Auth: 847291", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Thank you for your purchase!", bf)
    return _to_jpeg(img), "Receipt with credit card", ["credit_card"]


def swiss_tax_form() -> tuple[bytes, str, list[str]]:
    """Swiss tax declaration with AHV number."""
    img, d, hf, bf = _draw_doc()
    y = MARGIN
    _text(d, MARGIN, y, "STEUERERKLAERUNG 2024", hf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Kanton Zurich - Gemeinde Winterthur", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Name: Mueller, Hans Peter", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Geburtsdatum: 22.08.1978", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "AHV-Nr: 756.1234.5678.97", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Adresse: Technikumstrasse 9", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "8400 Winterthur", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Steuerbares Einkommen: CHF 128,450", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Steuerbares Vermoegen: CHF 342,100", bf)
    return _to_jpeg(img), "Swiss tax form with AHV", ["ahv", "dob"]


def bank_statement_with_iban() -> tuple[bytes, str, list[str]]:
    """Bank account statement with IBAN."""
    img, d, hf, bf = _draw_doc()
    y = MARGIN
    _text(d, MARGIN, y, "UBS Switzerland AG", hf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Monthly Statement - March 2025", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Account Holder: Maria L. Bernasconi", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "IBAN: CH56 0023 0023 1098 7654 3", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "01.03  Opening Balance      CHF  15,432.80", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "05.03  Salary Credit        CHF   8,950.00", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "07.03  Rent Payment         CHF  -2,100.00", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "12.03  Migros               CHF    -187.35", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "31.03  Closing Balance      CHF  20,595.45", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "For questions: +41 44 234 56 78", bf)
    return _to_jpeg(img), "Bank statement with IBAN", ["iban", "phone"]


def employment_contract() -> tuple[bytes, str, list[str]]:
    """Employment contract with personal details."""
    img, d, hf, bf = _draw_doc()
    y = MARGIN
    _text(d, MARGIN, y, "EMPLOYMENT CONTRACT", hf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Acme Technologies AG", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Employee: Sophie Dubois", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Date of Birth: 14.06.1990", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Passport: C12345678", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Email: sophie.dubois@email.ch", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Position: Senior Software Engineer", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Start Date: 01.04.2025", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Annual Salary: CHF 142,000", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Salary IBAN: CH31 0076 2011 6238 5295 7", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "AHV-Nr: 756.9876.5432.10", bf)
    return _to_jpeg(img), "Employment contract", ["dob", "passport", "email", "iban", "ahv"]


def insurance_claim() -> tuple[bytes, str, list[str]]:
    """Insurance claim form with SSN and phone."""
    img, d, hf, bf = _draw_doc()
    y = MARGIN
    _text(d, MARGIN, y, "INSURANCE CLAIM FORM", hf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Progressive Auto Insurance", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Policyholder: Robert J. Williams", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Policy: PA-2024-884721", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "SSN: 531-62-8847", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Phone: (312) 555-0147", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Email: r.williams@outlook.com", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Incident Date: 02/28/2025", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Rear-end collision at intersection", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Estimated Damage: $4,200.00", bf)
    return _to_jpeg(img), "Insurance claim with SSN", ["ssn", "phone", "email"]


def utility_bill() -> tuple[bytes, str, list[str]]:
    """Utility bill with account and payment info."""
    img, d, hf, bf = _draw_doc()
    y = MARGIN
    _text(d, MARGIN, y, "EWZ - Elektrizitaetswerk Zuerich", hf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Stromrechnung - Maerz 2025", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Kunde: Thomas Keller", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Kundennummer: EWZ-2847391", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Verbrauch: 342 kWh", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Betrag: CHF 87.45", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Einzahlung auf:", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "IBAN: CH12 0483 5012 3456 7100 0", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Zahlbar bis: 30.04.2025", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Hotline: +41 58 319 41 11", bf)
    return _to_jpeg(img), "Utility bill with IBAN", ["iban", "phone"]


def passport_copy() -> tuple[bytes, str, list[str]]:
    """Photocopy of a passport page."""
    img, d, hf, bf = _draw_doc()
    y = MARGIN
    _text(d, MARGIN, y, "SCHWEIZERISCHE EIDGENOSSENSCHAFT", hf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "REISEPASS / PASSPORT", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Name: MEIER", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Vorname: ANNA LUCIA", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Geburtsdatum: 17.11.1992", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Passnummer: X8234567", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Gueltig bis: 17.11.2033", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "AHV: 756.4321.8765.43", bf)
    return _to_jpeg(img), "Passport copy", ["passport", "dob", "ahv"]


def rental_agreement() -> tuple[bytes, str, list[str]]:
    """Rental agreement with tenant details."""
    img, d, hf, bf = _draw_doc()
    y = MARGIN
    _text(d, MARGIN, y, "MIETVERTRAG / RENTAL AGREEMENT", hf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Immobilien Verwaltung Zurich AG", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Mieter: Luca Fontana", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Geburtsdatum: 05.09.1988", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "AHV-Nr: 756.5555.6666.77", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Telefon: +41 79 123 45 67", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Objekt: Langstrasse 88, 8004 Zurich", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Mietzins: CHF 2,450.00 / Monat", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Konto: IBAN CH88 0900 0000 8765 4321 0", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Kontakt: verwaltung@immo-zh.ch", bf)
    return _to_jpeg(img), "Rental agreement", ["dob", "ahv", "phone", "iban", "email"]


def payslip() -> tuple[bytes, str, list[str]]:
    """Monthly payslip with salary details."""
    img, d, hf, bf = _draw_doc()
    y = MARGIN
    _text(d, MARGIN, y, "LOHNABRECHNUNG / PAYSLIP", hf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "SwissTech Solutions AG - March 2025", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Mitarbeiter: Elena Petrova", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Personal-Nr: EMP-00842", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "AHV-Nr: 756.7777.8888.99", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Bruttolohn:      CHF  11,833.33", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "AHV/IV/EO:       CHF    -625.58", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Nettolohn:       CHF   9,300.25", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Auszahlung: CH45 0070 0110 0009 5678 4", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "HR: hr@swisstech.ch / +41 44 888 99 00", bf)
    return _to_jpeg(img), "Payslip with AHV + IBAN", ["ahv", "iban", "email", "phone"]


def clean_letter() -> tuple[bytes, str, list[str]]:
    """Regular business letter with no sensitive data."""
    img, d, hf, bf = _draw_doc()
    y = MARGIN
    _text(d, MARGIN, y, "RE: Project Update - Q1 2025", hf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Dear Mr. Thompson,", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "I am writing to provide an update on the", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "current status of the Alpine Renovation.", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Phase one has been completed on schedule.", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "The team delivered all milestones within", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "the agreed budget of two million francs.", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "We expect the next phase to begin in April", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "with an estimated duration of eight weeks.", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Best regards,", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Andreas Huber, Project Manager", bf)
    return _to_jpeg(img), "Clean letter (no sensitive data)", []


def order_confirmation() -> tuple[bytes, str, list[str]]:
    """Online order confirmation with card details."""
    img, d, hf, bf = _draw_doc()
    y = MARGIN
    _text(d, MARGIN, y, "ORDER CONFIRMATION", hf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Digitec Galaxus AG", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Order DG-9947281", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Customer: Marc Zimmermann", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Email: marc.zimm@gmail.com", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Sony WH-1000XM5      CHF   349.00", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "USB-C Cable 2m        CHF    12.90", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Total:                CHF   361.90", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Payment: Mastercard", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, f"Card: {_MC_TEST}", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Support: support@digitec.ch", bf)
    return _to_jpeg(img), "Order with credit card", ["credit_card", "email"]


def doctor_referral() -> tuple[bytes, str, list[str]]:
    """Doctor referral letter with patient SSN."""
    img, d, hf, bf = _draw_doc()
    y = MARGIN
    _text(d, MARGIN, y, "REFERRAL LETTER", hf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Dr. Sarah Chen, MD - Internal Medicine", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Patient: David R. Anderson", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "DOB: 08/22/1976", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "SSN: 447-91-3258", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Referring to: Dr. Michael Torres", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Reason: Evaluation of persistent", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "chest pain and abnormal stress test.", bf); y += LINE_HEIGHT * 2
    _text(d, MARGIN, y, "Contact: (617) 555-0234", bf); y += LINE_HEIGHT
    _text(d, MARGIN, y, "Email: dr.chen@mercymed.org", bf)
    return _to_jpeg(img), "Doctor referral with SSN", ["ssn", "dob", "phone", "email"]


# ── Registry ─────────────────────────────────────────────────────

ALL_TEST_DOCUMENTS = [
    invoice_with_iban,
    medical_form_with_ssn,
    credit_card_receipt,
    swiss_tax_form,
    bank_statement_with_iban,
    employment_contract,
    insurance_claim,
    utility_bill,
    passport_copy,
    rental_agreement,
    payslip,
    clean_letter,
    order_confirmation,
    doctor_referral,
]


def pick_random(n: int = 3) -> list[tuple[bytes, str, list[str]]]:
    """Pick n random test documents. Returns list of (jpeg_bytes, name, expected_types)."""
    chosen = random.sample(ALL_TEST_DOCUMENTS, min(n, len(ALL_TEST_DOCUMENTS)))
    return [fn() for fn in chosen]
