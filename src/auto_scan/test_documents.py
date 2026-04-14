"""Generate realistic fake documents for testing OCR redaction.

Each generator creates a PIL Image simulating a scanned document with
specific types of sensitive data embedded in realistic context.
"""

from __future__ import annotations

import io
import random

from PIL import Image, ImageDraw


def _draw_doc(width: int = 800, height: int = 400) -> tuple[Image.Image, ImageDraw.Draw]:
    """Create a blank white document canvas."""
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    return img, draw


def _to_jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


# ── Document generators ────────────────────────────────────────────


def invoice_with_iban() -> tuple[bytes, str, list[str]]:
    """Invoice with IBAN and total amount."""
    img, d = _draw_doc()
    d.text((40, 30), "INVOICE #INV-2025-0847", fill="black")
    d.text((40, 60), "Schneider Consulting GmbH", fill="black")
    d.text((40, 90), "Bahnhofstrasse 42, 8001 Zurich", fill="black")
    d.text((40, 130), "Date: 15.03.2025", fill="black")
    d.text((40, 160), "Due: 15.04.2025", fill="black")
    d.text((40, 200), "Amount: CHF 4,350.00", fill="black")
    d.text((40, 250), "Please transfer to:", fill="black")
    d.text((40, 280), "IBAN: CH93 0076 2011 6238 5295 7", fill="black")
    d.text((40, 310), "BIC: UBSWCHZH80A", fill="black")
    d.text((40, 350), "Reference: Project Alpha Q1-2025", fill="black")
    return _to_jpeg(img), "Invoice with IBAN", ["iban"]


def medical_form_with_ssn() -> tuple[bytes, str, list[str]]:
    """US medical intake form with SSN."""
    img, d = _draw_doc()
    d.text((40, 30), "PATIENT INTAKE FORM", fill="black")
    d.text((40, 60), "Mercy General Hospital", fill="black")
    d.text((40, 100), "Name: John A. Smith", fill="black")
    d.text((40, 130), "Date of Birth: 03/15/1985", fill="black")
    d.text((40, 160), "SSN: 284-73-9164", fill="black")
    d.text((40, 200), "Address: 742 Evergreen Terrace", fill="black")
    d.text((40, 230), "Springfield, IL 62704", fill="black")
    d.text((40, 270), "Insurance: BlueCross BlueShield", fill="black")
    d.text((40, 300), "Policy #: BCB-8847291", fill="black")
    d.text((40, 340), "Emergency Contact: Jane Smith (555) 234-5678", fill="black")
    return _to_jpeg(img), "Medical form with SSN", ["ssn", "phone", "dob"]


def credit_card_receipt() -> tuple[bytes, str, list[str]]:
    """Store receipt showing credit card number."""
    img, d = _draw_doc(700, 450)
    d.text((40, 30), "ELECTRONIC RECEIPT", fill="black")
    d.text((40, 60), "TechStore Pro - Zurich Airport", fill="black")
    d.text((40, 90), "Date: 2025-03-20 14:32:07", fill="black")
    d.text((40, 130), "Item: MacBook Pro 14\" M4          CHF 2,499.00", fill="black")
    d.text((40, 160), "Item: AppleCare+ 3yr               CHF   349.00", fill="black")
    d.text((40, 190), "-------------------------------------------", fill="black")
    d.text((40, 220), "Subtotal:                          CHF 2,848.00", fill="black")
    d.text((40, 250), "VAT 8.1%:                          CHF   230.69", fill="black")
    d.text((40, 280), "TOTAL:                             CHF 3,078.69", fill="black")
    d.text((40, 320), "Payment: VISA", fill="black")
    d.text((40, 350), "Card: 4532 8901 2345 6789", fill="black")
    d.text((40, 380), "Auth: 847291", fill="black")
    d.text((40, 410), "Thank you for your purchase!", fill="black")
    return _to_jpeg(img), "Receipt with credit card", ["credit_card"]


def swiss_tax_form() -> tuple[bytes, str, list[str]]:
    """Swiss tax declaration with AHV number."""
    img, d = _draw_doc()
    d.text((40, 30), "STEUERERKLAERUNG 2024", fill="black")
    d.text((40, 60), "Kanton Zurich - Gemeinde Winterthur", fill="black")
    d.text((40, 100), "Name: Mueller, Hans Peter", fill="black")
    d.text((40, 130), "Geburtsdatum: 22.08.1978", fill="black")
    d.text((40, 160), "AHV-Nr: 756.1234.5678.97", fill="black")
    d.text((40, 200), "Adresse: Technikumstrasse 9", fill="black")
    d.text((40, 230), "8400 Winterthur", fill="black")
    d.text((40, 270), "Steuerbares Einkommen: CHF 128,450", fill="black")
    d.text((40, 300), "Steuerbares Vermoegen: CHF 342,100", fill="black")
    d.text((40, 340), "Steuerperiode: 01.01.2024 - 31.12.2024", fill="black")
    return _to_jpeg(img), "Swiss tax form with AHV", ["ahv", "dob"]


def bank_statement_with_iban() -> tuple[bytes, str, list[str]]:
    """Bank account statement with IBAN."""
    img, d = _draw_doc(800, 500)
    d.text((40, 30), "UBS Switzerland AG", fill="black")
    d.text((40, 60), "Monthly Statement - March 2025", fill="black")
    d.text((40, 100), "Account Holder: Maria L. Bernasconi", fill="black")
    d.text((40, 130), "IBAN: CH56 0023 0023 1098 7654 3", fill="black")
    d.text((40, 170), "01.03  Opening Balance             CHF  15,432.80", fill="black")
    d.text((40, 200), "05.03  Salary Credit               CHF   8,950.00", fill="black")
    d.text((40, 230), "07.03  Rent Payment                CHF  -2,100.00", fill="black")
    d.text((40, 260), "12.03  Migros                      CHF    -187.35", fill="black")
    d.text((40, 290), "15.03  Transfer to DE89 3704 0044 0532 0130 00", fill="black")
    d.text((40, 310), "                                   CHF  -1,500.00", fill="black")
    d.text((40, 350), "31.03  Closing Balance             CHF  20,595.45", fill="black")
    d.text((40, 400), "For questions: +41 44 234 56 78", fill="black")
    return _to_jpeg(img), "Bank statement with IBAN", ["iban", "phone"]


def employment_contract() -> tuple[bytes, str, list[str]]:
    """Employment contract with personal details."""
    img, d = _draw_doc(800, 450)
    d.text((40, 30), "EMPLOYMENT CONTRACT", fill="black")
    d.text((40, 60), "Acme Technologies AG", fill="black")
    d.text((40, 100), "Employee: Sophie Dubois", fill="black")
    d.text((40, 130), "Date of Birth: 14.06.1990", fill="black")
    d.text((40, 160), "Passport: C12345678", fill="black")
    d.text((40, 190), "Email: sophie.dubois@email.ch", fill="black")
    d.text((40, 230), "Position: Senior Software Engineer", fill="black")
    d.text((40, 260), "Start Date: 01.04.2025", fill="black")
    d.text((40, 290), "Annual Salary: CHF 142,000", fill="black")
    d.text((40, 330), "IBAN for salary: CH31 0076 2011 6238 5295 7", fill="black")
    d.text((40, 360), "AHV-Nr: 756.9876.5432.10", fill="black")
    d.text((40, 400), "Signed: _______________  Date: 15.03.2025", fill="black")
    return _to_jpeg(img), "Employment contract", ["dob", "passport", "email", "iban", "ahv"]


def insurance_claim() -> tuple[bytes, str, list[str]]:
    """Insurance claim form with SSN and phone."""
    img, d = _draw_doc()
    d.text((40, 30), "INSURANCE CLAIM FORM", fill="black")
    d.text((40, 60), "Progressive Auto Insurance", fill="black")
    d.text((40, 100), "Policyholder: Robert J. Williams", fill="black")
    d.text((40, 130), "Policy #: PA-2024-884721", fill="black")
    d.text((40, 160), "SSN: 531-62-8847", fill="black")
    d.text((40, 200), "Phone: (312) 555-0147", fill="black")
    d.text((40, 230), "Email: r.williams@outlook.com", fill="black")
    d.text((40, 270), "Incident Date: 02/28/2025", fill="black")
    d.text((40, 300), "Description: Rear-end collision at intersection", fill="black")
    d.text((40, 340), "Estimated Damage: $4,200.00", fill="black")
    return _to_jpeg(img), "Insurance claim with SSN", ["ssn", "phone", "email"]


def utility_bill() -> tuple[bytes, str, list[str]]:
    """Utility bill with account and payment info."""
    img, d = _draw_doc()
    d.text((40, 30), "EWZ - Elektrizitaetswerk der Stadt Zuerich", fill="black")
    d.text((40, 60), "Stromrechnung - Maerz 2025", fill="black")
    d.text((40, 100), "Kunde: Thomas Keller", fill="black")
    d.text((40, 130), "Kundennummer: EWZ-2847391", fill="black")
    d.text((40, 170), "Verbrauch: 342 kWh", fill="black")
    d.text((40, 200), "Betrag: CHF 87.45", fill="black")
    d.text((40, 240), "Einzahlung auf:", fill="black")
    d.text((40, 270), "IBAN: CH12 0483 5012 3456 7100 0", fill="black")
    d.text((40, 300), "Zahlbar bis: 30.04.2025", fill="black")
    d.text((40, 340), "Hotline: +41 58 319 41 11", fill="black")
    return _to_jpeg(img), "Utility bill with IBAN", ["iban", "phone"]


def passport_copy() -> tuple[bytes, str, list[str]]:
    """Photocopy of a passport page."""
    img, d = _draw_doc(700, 350)
    d.text((40, 30), "SCHWEIZERISCHE EIDGENOSSENSCHAFT", fill="black")
    d.text((40, 60), "SWISS CONFEDERATION", fill="black")
    d.text((40, 100), "REISEPASS / PASSPORT", fill="black")
    d.text((40, 140), "Name: MEIER", fill="black")
    d.text((40, 170), "Vorname: ANNA LUCIA", fill="black")
    d.text((40, 200), "Geburtsdatum: 17.11.1992", fill="black")
    d.text((40, 230), "Passnummer: X8234567", fill="black")
    d.text((40, 260), "Gueltig bis: 17.11.2033", fill="black")
    d.text((40, 300), "AHV: 756.4321.8765.43", fill="black")
    return _to_jpeg(img), "Passport copy", ["passport", "dob", "ahv"]


def rental_agreement() -> tuple[bytes, str, list[str]]:
    """Rental agreement with tenant details."""
    img, d = _draw_doc(800, 500)
    d.text((40, 30), "MIETVERTRAG / RENTAL AGREEMENT", fill="black")
    d.text((40, 60), "Immobilien Verwaltung Zurich AG", fill="black")
    d.text((40, 100), "Mieter: Luca Fontana", fill="black")
    d.text((40, 130), "Geburtsdatum: 05.09.1988", fill="black")
    d.text((40, 160), "AHV-Nr: 756.5555.6666.77", fill="black")
    d.text((40, 190), "Telefon: +41 79 123 45 67", fill="black")
    d.text((40, 230), "Objekt: Langstrasse 88, 8004 Zurich", fill="black")
    d.text((40, 260), "Mietzins: CHF 2,450.00 / Monat", fill="black")
    d.text((40, 290), "Nebenkosten: CHF 280.00 / Monat", fill="black")
    d.text((40, 330), "Mietbeginn: 01.05.2025", fill="black")
    d.text((40, 360), "Kaution: CHF 7,350.00", fill="black")
    d.text((40, 390), "Konto: IBAN CH88 0900 0000 8765 4321 0", fill="black")
    d.text((40, 430), "Kontakt: verwaltung@immo-zh.ch", fill="black")
    return _to_jpeg(img), "Rental agreement", ["dob", "ahv", "phone", "iban", "email"]


def payslip() -> tuple[bytes, str, list[str]]:
    """Monthly payslip with salary details."""
    img, d = _draw_doc(800, 500)
    d.text((40, 30), "LOHNABRECHNUNG / PAYSLIP", fill="black")
    d.text((40, 60), "SwissTech Solutions AG - March 2025", fill="black")
    d.text((40, 100), "Mitarbeiter: Elena Petrova", fill="black")
    d.text((40, 130), "Personal-Nr: EMP-00842", fill="black")
    d.text((40, 160), "AHV-Nr: 756.7777.8888.99", fill="black")
    d.text((40, 200), "Bruttolohn:           CHF  11,833.33", fill="black")
    d.text((40, 230), "AHV/IV/EO:            CHF    -625.58", fill="black")
    d.text((40, 260), "BVG:                  CHF    -487.50", fill="black")
    d.text((40, 290), "Quellensteuer:        CHF  -1,420.00", fill="black")
    d.text((40, 320), "-----------------------------------", fill="black")
    d.text((40, 350), "Nettolohn:            CHF   9,300.25", fill="black")
    d.text((40, 390), "Auszahlung auf: CH45 0070 0110 0009 5678 4", fill="black")
    d.text((40, 420), "Kontakt HR: hr@swisstech.ch / +41 44 888 99 00", fill="black")
    return _to_jpeg(img), "Payslip with AHV + IBAN", ["ahv", "iban", "email", "phone"]


def clean_letter() -> tuple[bytes, str, list[str]]:
    """Regular business letter with no sensitive data."""
    img, d = _draw_doc()
    d.text((40, 30), "RE: Project Update - Q1 2025", fill="black")
    d.text((40, 70), "Dear Mr. Thompson,", fill="black")
    d.text((40, 110), "I am writing to provide an update on the current", fill="black")
    d.text((40, 140), "status of the Alpine Renovation Project.", fill="black")
    d.text((40, 180), "Phase 1 has been completed on schedule. The team", fill="black")
    d.text((40, 210), "delivered all milestones within the agreed budget.", fill="black")
    d.text((40, 250), "We expect Phase 2 to begin in April 2025 with", fill="black")
    d.text((40, 280), "an estimated duration of 8 weeks.", fill="black")
    d.text((40, 320), "Best regards,", fill="black")
    d.text((40, 350), "Andreas Huber, Project Manager", fill="black")
    return _to_jpeg(img), "Clean letter (no sensitive data)", []


def order_confirmation() -> tuple[bytes, str, list[str]]:
    """Online order confirmation with card details."""
    img, d = _draw_doc(800, 450)
    d.text((40, 30), "ORDER CONFIRMATION", fill="black")
    d.text((40, 60), "Digitec Galaxus AG - Order #DG-9947281", fill="black")
    d.text((40, 100), "Customer: Marc Zimmermann", fill="black")
    d.text((40, 130), "Email: marc.zimm@gmail.com", fill="black")
    d.text((40, 170), "1x Sony WH-1000XM5              CHF   349.00", fill="black")
    d.text((40, 200), "1x USB-C Cable 2m                CHF    12.90", fill="black")
    d.text((40, 230), "Shipping:                        CHF     0.00", fill="black")
    d.text((40, 260), "Total:                           CHF   361.90", fill="black")
    d.text((40, 300), "Payment: Mastercard", fill="black")
    d.text((40, 330), "Card: 5412 7534 9012 3456", fill="black")
    d.text((40, 360), "Delivery to: Bahnhofstrasse 15, 8001 Zurich", fill="black")
    d.text((40, 400), "Support: support@digitec.ch / 044 575 95 00", fill="black")
    return _to_jpeg(img), "Order with credit card", ["credit_card", "email", "phone"]


def doctor_referral() -> tuple[bytes, str, list[str]]:
    """Doctor referral letter with patient SSN."""
    img, d = _draw_doc(800, 450)
    d.text((40, 30), "REFERRAL LETTER", fill="black")
    d.text((40, 60), "Dr. Sarah Chen, MD - Internal Medicine", fill="black")
    d.text((40, 100), "Patient: David R. Anderson", fill="black")
    d.text((40, 130), "DOB: 08/22/1976", fill="black")
    d.text((40, 160), "SSN: 447-91-3258", fill="black")
    d.text((40, 200), "Referring to: Dr. Michael Torres, Cardiology", fill="black")
    d.text((40, 240), "Reason: Evaluation of persistent chest pain", fill="black")
    d.text((40, 270), "and abnormal stress test results. Patient has", fill="black")
    d.text((40, 300), "family history of coronary artery disease.", fill="black")
    d.text((40, 340), "Please contact my office at (617) 555-0234", fill="black")
    d.text((40, 370), "or email: dr.chen@mercymed.org", fill="black")
    d.text((40, 410), "Date: March 18, 2025", fill="black")
    return _to_jpeg(img), "Doctor referral with SSN", ["ssn", "dob", "phone", "email"]


# ── Registry ───────────────────────────────────────────────────────

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
