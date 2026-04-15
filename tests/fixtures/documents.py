"""100 synthetic test documents with known metadata for unit and E2E tests.

Each DocFixture carries:
  - images: JPEG bytes for each page (1-3 pages)
  - Known metadata fields matching what the Claude API would return
  - api_response() → canned API JSON for mocking _classify_images
  - batch_entry() → canned batch-combined JSON entry for mocking _single_pass_batch

For batch tests, BatchFixture groups multiple DocFixtures and provides
grouping_response() and combined_response() matching the engine's expectations.
"""

from __future__ import annotations

import io
import json
import random
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFont


# ── Font loading ──────────────────────────────────────────────────

_FONT_CACHE: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def _get_font(size: int = 28) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    for path in [
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "arial.ttf",
    ]:
        try:
            font = ImageFont.truetype(path, size)
            _FONT_CACHE[size] = font
            return font
        except (OSError, IOError):
            continue
    font = ImageFont.load_default()
    _FONT_CACHE[size] = font
    return font


# ── Image generation helpers ─────────────────────────────────────

# Smaller than test_documents.py (faster tests, less memory)
_W, _H = 800, 1100
_MARGIN = 40
_LINE = 36


def _make_page(
    header: str,
    lines: list[str],
    header_color: tuple[int, int, int] = (0, 0, 128),
    page_label: str | None = None,
) -> bytes:
    """Create a synthetic document page with header bar and text lines."""
    img = Image.new("RGB", (_W, _H), "white")
    draw = ImageDraw.Draw(img)
    hf = _get_font(32)
    bf = _get_font(22)

    # Colored header bar
    draw.rectangle([0, 0, _W, 80], fill=header_color)
    draw.text((_MARGIN, 24), header, fill="white", font=hf)

    # Body text
    y = 100
    for line in lines:
        draw.text((_MARGIN, y), line, fill="black", font=bf)
        y += _LINE

    # Optional page label in footer
    if page_label:
        draw.text((_W // 2 - 40, _H - 40), page_label, fill=(128, 128, 128), font=bf)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


# ── DocFixture dataclass ─────────────────────────────────────────


@dataclass
class DocFixture:
    """A test document with known metadata and synthetic page images."""

    images: list[bytes]
    category: str
    issuer: str
    subject: str
    ref_number: str | None = None
    date: str | None = None
    summary: str = ""
    amount: str | None = None
    recipient: str | None = None
    tags: list[str] = field(default_factory=list)
    risk_level: str = "none"
    risks: list[str] = field(default_factory=list)
    suggested_categories: list[str] = field(default_factory=list)

    @property
    def num_pages(self) -> int:
        return len(self.images)

    def api_response(self) -> dict:
        """Return what Claude would return for _classify_images."""
        key_fields: dict = {}
        if self.amount:
            key_fields["amount"] = self.amount
        if self.recipient:
            key_fields["recipient"] = self.recipient
        key_fields["issuer"] = self.issuer
        key_fields["subject"] = self.subject
        if self.ref_number:
            key_fields["ref_number"] = self.ref_number
        return {
            "category": self.category,
            "issuer": self.issuer,
            "subject": self.subject,
            "ref_number": self.ref_number,
            "summary": self.summary,
            "date": self.date,
            "key_fields": key_fields,
            "suggested_categories": self.suggested_categories or [self.category],
            "tags": self.tags or [self.category, self.issuer.lower().replace(" ", "_")],
            "risk_level": self.risk_level,
            "risks": self.risks,
        }

    def api_response_json(self) -> str:
        """JSON string minus the leading '{' (for assistant prefill mock)."""
        full = json.dumps(self.api_response())
        return full[1:]  # strip leading '{'

    def batch_entry(self, pages: list[int], confidence: int = 95) -> dict:
        """Return a single entry for a batch-combined response."""
        resp = self.api_response()
        resp["pages"] = pages
        resp["confidence"] = confidence
        resp["page_confidence"] = {str(p): confidence for p in pages}
        return resp


# ── BatchFixture ─────────────────────────────────────────────────


@dataclass
class BatchFixture:
    """A batch of documents with interleaved pages for testing grouping."""

    documents: list[DocFixture]
    page_order: list[tuple[int, int]] | None = None  # (doc_idx, page_within_doc)

    def __post_init__(self):
        if self.page_order is None:
            # Default: sequential order (doc0-pages, doc1-pages, ...)
            order = []
            for doc_idx, doc in enumerate(self.documents):
                for page_idx in range(doc.num_pages):
                    order.append((doc_idx, page_idx))
            self.page_order = order

    @property
    def num_pages(self) -> int:
        return len(self.page_order)

    @property
    def all_images(self) -> list[bytes]:
        """All page images in scan order."""
        return [
            self.documents[doc_idx].images[page_idx]
            for doc_idx, page_idx in self.page_order
        ]

    def _pages_for_doc(self, doc_idx: int) -> list[int]:
        """1-indexed page numbers for a document in the scan order."""
        return [
            i + 1
            for i, (d, _) in enumerate(self.page_order)
            if d == doc_idx
        ]

    def grouping_response(self, base_confidence: int = 95) -> list[dict]:
        """What _group_pages would return."""
        groups = []
        for doc_idx, doc in enumerate(self.documents):
            pages = self._pages_for_doc(doc_idx)
            if not pages:
                continue
            pc = {str(p): base_confidence for p in pages}
            groups.append({
                "pages": pages,
                "confidence": base_confidence,
                "page_confidence": pc,
                "reasoning": f"Test grouping for {doc.issuer} {doc.category}",
            })
        return groups

    def grouping_response_json(self) -> str:
        """JSON for grouping response (complete array, no prefill)."""
        data = self.grouping_response()
        full = json.dumps(data)
        return full[1:]  # strip leading '['

    def combined_response(self, base_confidence: int = 95) -> list[dict]:
        """What _single_pass_batch would return (group + classify)."""
        entries = []
        for doc_idx, doc in enumerate(self.documents):
            pages = self._pages_for_doc(doc_idx)
            if not pages:
                continue
            entries.append(doc.batch_entry(pages, base_confidence))
        return entries

    def combined_response_json(self) -> str:
        """JSON for combined response minus leading '['."""
        data = self.combined_response()
        full = json.dumps(data)
        return full[1:]  # strip leading '['


# ── Document generators (one per category) ───────────────────────
# Each returns a DocFixture. Multiple variants per category reach 100.

# Colors for different letterheads (visual boundary detection)
_COLORS = {
    "blue": (0, 51, 153),
    "red": (153, 0, 0),
    "green": (0, 102, 51),
    "purple": (76, 0, 153),
    "orange": (204, 102, 0),
    "teal": (0, 128, 128),
    "gray": (80, 80, 80),
    "navy": (0, 0, 80),
    "maroon": (128, 0, 0),
    "dark_green": (0, 80, 0),
}


def _c(name: str) -> tuple[int, int, int]:
    return _COLORS.get(name, (0, 51, 153))


# ────────────────────────────────────────────────────────────────
# INVOICE (5)
# ────────────────────────────────────────────────────────────────

def _invoice_vodafone() -> DocFixture:
    return DocFixture(
        images=[_make_page("Vodafone", [
            "Invoice #INV-2024-88431",
            "Date: 2024-03-15",
            "Customer: Nate Barbey",
            "Mobile plan: March 2024",
            "Amount: CHF 89.00",
            "Payment due: 2024-04-15",
        ], _c("red"))],
        category="invoice", issuer="vodafone", subject="mobile_bill_march",
        ref_number="INV-2024-88431", date="2024-03-15",
        summary="Vodafone monthly mobile invoice for March 2024, CHF 89.00",
        amount="CHF 89.00", recipient="Nate Barbey",
        tags=["vodafone", "invoice", "mobile", "march", "chf_89"],
        suggested_categories=["utilities", "subscription", "receipt"],
    )


def _invoice_swisscom() -> DocFixture:
    return DocFixture(
        images=[
            _make_page("Swisscom", [
                "Rechnung Nr. SC-2024-44291",
                "Datum: 2024-05-01",
                "Kunde: Hans Mueller",
                "Internet + TV Abo",
                "Betrag: CHF 129.00",
            ], _c("blue"), page_label="Page 1 of 2"),
            _make_page("Swisscom", [
                "Detailaufstellung:",
                "Internet 1Gbit: CHF 79.00",
                "TV Basic: CHF 30.00",
                "Router Miete: CHF 20.00",
            ], _c("blue"), page_label="Page 2 of 2"),
        ],
        category="invoice", issuer="swisscom", subject="internet_tv_may",
        ref_number="SC-2024-44291", date="2024-05-01",
        summary="Swisscom monthly internet and TV invoice for May 2024",
        amount="CHF 129.00", recipient="Hans Mueller",
        tags=["swisscom", "invoice", "internet", "tv", "may"],
        suggested_categories=["utilities", "subscription"],
    )


def _invoice_dentist() -> DocFixture:
    return DocFixture(
        images=[_make_page("Dr. Weber Zahnarztpraxis", [
            "Rechnung RG-2025-0012",
            "Datum: 2025-01-20",
            "Patient: Sophie Dubois",
            "Kontrolle und Zahnreinigung",
            "Total: CHF 280.00",
            "Bitte ueberweisen auf IBAN CH93 0076 2011 6238 5295 7",
        ], _c("teal"))],
        category="invoice", issuer="dr_weber_zahnarzt", subject="dental_checkup_jan",
        ref_number="RG-2025-0012", date="2025-01-20",
        summary="Dental invoice for checkup and cleaning, CHF 280.00",
        amount="CHF 280.00", recipient="Sophie Dubois",
        tags=["dentist", "invoice", "medical", "zahnarzt"],
        suggested_categories=["medical", "receipt"],
    )


def _invoice_amazon() -> DocFixture:
    return DocFixture(
        images=[_make_page("Amazon.de", [
            "Order Invoice",
            "Invoice: DE-2024-9847231",
            "Date: 2024-11-28",
            "Ship to: Marc Zimmermann",
            "Logitech MX Master 3S: EUR 89.99",
            "USB-C Hub 7-port: EUR 34.99",
            "Total: EUR 124.98",
        ], _c("orange"))],
        category="invoice", issuer="amazon", subject="electronics_order_nov",
        ref_number="DE-2024-9847231", date="2024-11-28",
        summary="Amazon invoice for electronics order, EUR 124.98",
        amount="EUR 124.98", recipient="Marc Zimmermann",
        tags=["amazon", "invoice", "electronics", "logitech"],
        suggested_categories=["receipt", "other"],
    )


def _invoice_garage() -> DocFixture:
    return DocFixture(
        images=[_make_page("Garage Morges SA", [
            "Facture F-2025-0331",
            "Date: 2025-03-10",
            "Client: Nate Barbey",
            "Service annuel BMW X3",
            "Main d'oeuvre: CHF 450.00",
            "Pieces: CHF 320.00",
            "Total TTC: CHF 770.00",
        ], _c("gray"))],
        category="invoice", issuer="garage_morges", subject="bmw_annual_service",
        ref_number="F-2025-0331", date="2025-03-10",
        summary="Annual service invoice for BMW X3 from Garage Morges, CHF 770.00",
        amount="CHF 770.00", recipient="Nate Barbey",
        tags=["garage", "invoice", "bmw", "service", "automobile"],
        suggested_categories=["automobile", "receipt"],
    )


# ────────────────────────────────────────────────────────────────
# RECEIPT (4)
# ────────────────────────────────────────────────────────────────

def _receipt_migros() -> DocFixture:
    return DocFixture(
        images=[_make_page("Migros", [
            "Filiale: Zurich HB", "Datum: 14.03.2025",
            "Milch 1L: CHF 1.80", "Brot: CHF 3.50",
            "Kaese: CHF 7.20", "Total: CHF 12.50",
            "Bezahlt: TWINT",
        ], _c("orange"))],
        category="receipt", issuer="migros", subject="groceries_march",
        ref_number=None, date="2025-03-14",
        summary="Migros grocery receipt, CHF 12.50",
        amount="CHF 12.50", tags=["migros", "receipt", "groceries"],
        suggested_categories=["invoice", "personal"],
    )


def _receipt_restaurant() -> DocFixture:
    return DocFixture(
        images=[_make_page("Restaurant Kronenhalle", [
            "Table 12 | 2025-02-14",
            "2x Zuercher Geschnetzeltes: CHF 78.00",
            "1x Dessert: CHF 18.00",
            "Wine: CHF 65.00", "Total: CHF 161.00",
            "Card: VISA ****4242",
        ], _c("maroon"))],
        category="receipt", issuer="kronenhalle", subject="dinner_valentines",
        ref_number=None, date="2025-02-14",
        summary="Restaurant Kronenhalle dinner receipt, CHF 161.00",
        amount="CHF 161.00", tags=["restaurant", "receipt", "dinner"],
        suggested_categories=["invoice", "personal"],
    )


def _receipt_apple() -> DocFixture:
    return DocFixture(
        images=[_make_page("Apple Store Zurich", [
            "Receipt #APL-Z-2025-0088",
            "Date: 2025-01-05",
            "MacBook Air M4 15-inch: CHF 1,599.00",
            "AppleCare+: CHF 299.00",
            "Total: CHF 1,898.00",
            "Payment: Mastercard ****8811",
        ], _c("gray"))],
        category="receipt", issuer="apple", subject="macbook_purchase",
        ref_number="APL-Z-2025-0088", date="2025-01-05",
        summary="Apple Store receipt for MacBook Air M4, CHF 1,898.00",
        amount="CHF 1,898.00",
        tags=["apple", "receipt", "macbook", "electronics"],
        suggested_categories=["invoice", "warranty"],
    )


def _receipt_pharmacy() -> DocFixture:
    return DocFixture(
        images=[_make_page("Apotheke zum Engel", [
            "Kassenbon | 2025-03-20",
            "Dafalgan 1g x20: CHF 8.90",
            "Nasenspray: CHF 12.50",
            "Total: CHF 21.40",
            "Bar bezahlt",
        ], _c("green"))],
        category="receipt", issuer="apotheke_engel", subject="medication_march",
        ref_number=None, date="2025-03-20",
        summary="Pharmacy receipt for medication, CHF 21.40",
        amount="CHF 21.40",
        tags=["pharmacy", "receipt", "medication", "medical"],
        suggested_categories=["medical", "personal"],
    )


# ────────────────────────────────────────────────────────────────
# CONTRACT (4)
# ────────────────────────────────────────────────────────────────

def _contract_bmw() -> DocFixture:
    return DocFixture(
        images=[
            _make_page("BMW Morges", [
                "KAUFVERTRAG / SALES CONTRACT",
                "Vertrag-Nr: KV-2025-1192",
                "Datum: 2025-02-20",
                "Kaeufer: Nate Barbey",
                "Fahrzeug: BMW X3 30e xDrive",
                "VIN: WBA123456789",
            ], _c("blue"), page_label="Page 1 of 3"),
            _make_page("BMW Morges", [
                "Kaufpreis: CHF 72,400.00",
                "Anzahlung: CHF 10,000.00",
                "Finanzierung: CHF 62,400.00",
                "Liefertermin: April 2025",
            ], _c("blue"), page_label="Page 2 of 3"),
            _make_page("BMW Morges", [
                "Allgemeine Geschaeftsbedingungen",
                "Gerichtsstand: Morges VD",
                "Unterschrift Kaeufer: ___________",
                "Unterschrift Verkaeufer: ___________",
            ], _c("blue"), page_label="Page 3 of 3"),
        ],
        category="contract", issuer="bmw_morges", subject="x3_30e_purchase",
        ref_number="KV-2025-1192", date="2025-02-20",
        summary="Purchase contract for BMW X3 30e xDrive from BMW Morges",
        amount="CHF 72,400.00", recipient="Nate Barbey",
        tags=["bmw", "contract", "x3_30e", "purchase", "automobile"],
        suggested_categories=["automobile", "legal", "receipt"],
    )


def _contract_employment() -> DocFixture:
    return DocFixture(
        images=[
            _make_page("Acme Technologies AG", [
                "EMPLOYMENT CONTRACT",
                "Contract ID: EMP-2025-042",
                "Employee: Sophie Dubois",
                "Position: Senior Software Engineer",
                "Start: 01.04.2025",
                "Salary: CHF 142,000 p.a.",
            ], _c("navy"), page_label="1/2"),
            _make_page("Acme Technologies AG", [
                "Benefits: 5 weeks holiday",
                "Pension: BVG standard",
                "Notice period: 3 months",
                "Probation: 3 months",
                "Signed: 15.03.2025",
            ], _c("navy"), page_label="2/2"),
        ],
        category="contract", issuer="acme_technologies", subject="employment_sophie",
        ref_number="EMP-2025-042", date="2025-03-15",
        summary="Employment contract for Senior Software Engineer at Acme Technologies",
        amount="CHF 142,000", recipient="Sophie Dubois",
        tags=["employment", "contract", "acme", "engineer"],
        suggested_categories=["employment", "legal"],
    )


def _contract_rental() -> DocFixture:
    return DocFixture(
        images=[
            _make_page("Immobilien Verwaltung AG", [
                "MIETVERTRAG / RENTAL AGREEMENT",
                "Ref: MV-2024-0889",
                "Mieter: Luca Fontana",
                "Objekt: Langstrasse 88, 8004 Zurich",
                "Mietzins: CHF 2,450.00 / Monat",
                "Nebenkosten: CHF 200.00 / Monat",
            ], _c("dark_green"), page_label="Seite 1 von 2"),
            _make_page("Immobilien Verwaltung AG", [
                "Mietbeginn: 01.01.2024",
                "Kuendigungsfrist: 3 Monate",
                "Kaution: CHF 7,350.00",
                "Parkplatz: Nr. 42 inkl.",
                "Unterschrift: ___________",
            ], _c("dark_green"), page_label="Seite 2 von 2"),
        ],
        category="contract", issuer="immobilien_verwaltung", subject="apartment_rental_langstrasse",
        ref_number="MV-2024-0889", date="2024-01-01",
        summary="Rental agreement for apartment at Langstrasse 88, CHF 2,450/month",
        amount="CHF 2,450.00", recipient="Luca Fontana",
        tags=["rental", "contract", "apartment", "zurich", "housing"],
        suggested_categories=["housing", "legal"],
    )


def _contract_phone() -> DocFixture:
    return DocFixture(
        images=[_make_page("Salt Mobile SA", [
            "Abo-Vertrag",
            "Vertrag: SM-2024-77431",
            "Kunde: Thomas Keller",
            "Abo: Salt Swiss Max",
            "Monatspreis: CHF 49.95",
            "Laufzeit: 24 Monate ab 01.06.2024",
            "Geraet: Samsung Galaxy S24 (CHF 1.00)",
        ], _c("purple"))],
        category="contract", issuer="salt_mobile", subject="phone_subscription",
        ref_number="SM-2024-77431", date="2024-06-01",
        summary="Salt Mobile 24-month phone subscription contract",
        amount="CHF 49.95/month", recipient="Thomas Keller",
        tags=["salt", "contract", "mobile", "subscription"],
        suggested_categories=["subscription", "utilities"],
    )


# ────────────────────────────────────────────────────────────────
# LETTER (4)
# ────────────────────────────────────────────────────────────────

def _letter_business() -> DocFixture:
    return DocFixture(
        images=[_make_page("Schneider Consulting", [
            "RE: Project Update Q1 2025",
            "Zurich, 28. March 2025",
            "Dear Mr. Thompson,",
            "Phase one has been completed on schedule.",
            "The next phase will begin in April.",
            "Best regards, Andreas Huber",
        ], _c("navy"))],
        category="letter", issuer="schneider_consulting", subject="project_update_q1",
        ref_number=None, date="2025-03-28",
        summary="Business letter about Q1 project update from Schneider Consulting",
        tags=["letter", "business", "project", "update"],
        suggested_categories=["other", "personal"],
    )


def _letter_bank_notification() -> DocFixture:
    return DocFixture(
        images=[_make_page("UBS Switzerland AG", [
            "Wichtige Mitteilung",
            "Zurich, 15.02.2025",
            "Sehr geehrter Herr Mueller,",
            "Wir informieren Sie ueber eine Aenderung",
            "unserer Allgemeinen Geschaeftsbedingungen.",
            "Die neuen AGB gelten ab 01.04.2025.",
        ], _c("red"))],
        category="letter", issuer="ubs", subject="agb_changes_notification",
        ref_number=None, date="2025-02-15",
        summary="UBS notification about changes to general terms and conditions",
        tags=["ubs", "letter", "bank", "notification", "agb"],
        suggested_categories=["bank", "legal"],
    )


def _letter_complaint() -> DocFixture:
    return DocFixture(
        images=[_make_page("Private Correspondence", [
            "Zurich, 01.04.2025",
            "Reklamation Bestellung #ORD-88421",
            "Sehr geehrte Damen und Herren,",
            "Die gelieferte Ware weist Maengel auf.",
            "Ich bitte um Ersatzlieferung.",
            "Mit freundlichen Gruessen, Anna Meier",
        ], _c("gray"))],
        category="letter", issuer="anna_meier", subject="product_complaint",
        ref_number="ORD-88421", date="2025-04-01",
        summary="Product complaint letter regarding defective delivery",
        tags=["letter", "complaint", "personal"],
        suggested_categories=["personal", "legal"],
    )


def _letter_termination() -> DocFixture:
    return DocFixture(
        images=[_make_page("Zurich Versicherung", [
            "Kuendigung Police Nr. ZV-2020-44821",
            "Zurich, 30.09.2024",
            "Sehr geehrter Herr Barbey,",
            "Hiermit kuendigen wir die oben genannte",
            "Versicherungspolice per 31.12.2024.",
            "Freundliche Gruesse, Zurich Versicherung",
        ], _c("blue"))],
        category="letter", issuer="zurich_versicherung", subject="policy_termination",
        ref_number="ZV-2020-44821", date="2024-09-30",
        summary="Insurance policy termination notice from Zurich Versicherung",
        tags=["letter", "insurance", "termination", "zurich"],
        suggested_categories=["insurance", "legal"],
    )


# ────────────────────────────────────────────────────────────────
# MEDICAL (4)
# ────────────────────────────────────────────────────────────────

def _medical_prescription() -> DocFixture:
    return DocFixture(
        images=[_make_page("Dr. med. Sarah Chen", [
            "REZEPT / PRESCRIPTION",
            "Patient: David Anderson",
            "Datum: 2025-03-18",
            "Rx: Amoxicillin 500mg 3x taeglich",
            "Dauer: 7 Tage",
            "Dr. Chen, FMH Innere Medizin",
        ], _c("teal"))],
        category="medical", issuer="dr_chen", subject="prescription_amoxicillin",
        ref_number=None, date="2025-03-18",
        summary="Medical prescription for Amoxicillin from Dr. Chen",
        tags=["medical", "prescription", "doctor"],
        suggested_categories=["personal", "other"],
    )


def _medical_lab_results() -> DocFixture:
    return DocFixture(
        images=[
            _make_page("Unilabs Schweiz", [
                "LABORERGEBNISSE / LAB RESULTS",
                "Auftrag: LAB-2025-09821",
                "Patient: Maria Bernasconi",
                "Datum: 2025-02-28",
                "Blutbild: Normal",
                "Cholesterin: 5.2 mmol/L",
            ], _c("green"), page_label="1/2"),
            _make_page("Unilabs Schweiz", [
                "Schilddruese: TSH 2.1 mU/L (normal)",
                "Leber: ALT 28 U/L (normal)",
                "Niere: Kreatinin 78 umol/L",
                "Arzt: Dr. med. P. Rossi",
            ], _c("green"), page_label="2/2"),
        ],
        category="medical", issuer="unilabs", subject="blood_test_results",
        ref_number="LAB-2025-09821", date="2025-02-28",
        summary="Lab results from Unilabs for blood panel, all normal",
        recipient="Maria Bernasconi",
        tags=["medical", "lab", "blood_test", "unilabs"],
        suggested_categories=["personal", "other"],
    )


def _medical_referral() -> DocFixture:
    return DocFixture(
        images=[_make_page("Dr. med. M. Torres", [
            "UEBERWEISUNG / REFERRAL",
            "Ref: REF-2025-0044",
            "Patient: Robert Williams",
            "An: Dr. med. K. Brunner, Kardiologie",
            "Grund: Belastungs-EKG auffaellig",
            "Bitte um weitere Abklaerung",
        ], _c("teal"))],
        category="medical", issuer="dr_torres", subject="cardiology_referral",
        ref_number="REF-2025-0044", date="2025-03-05",
        summary="Medical referral to cardiology for abnormal stress test",
        recipient="Robert Williams",
        tags=["medical", "referral", "cardiology"],
        suggested_categories=["personal", "other"],
    )


def _medical_hospital_bill() -> DocFixture:
    return DocFixture(
        images=[_make_page("Universitaetsspital Zurich", [
            "Rechnung / Invoice",
            "Rechnungsnr: USZ-2025-88412",
            "Patient: Elena Petrova",
            "Behandlung: Ambulante OP 12.02.2025",
            "Total: CHF 3,200.00",
            "Krankenkasse: CSS (Police KK-882741)",
        ], _c("blue"))],
        category="medical", issuer="usz", subject="outpatient_surgery_feb",
        ref_number="USZ-2025-88412", date="2025-02-12",
        summary="Hospital invoice for outpatient surgery at USZ, CHF 3,200.00",
        amount="CHF 3,200.00", recipient="Elena Petrova",
        tags=["medical", "hospital", "invoice", "usz"],
        suggested_categories=["invoice", "insurance"],
    )


# ────────────────────────────────────────────────────────────────
# TAX (4)
# ────────────────────────────────────────────────────────────────

def _tax_declaration() -> DocFixture:
    return DocFixture(
        images=[
            _make_page("Steueramt Kanton Zurich", [
                "STEUERERKLAERUNG 2024",
                "Steuerpflichtiger: Hans Mueller",
                "AHV-Nr: 756.1234.5678.97",
                "Steuerbares Einkommen: CHF 128,450",
                "Steuerbares Vermoegen: CHF 342,100",
            ], _c("navy"), page_label="Seite 1 von 2"),
            _make_page("Steueramt Kanton Zurich", [
                "Abzuege:",
                "Berufsauslagen: CHF 4,000",
                "Versicherungen: CHF 5,200",
                "3. Saeule: CHF 7,056",
                "Total Abzuege: CHF 16,256",
            ], _c("navy"), page_label="Seite 2 von 2"),
        ],
        category="tax", issuer="steueramt_zurich", subject="declaration_2024",
        ref_number=None, date="2024-12-31",
        summary="Swiss tax declaration 2024 for Kanton Zurich",
        recipient="Hans Mueller",
        tags=["tax", "declaration", "zurich", "2024"],
        suggested_categories=["government", "personal"],
    )


def _tax_assessment() -> DocFixture:
    return DocFixture(
        images=[_make_page("Steueramt Winterthur", [
            "STEUERVERANLAGUNG 2023",
            "Veranlagung Nr: STV-2023-44821",
            "Steuerpflichtiger: Thomas Keller",
            "Einkommen: CHF 95,200",
            "Steuer: CHF 12,847.00",
            "Zahlbar bis: 30.06.2024",
        ], _c("navy"))],
        category="tax", issuer="steueramt_winterthur", subject="assessment_2023",
        ref_number="STV-2023-44821", date="2024-03-15",
        summary="Tax assessment for 2023 from Winterthur, CHF 12,847.00 due",
        amount="CHF 12,847.00", recipient="Thomas Keller",
        tags=["tax", "assessment", "winterthur"],
        suggested_categories=["government", "invoice"],
    )


def _tax_receipt_3a() -> DocFixture:
    return DocFixture(
        images=[_make_page("VIAC - Terzo Vorsorge", [
            "Saeule 3a Bescheinigung 2024",
            "Ref: VIAC-3A-2024-8821",
            "Versicherter: Nate Barbey",
            "Einzahlungen 2024: CHF 7,056.00",
            "Guthaben per 31.12.2024: CHF 42,881.00",
            "Fuer Steuerzwecke",
        ], _c("purple"))],
        category="tax", issuer="viac", subject="pillar_3a_certificate_2024",
        ref_number="VIAC-3A-2024-8821", date="2024-12-31",
        summary="Pillar 3a tax certificate from VIAC for 2024, CHF 7,056.00 contributed",
        amount="CHF 7,056.00", recipient="Nate Barbey",
        tags=["tax", "3a", "pension", "viac", "certificate"],
        suggested_categories=["pension", "investment", "certificate"],
    )


def _tax_withholding() -> DocFixture:
    return DocFixture(
        images=[_make_page("Acme Technologies AG", [
            "Lohnausweis / Salary Certificate 2024",
            "Ref: LA-2024-042",
            "Mitarbeiter: Sophie Dubois",
            "Bruttolohn: CHF 142,000",
            "Quellensteuer: CHF 18,460",
            "AHV-Beitraege: CHF 7,525.60",
        ], _c("navy"))],
        category="tax", issuer="acme_technologies", subject="salary_certificate_2024",
        ref_number="LA-2024-042", date="2024-12-31",
        summary="Salary certificate 2024 from Acme Technologies",
        amount="CHF 142,000", recipient="Sophie Dubois",
        tags=["tax", "salary", "lohnausweis", "employment"],
        suggested_categories=["employment", "personal"],
    )


# ────────────────────────────────────────────────────────────────
# INSURANCE (4)
# ────────────────────────────────────────────────────────────────

def _insurance_policy() -> DocFixture:
    return DocFixture(
        images=[
            _make_page("AXA Winterthur", [
                "VERSICHERUNGSPOLICE",
                "Police Nr: AX-4821-2024",
                "Versicherungsnehmer: Nate Barbey",
                "Produkt: Hausratversicherung",
                "Deckung: CHF 100,000",
                "Praemie: CHF 42.00/Monat",
            ], _c("blue"), page_label="1/2"),
            _make_page("AXA Winterthur", [
                "Deckungsumfang:",
                "- Feuer, Wasser, Diebstahl",
                "- Glasbruch",
                "- Haftpflicht inkl.",
                "Gueltig ab: 01.01.2024",
                "Selbstbehalt: CHF 500",
            ], _c("blue"), page_label="2/2"),
        ],
        category="insurance", issuer="axa", subject="household_insurance_policy",
        ref_number="AX-4821-2024", date="2024-01-01",
        summary="AXA household insurance policy, CHF 100,000 coverage",
        amount="CHF 42.00/month", recipient="Nate Barbey",
        tags=["insurance", "axa", "household", "policy"],
        suggested_categories=["housing", "legal"],
    )


def _insurance_claim() -> DocFixture:
    return DocFixture(
        images=[_make_page("CSS Krankenkasse", [
            "LEISTUNGSABRECHNUNG",
            "Abrechnungs-Nr: CSS-2025-44291",
            "Versicherter: Elena Petrova",
            "Police: KK-882741",
            "Behandlung: Dr. Chen, 18.03.2025",
            "Rueckerstattung: CHF 180.00",
        ], _c("green"))],
        category="insurance", issuer="css", subject="health_claim_reimbursement",
        ref_number="CSS-2025-44291", date="2025-03-25",
        summary="CSS health insurance reimbursement for doctor visit, CHF 180.00",
        amount="CHF 180.00", recipient="Elena Petrova",
        tags=["insurance", "css", "health", "claim", "reimbursement"],
        suggested_categories=["medical", "receipt"],
    )


def _insurance_auto() -> DocFixture:
    return DocFixture(
        images=[_make_page("Mobiliar", [
            "MOTORFAHRZEUGVERSICHERUNG",
            "Police: MOB-MFV-2025-0042",
            "Halter: Nate Barbey",
            "Fahrzeug: BMW X3 30e, VD 12345",
            "Vollkasko + Teilkasko",
            "Jahrespraemie: CHF 1,842.00",
        ], _c("red"))],
        category="insurance", issuer="mobiliar", subject="auto_insurance_bmw",
        ref_number="MOB-MFV-2025-0042", date="2025-01-01",
        summary="Mobiliar full coverage auto insurance for BMW X3",
        amount="CHF 1,842.00", recipient="Nate Barbey",
        tags=["insurance", "mobiliar", "auto", "bmw", "vollkasko"],
        suggested_categories=["automobile", "legal"],
    )


def _insurance_renewal() -> DocFixture:
    return DocFixture(
        images=[_make_page("Helvetia Versicherung", [
            "PRAEMIENRECHNUNG 2025",
            "Police: HV-HH-2024-9912",
            "Versicherter: Luca Fontana",
            "Haftpflichtversicherung",
            "Praemie 2025: CHF 198.00",
            "Zahlbar bis: 31.01.2025",
        ], _c("red"))],
        category="insurance", issuer="helvetia", subject="liability_premium_2025",
        ref_number="HV-HH-2024-9912", date="2025-01-01",
        summary="Helvetia liability insurance premium invoice for 2025",
        amount="CHF 198.00", recipient="Luca Fontana",
        tags=["insurance", "helvetia", "liability", "premium"],
        suggested_categories=["invoice", "legal"],
    )


# ────────────────────────────────────────────────────────────────
# BANK (4)
# ────────────────────────────────────────────────────────────────

def _bank_statement() -> DocFixture:
    return DocFixture(
        images=[
            _make_page("UBS Switzerland AG", [
                "KONTOAUSZUG / ACCOUNT STATEMENT",
                "Konto: CH56 0023 0023 1098 7654 3",
                "Periode: Maerz 2025",
                "Anfangssaldo: CHF 15,432.80",
                "Lohneingang: CHF 8,950.00",
            ], _c("red"), page_label="1/2"),
            _make_page("UBS Switzerland AG", [
                "Miete: CHF -2,100.00",
                "Einkauf Migros: CHF -187.35",
                "Swisscom: CHF -129.00",
                "Endsaldo: CHF 20,595.45",
            ], _c("red"), page_label="2/2"),
        ],
        category="bank", issuer="ubs", subject="statement_march_2025",
        ref_number=None, date="2025-03-31",
        summary="UBS bank statement for March 2025",
        amount="CHF 20,595.45", recipient="Maria Bernasconi",
        tags=["bank", "ubs", "statement", "march"],
        suggested_categories=["personal", "other"],
    )


def _bank_credit_card() -> DocFixture:
    return DocFixture(
        images=[_make_page("Viseca Card Services", [
            "KREDITKARTENABRECHNUNG",
            "Karte: VISA ****4242",
            "Abrechnungsnr: VCS-2025-03",
            "Periode: 01.03-31.03.2025",
            "Total Ausgaben: CHF 2,847.30",
            "Zahlbar bis: 20.04.2025",
        ], _c("blue"))],
        category="bank", issuer="viseca", subject="credit_card_march",
        ref_number="VCS-2025-03", date="2025-03-31",
        summary="Viseca credit card statement for March 2025, CHF 2,847.30",
        amount="CHF 2,847.30",
        tags=["bank", "credit_card", "viseca", "march"],
        suggested_categories=["invoice", "personal"],
    )


def _bank_mortgage_conf() -> DocFixture:
    return DocFixture(
        images=[_make_page("Raiffeisen Schweiz", [
            "HYPOTHEKENBESTAETIGUNG",
            "Ref: RAIF-HYP-2024-0088",
            "Kreditnehmer: Thomas Keller",
            "Hypothek: CHF 650,000.00",
            "Zinssatz: 1.85% fest bis 2029",
            "Naechste Zinsperiode: 01.01.2029",
        ], _c("orange"))],
        category="bank", issuer="raiffeisen", subject="mortgage_confirmation",
        ref_number="RAIF-HYP-2024-0088", date="2024-06-15",
        summary="Raiffeisen mortgage confirmation, CHF 650,000 at 1.85% fixed",
        amount="CHF 650,000.00", recipient="Thomas Keller",
        tags=["bank", "mortgage", "raiffeisen", "hypothek"],
        suggested_categories=["housing", "legal"],
    )


def _bank_account_opening() -> DocFixture:
    return DocFixture(
        images=[_make_page("PostFinance", [
            "KONTOEROEFFNUNGSBESTAETIGUNG",
            "Ref: PF-2025-NEW-4421",
            "Kontoinhaber: Sophie Dubois",
            "IBAN: CH94 0900 0000 1234 5678 9",
            "Kontotyp: Privatkonto",
            "Eroeffnet: 01.03.2025",
        ], _c("orange"))],
        category="bank", issuer="postfinance", subject="account_opening",
        ref_number="PF-2025-NEW-4421", date="2025-03-01",
        summary="PostFinance new private account opening confirmation",
        recipient="Sophie Dubois",
        tags=["bank", "postfinance", "account", "new"],
        suggested_categories=["personal", "other"],
    )


# ────────────────────────────────────────────────────────────────
# GOVERNMENT (4)
# ────────────────────────────────────────────────────────────────

def _gov_residence_permit() -> DocFixture:
    return DocFixture(
        images=[_make_page("Amt fuer Migration Kt. Zurich", [
            "AUFENTHALTSBEWILLIGUNG B",
            "Ref: MIG-B-2024-88421",
            "Name: Dubois, Sophie",
            "Nationalitaet: Franzoesisch",
            "Gueltig: 01.04.2025 - 31.03.2030",
            "Arbeitsbewilligung: Ja",
        ], _c("red"))],
        category="government", issuer="amt_migration_zh", subject="residence_permit_b",
        ref_number="MIG-B-2024-88421", date="2025-04-01",
        summary="Swiss B residence permit for Sophie Dubois, valid until 2030",
        recipient="Sophie Dubois",
        tags=["government", "permit", "residence", "migration"],
        suggested_categories=["permit", "personal"],
    )


def _gov_voting() -> DocFixture:
    return DocFixture(
        images=[_make_page("Stadt Zurich - Stimmregister", [
            "STIMMRECHTSAUSWEIS",
            "Abstimmung: 09.06.2025",
            "Stimmberechtigter: Nate Barbey",
            "Gemeinde: Zurich (261)",
            "Stimmlokale offen: 10-12 Uhr",
        ], _c("navy"))],
        category="government", issuer="stadt_zurich", subject="voting_card_june",
        ref_number=None, date="2025-06-09",
        summary="Voting card for June 2025 ballot from Stadt Zurich",
        recipient="Nate Barbey",
        tags=["government", "voting", "zurich"],
        suggested_categories=["personal", "other"],
    )


def _gov_fine() -> DocFixture:
    return DocFixture(
        images=[_make_page("Kantonspolizei Waadt", [
            "ORDNUNGSBUSSE / FINE",
            "Busse Nr: VD-POL-2025-0442",
            "Betroffener: Nate Barbey",
            "Datum: 05.03.2025",
            "Grund: Geschwindigkeitsueberschreitung",
            "Betrag: CHF 250.00",
            "Zahlbar innert 30 Tagen",
        ], _c("navy"))],
        category="government", issuer="kantonspolizei_vd", subject="speeding_fine",
        ref_number="VD-POL-2025-0442", date="2025-03-05",
        summary="Speeding fine from Kantonspolizei Waadt, CHF 250.00",
        amount="CHF 250.00", recipient="Nate Barbey",
        tags=["government", "fine", "police", "speeding"],
        suggested_categories=["legal", "automobile"],
    )


def _gov_birth_registration() -> DocFixture:
    return DocFixture(
        images=[_make_page("Zivilstandsamt Zurich", [
            "GEBURTSURKUNDE / BIRTH CERTIFICATE",
            "Register Nr: ZH-GEB-2020-4421",
            "Name: Barbey, Mia Louise",
            "Geboren: 15.08.2020",
            "Ort: Zurich",
            "Eltern: Nate Barbey, Laura Barbey",
        ], _c("navy"))],
        category="government", issuer="zivilstandsamt_zh", subject="birth_certificate_mia",
        ref_number="ZH-GEB-2020-4421", date="2020-08-15",
        summary="Birth certificate for Mia Louise Barbey from Zurich",
        recipient="Mia Louise Barbey",
        tags=["government", "birth", "certificate", "zurich"],
        suggested_categories=["certificate", "personal"],
    )


# ────────────────────────────────────────────────────────────────
# PERSONAL (3)
# ────────────────────────────────────────────────────────────────

def _personal_passport_copy() -> DocFixture:
    return DocFixture(
        images=[_make_page("Schweizerische Eidgenossenschaft", [
            "REISEPASS / PASSPORT",
            "Name: BARBEY, NATE",
            "Passnummer: X9876543",
            "Geburtsdatum: 12.05.1990",
            "Gueltig bis: 12.05.2033",
        ], _c("red"))],
        category="personal", issuer="swiss_confederation", subject="passport_copy",
        ref_number="X9876543", date="2023-05-12",
        summary="Copy of Swiss passport for Nate Barbey",
        recipient="Nate Barbey",
        tags=["personal", "passport", "identity"],
        suggested_categories=["government", "certificate"],
    )


def _personal_cv() -> DocFixture:
    return DocFixture(
        images=[
            _make_page("Curriculum Vitae", [
                "Sophie Dubois",
                "Senior Software Engineer",
                "Zurich, Switzerland",
                "Experience: 8 years",
                "Languages: FR, EN, DE",
            ], _c("gray"), page_label="1/2"),
            _make_page("Curriculum Vitae", [
                "Education:",
                "EPFL - MSc Computer Science 2017",
                "Skills: Python, Go, Kubernetes",
                "References available on request",
            ], _c("gray"), page_label="2/2"),
        ],
        category="personal", issuer="sophie_dubois", subject="cv_resume",
        ref_number=None, date=None,
        summary="Curriculum vitae of Sophie Dubois, Senior Software Engineer",
        tags=["personal", "cv", "resume", "employment"],
        suggested_categories=["employment", "other"],
    )


def _personal_family_booklet() -> DocFixture:
    return DocFixture(
        images=[_make_page("Zivilstandsamt", [
            "FAMILIENBUCH / FAMILY BOOKLET",
            "Familie: Barbey",
            "Eheschliessung: 20.06.2018, Lausanne",
            "Ehemann: Nate Barbey",
            "Ehefrau: Laura Barbey geb. Martin",
            "Kinder: Mia Louise (2020), Leo (2022)",
        ], _c("navy"))],
        category="personal", issuer="zivilstandsamt", subject="family_booklet",
        ref_number=None, date="2018-06-20",
        summary="Family booklet (Familienbuch) for the Barbey family",
        tags=["personal", "family", "civil_status"],
        suggested_categories=["government", "certificate"],
    )


# ────────────────────────────────────────────────────────────────
# AUTOMOBILE (4)
# ────────────────────────────────────────────────────────────────

def _auto_registration() -> DocFixture:
    return DocFixture(
        images=[_make_page("Strassenverkehrsamt VD", [
            "FAHRZEUGAUSWEIS / VEHICLE REGISTRATION",
            "Kontrollschild: VD 12345",
            "Halter: Nate Barbey",
            "Fahrzeug: BMW X3 30e xDrive",
            "VIN: WBA123456789",
            "Erstinverkehrsetzung: 15.04.2025",
        ], _c("navy"))],
        category="automobile", issuer="stva_vd", subject="vehicle_registration_bmw",
        ref_number="VD 12345", date="2025-04-15",
        summary="Vehicle registration for BMW X3 30e, plate VD 12345",
        recipient="Nate Barbey",
        tags=["automobile", "registration", "bmw", "vaud"],
        suggested_categories=["government", "registration"],
    )


def _auto_service_report() -> DocFixture:
    return DocFixture(
        images=[_make_page("Garage Morges SA", [
            "SERVICEBERICHT / SERVICE REPORT",
            "Auftrag: SVC-2025-0088",
            "Fahrzeug: BMW X3 30e (VD 12345)",
            "Km-Stand: 12,450",
            "Oelwechsel: erledigt",
            "Bremsen: OK",
            "Naechster Service: 25,000 km",
        ], _c("gray"))],
        category="automobile", issuer="garage_morges", subject="service_report_12k",
        ref_number="SVC-2025-0088", date="2025-03-10",
        summary="Service report for BMW X3 30e at 12,450 km",
        recipient="Nate Barbey",
        tags=["automobile", "service", "bmw", "garage"],
        suggested_categories=["invoice", "warranty"],
    )


def _auto_vignette() -> DocFixture:
    return DocFixture(
        images=[_make_page("ASTRA / OFROU", [
            "E-VIGNETTE 2025",
            "Bestell-Nr: EVIG-2025-88421",
            "Kontrollschild: VD 12345",
            "Gueltig: 01.12.2024 - 31.01.2026",
            "Preis: CHF 40.00",
        ], _c("red"))],
        category="automobile", issuer="astra", subject="evignette_2025",
        ref_number="EVIG-2025-88421", date="2024-12-01",
        summary="Swiss motorway e-vignette 2025 for VD 12345",
        amount="CHF 40.00",
        tags=["automobile", "vignette", "motorway"],
        suggested_categories=["receipt", "government"],
    )


def _auto_parking_fine() -> DocFixture:
    return DocFixture(
        images=[_make_page("Parking Morges SA", [
            "PARKBUSSE / PARKING FINE",
            "Nr: PM-2025-0221",
            "Kontrollschild: VD 12345",
            "Datum: 22.02.2025, 14:30",
            "Grund: Parkzeit ueberschritten",
            "Busse: CHF 40.00",
        ], _c("orange"))],
        category="automobile", issuer="parking_morges", subject="parking_fine_feb",
        ref_number="PM-2025-0221", date="2025-02-22",
        summary="Parking fine for exceeding time limit, CHF 40.00",
        amount="CHF 40.00",
        tags=["automobile", "parking", "fine"],
        suggested_categories=["government", "receipt"],
    )


# ────────────────────────────────────────────────────────────────
# HOUSING (3)
# ────────────────────────────────────────────────────────────────

def _housing_nebenkosten() -> DocFixture:
    return DocFixture(
        images=[
            _make_page("Immobilien Verwaltung AG", [
                "NEBENKOSTENABRECHNUNG 2024",
                "Ref: NK-2024-LG88",
                "Mieter: Luca Fontana",
                "Objekt: Langstrasse 88",
                "Heizung: CHF 1,200.00",
                "Wasser: CHF 480.00",
            ], _c("dark_green"), page_label="1/2"),
            _make_page("Immobilien Verwaltung AG", [
                "Hauswart: CHF 600.00",
                "Total: CHF 2,280.00",
                "Akonto bezahlt: CHF 2,400.00",
                "Guthaben: CHF 120.00",
            ], _c("dark_green"), page_label="2/2"),
        ],
        category="housing", issuer="immobilien_verwaltung", subject="utility_costs_2024",
        ref_number="NK-2024-LG88", date="2025-03-15",
        summary="Annual utility cost settlement 2024, CHF 120.00 credit",
        amount="CHF 120.00 credit", recipient="Luca Fontana",
        tags=["housing", "nebenkosten", "utilities", "annual"],
        suggested_categories=["invoice", "utilities"],
    )


def _housing_renovation_quote() -> DocFixture:
    return DocFixture(
        images=[_make_page("Schreinerei Huber", [
            "OFFERTE / QUOTE",
            "Offerte Nr: OFF-2025-0033",
            "Kunde: Nate Barbey",
            "Projekt: Einbauschrank Schlafzimmer",
            "Material: Eiche massiv",
            "Preis: CHF 4,800.00 inkl. Montage",
            "Gueltig bis: 30.04.2025",
        ], _c("orange"))],
        category="housing", issuer="schreinerei_huber", subject="wardrobe_quote",
        ref_number="OFF-2025-0033", date="2025-03-20",
        summary="Quote for built-in wardrobe from Schreinerei Huber, CHF 4,800.00",
        amount="CHF 4,800.00", recipient="Nate Barbey",
        tags=["housing", "renovation", "quote", "furniture"],
        suggested_categories=["invoice", "other"],
    )


def _housing_building_insurance() -> DocFixture:
    return DocFixture(
        images=[_make_page("GVZ Gebaeudeversicherung", [
            "GEBAEUDEVERSICHERUNGSPOLICE",
            "Police Nr: GVZ-2024-88412",
            "Eigentuemer: Thomas Keller",
            "Objekt: Technikumstrasse 9, Winterthur",
            "Versicherungswert: CHF 950,000",
            "Praemie: CHF 420.00/Jahr",
        ], _c("blue"))],
        category="housing", issuer="gvz", subject="building_insurance",
        ref_number="GVZ-2024-88412", date="2024-01-01",
        summary="Building insurance policy from GVZ, CHF 950,000 coverage",
        amount="CHF 420.00/year", recipient="Thomas Keller",
        tags=["housing", "insurance", "building", "gvz"],
        suggested_categories=["insurance", "legal"],
    )


# ────────────────────────────────────────────────────────────────
# EDUCATION (3)
# ────────────────────────────────────────────────────────────────

def _education_transcript() -> DocFixture:
    return DocFixture(
        images=[_make_page("EPFL", [
            "TRANSCRIPT OF RECORDS",
            "Student: Sophie Dubois",
            "Program: MSc Computer Science",
            "Semester: HS 2016",
            "Distributed Systems: 5.5",
            "Machine Learning: 6.0",
            "GPA: 5.75",
        ], _c("red"))],
        category="education", issuer="epfl", subject="transcript_hs2016",
        ref_number=None, date="2017-01-15",
        summary="EPFL transcript for MSc Computer Science, HS 2016",
        recipient="Sophie Dubois",
        tags=["education", "epfl", "transcript", "university"],
        suggested_categories=["certificate", "personal"],
    )


def _education_diploma() -> DocFixture:
    return DocFixture(
        images=[_make_page("ETH Zurich", [
            "DIPLOMA",
            "This certifies that",
            "Thomas Keller",
            "has completed the degree of",
            "Bachelor of Science in Mechanical Engineering",
            "Awarded: 15 July 2015",
        ], _c("navy"))],
        category="education", issuer="eth_zurich", subject="bsc_diploma",
        ref_number=None, date="2015-07-15",
        summary="ETH Zurich BSc diploma in Mechanical Engineering",
        recipient="Thomas Keller",
        tags=["education", "eth", "diploma", "engineering"],
        suggested_categories=["certificate", "personal"],
    )


def _education_course_invoice() -> DocFixture:
    return DocFixture(
        images=[_make_page("Migros Klubschule", [
            "KURSBESTAETIGUNG / ENROLLMENT",
            "Kurs-Nr: MKS-2025-B2-DE",
            "Teilnehmer: Elena Petrova",
            "Kurs: Deutsch B2 Intensiv",
            "Dauer: 01.04 - 30.06.2025",
            "Kursgebuehr: CHF 1,200.00",
        ], _c("orange"))],
        category="education", issuer="migros_klubschule", subject="german_b2_course",
        ref_number="MKS-2025-B2-DE", date="2025-03-15",
        summary="Enrollment confirmation for German B2 course at Migros Klubschule",
        amount="CHF 1,200.00", recipient="Elena Petrova",
        tags=["education", "course", "german", "language"],
        suggested_categories=["invoice", "receipt"],
    )


# ────────────────────────────────────────────────────────────────
# EMPLOYMENT (3) — beyond the employment contract above
# ────────────────────────────────────────────────────────────────

def _employment_payslip() -> DocFixture:
    return DocFixture(
        images=[_make_page("SwissTech Solutions AG", [
            "LOHNABRECHNUNG MAERZ 2025",
            "Personal-Nr: EMP-00842",
            "Mitarbeiter: Elena Petrova",
            "Bruttolohn: CHF 11,833.33",
            "AHV/IV/EO: CHF -625.58",
            "Nettolohn: CHF 9,300.25",
        ], _c("purple"))],
        category="employment", issuer="swisstech", subject="payslip_march_2025",
        ref_number="EMP-00842", date="2025-03-31",
        summary="Monthly payslip March 2025 from SwissTech Solutions",
        amount="CHF 9,300.25", recipient="Elena Petrova",
        tags=["employment", "payslip", "salary", "march"],
        suggested_categories=["personal", "tax"],
    )


def _employment_reference() -> DocFixture:
    return DocFixture(
        images=[_make_page("Acme Technologies AG", [
            "ARBEITSZEUGNIS / REFERENCE",
            "Ref: AZ-2025-042",
            "Mitarbeiterin: Sophie Dubois",
            "Position: Senior Software Engineer",
            "Anstellung: 01.04.2022 - 31.03.2025",
            "Wir koennen Frau Dubois bestens empfehlen.",
        ], _c("navy"))],
        category="employment", issuer="acme_technologies", subject="work_reference",
        ref_number="AZ-2025-042", date="2025-03-31",
        summary="Work reference letter for Sophie Dubois from Acme Technologies",
        recipient="Sophie Dubois",
        tags=["employment", "reference", "zeugnis"],
        suggested_categories=["letter", "personal"],
    )


def _employment_termination() -> DocFixture:
    return DocFixture(
        images=[_make_page("GlobalCorp SA", [
            "KUENDIGUNG / TERMINATION",
            "Ref: KUE-2025-0088",
            "Mitarbeiter: Marc Zimmermann",
            "Per: 30.06.2025 (3 Monate Frist)",
            "Letzer Arbeitstag: 30.06.2025",
            "Ferientage-Guthaben: 8 Tage",
        ], _c("red"))],
        category="employment", issuer="globalcorp", subject="termination_notice",
        ref_number="KUE-2025-0088", date="2025-03-31",
        summary="Employment termination notice from GlobalCorp, effective June 2025",
        recipient="Marc Zimmermann",
        tags=["employment", "termination", "kuendigung"],
        suggested_categories=["letter", "legal"],
    )


# ────────────────────────────────────────────────────────────────
# TRAVEL (3)
# ────────────────────────────────────────────────────────────────

def _travel_booking() -> DocFixture:
    return DocFixture(
        images=[_make_page("Swiss International Air Lines", [
            "E-TICKET / BOOKING CONFIRMATION",
            "Booking Ref: LX-ABCD12",
            "Passenger: Nate Barbey",
            "ZRH -> BCN  LX1942  15.07.2025  08:30",
            "BCN -> ZRH  LX1943  22.07.2025  18:45",
            "Total: CHF 389.00",
        ], _c("red"))],
        category="travel", issuer="swiss_airlines", subject="flight_zurich_barcelona",
        ref_number="LX-ABCD12", date="2025-04-01",
        summary="Swiss Air flight booking Zurich-Barcelona round trip, July 2025",
        amount="CHF 389.00", recipient="Nate Barbey",
        tags=["travel", "flight", "swiss", "barcelona"],
        suggested_categories=["receipt", "booking"],
    )


def _travel_hotel() -> DocFixture:
    return DocFixture(
        images=[_make_page("Hotel Arts Barcelona", [
            "RESERVATION CONFIRMATION",
            "Conf. Nr: HAB-2025-88421",
            "Guest: Nate Barbey",
            "Check-in: 15.07.2025",
            "Check-out: 22.07.2025 (7 nights)",
            "Room: Superior Sea View",
            "Total: EUR 1,890.00",
        ], _c("purple"))],
        category="travel", issuer="hotel_arts_bcn", subject="barcelona_hotel_july",
        ref_number="HAB-2025-88421", date="2025-04-02",
        summary="Hotel Arts Barcelona reservation for 7 nights, EUR 1,890.00",
        amount="EUR 1,890.00", recipient="Nate Barbey",
        tags=["travel", "hotel", "barcelona", "reservation"],
        suggested_categories=["receipt", "booking"],
    )


def _travel_insurance() -> DocFixture:
    return DocFixture(
        images=[_make_page("Allianz Travel", [
            "REISEVERSICHERUNG / TRAVEL INSURANCE",
            "Police: AT-2025-TRV-0042",
            "Versicherter: Nate Barbey",
            "Reise: 15.07 - 22.07.2025, Spanien",
            "Deckung: Annullierung + Gepaeck + Heilungskosten",
            "Praemie: CHF 68.00",
        ], _c("blue"))],
        category="travel", issuer="allianz_travel", subject="trip_insurance_spain",
        ref_number="AT-2025-TRV-0042", date="2025-04-01",
        summary="Allianz travel insurance for Spain trip July 2025",
        amount="CHF 68.00", recipient="Nate Barbey",
        tags=["travel", "insurance", "spain", "allianz"],
        suggested_categories=["insurance", "receipt"],
    )


# ────────────────────────────────────────────────────────────────
# UTILITIES (4)
# ────────────────────────────────────────────────────────────────

def _utilities_electricity() -> DocFixture:
    return DocFixture(
        images=[_make_page("EWZ Elektrizitaetswerk Zurich", [
            "STROMRECHNUNG / ELECTRICITY BILL",
            "Kundennr: EWZ-2847391",
            "Kunde: Thomas Keller",
            "Periode: Jan-Maerz 2025",
            "Verbrauch: 342 kWh",
            "Betrag: CHF 87.45",
        ], _c("green"))],
        category="utilities", issuer="ewz", subject="electricity_q1_2025",
        ref_number="EWZ-2847391", date="2025-03-31",
        summary="EWZ electricity bill for Q1 2025, CHF 87.45",
        amount="CHF 87.45", recipient="Thomas Keller",
        tags=["utilities", "electricity", "ewz"],
        suggested_categories=["invoice", "housing"],
    )


def _utilities_water() -> DocFixture:
    return DocFixture(
        images=[_make_page("Wasserversorgung Zurich", [
            "WASSERRECHNUNG 2024",
            "Kunden-Nr: WVZ-44821",
            "Verbraucher: Luca Fontana",
            "Verbrauch: 85 m3",
            "Betrag: CHF 212.50",
            "Zahlbar bis: 30.04.2025",
        ], _c("blue"))],
        category="utilities", issuer="wasserversorgung_zh", subject="water_bill_2024",
        ref_number="WVZ-44821", date="2025-03-15",
        summary="Annual water bill for 2024, CHF 212.50",
        amount="CHF 212.50", recipient="Luca Fontana",
        tags=["utilities", "water", "annual"],
        suggested_categories=["invoice", "housing"],
    )


def _utilities_internet() -> DocFixture:
    return DocFixture(
        images=[_make_page("Init7 (Schweiz) AG", [
            "RECHNUNG FIBER7",
            "Rechnungsnr: I7-2025-03-4421",
            "Kunde: Nate Barbey",
            "Fiber7 25Gbit/s",
            "Maerz 2025: CHF 64.00",
            "IBAN: CH12 0483 5012 3456 7100 0",
        ], _c("orange"))],
        category="utilities", issuer="init7", subject="fiber_internet_march",
        ref_number="I7-2025-03-4421", date="2025-03-01",
        summary="Init7 Fiber7 internet bill for March 2025, CHF 64.00",
        amount="CHF 64.00", recipient="Nate Barbey",
        tags=["utilities", "internet", "fiber", "init7"],
        suggested_categories=["invoice", "subscription"],
    )


def _utilities_serafe() -> DocFixture:
    return DocFixture(
        images=[_make_page("Serafe AG", [
            "RECHNUNG RADIO/TV-ABGABE",
            "Ref: SER-2025-Q1-88421",
            "Abgabepflichtiger: Hans Mueller",
            "Periode: Jan-Maerz 2025",
            "Betrag: CHF 91.40",
            "ESR-Referenz: 00 00000 00000 00000 00088 42109",
        ], _c("teal"))],
        category="utilities", issuer="serafe", subject="radio_tv_fee_q1",
        ref_number="SER-2025-Q1-88421", date="2025-01-01",
        summary="Serafe radio/TV fee for Q1 2025, CHF 91.40",
        amount="CHF 91.40", recipient="Hans Mueller",
        tags=["utilities", "serafe", "radio", "tv", "fee"],
        suggested_categories=["invoice", "government"],
    )


# ────────────────────────────────────────────────────────────────
# LEGAL (3)
# ────────────────────────────────────────────────────────────────

def _legal_power_of_attorney() -> DocFixture:
    return DocFixture(
        images=[_make_page("Notariat Zurich", [
            "VOLLMACHT / POWER OF ATTORNEY",
            "Urkunde Nr: NOT-2025-0221",
            "Vollmachtgeber: Nate Barbey",
            "Bevollmaechtigter: Laura Barbey",
            "Umfang: Generalvollmacht",
            "Datum: 10.01.2025",
        ], _c("navy"))],
        category="legal", issuer="notariat_zurich", subject="general_power_of_attorney",
        ref_number="NOT-2025-0221", date="2025-01-10",
        summary="General power of attorney from Notariat Zurich",
        recipient="Nate Barbey",
        tags=["legal", "power_of_attorney", "notary"],
        suggested_categories=["personal", "government"],
    )


def _legal_court_summons() -> DocFixture:
    return DocFixture(
        images=[_make_page("Bezirksgericht Zurich", [
            "VORLADUNG / COURT SUMMONS",
            "Geschaefts-Nr: BG-ZH-2025-4421",
            "Betreff: Mietstreitigkeit",
            "Beklagter: Immobilien Verwaltung AG",
            "Termin: 15.05.2025, 10:00 Uhr",
            "Saal 3, Badenerstrasse 90",
        ], _c("navy"))],
        category="legal", issuer="bezirksgericht_zh", subject="rental_dispute_hearing",
        ref_number="BG-ZH-2025-4421", date="2025-04-01",
        summary="Court summons for rental dispute at Bezirksgericht Zurich",
        tags=["legal", "court", "summons", "rental"],
        suggested_categories=["government", "housing"],
    )


def _legal_notarized_copy() -> DocFixture:
    return DocFixture(
        images=[_make_page("Notariat Morges", [
            "BEGLAUBIGTE KOPIE / CERTIFIED COPY",
            "Ref: NM-2025-0099",
            "Dokument: Kaufvertrag KV-2025-1192",
            "Beglaubigt am: 01.03.2025",
            "Notar: Me. Jean-Pierre Rochat",
            "Gebuehr: CHF 80.00",
        ], _c("navy"))],
        category="legal", issuer="notariat_morges", subject="certified_contract_copy",
        ref_number="NM-2025-0099", date="2025-03-01",
        summary="Notarized copy of purchase contract from Notariat Morges",
        amount="CHF 80.00",
        tags=["legal", "notary", "certified_copy"],
        suggested_categories=["contract", "other"],
    )


# ────────────────────────────────────────────────────────────────
# WARRANTY (3)
# ────────────────────────────────────────────────────────────────

def _warranty_apple() -> DocFixture:
    return DocFixture(
        images=[_make_page("Apple Inc.", [
            "APPLECARE+ CERTIFICATE",
            "Agreement: AC-2025-CH-88421",
            "Product: MacBook Air M4 15-inch",
            "Serial: FXJY2CH/A",
            "Coverage: 05.01.2025 - 05.01.2028",
            "Includes accidental damage (2 incidents)",
        ], _c("gray"))],
        category="warranty", issuer="apple", subject="applecare_macbook",
        ref_number="AC-2025-CH-88421", date="2025-01-05",
        summary="AppleCare+ warranty certificate for MacBook Air M4, 3 years",
        recipient="Nate Barbey",
        tags=["warranty", "apple", "applecare", "macbook"],
        suggested_categories=["receipt", "certificate"],
    )


def _warranty_bosch() -> DocFixture:
    return DocFixture(
        images=[_make_page("Bosch Hausgeraete", [
            "GARANTIESCHEIN",
            "Produkt: Bosch Serie 8 Waschmaschine",
            "Modell: WAX32M41CH",
            "Seriennr: BSH-2024-88412",
            "Kaufdatum: 15.11.2024",
            "Garantie: 2 Jahre (bis 15.11.2026)",
        ], _c("blue"))],
        category="warranty", issuer="bosch", subject="washing_machine_warranty",
        ref_number="BSH-2024-88412", date="2024-11-15",
        summary="Bosch washing machine 2-year warranty certificate",
        tags=["warranty", "bosch", "appliance", "washing_machine"],
        suggested_categories=["receipt", "manual"],
    )


def _warranty_bmw() -> DocFixture:
    return DocFixture(
        images=[_make_page("BMW AG", [
            "HERSTELLERGARANTIE / MANUFACTURER WARRANTY",
            "Fahrzeug: BMW X3 30e xDrive",
            "VIN: WBA123456789",
            "Garantie: 3 Jahre / 100,000 km",
            "Batterie: 8 Jahre / 160,000 km",
            "Beginn: 15.04.2025",
        ], _c("blue"))],
        category="warranty", issuer="bmw", subject="x3_manufacturer_warranty",
        ref_number="WBA123456789", date="2025-04-15",
        summary="BMW manufacturer warranty for X3 30e, 3 years / 100,000 km",
        tags=["warranty", "bmw", "automobile", "battery"],
        suggested_categories=["automobile", "certificate"],
    )


# ────────────────────────────────────────────────────────────────
# SUBSCRIPTION (3)
# ────────────────────────────────────────────────────────────────

def _subscription_spotify() -> DocFixture:
    return DocFixture(
        images=[_make_page("Spotify AB", [
            "SUBSCRIPTION RECEIPT",
            "Invoice: SP-2025-03-BARBEY",
            "Plan: Premium Family",
            "Period: March 2025",
            "Amount: CHF 19.99",
            "Next billing: 01.04.2025",
        ], _c("green"))],
        category="subscription", issuer="spotify", subject="premium_family_march",
        ref_number="SP-2025-03-BARBEY", date="2025-03-01",
        summary="Spotify Premium Family subscription for March 2025",
        amount="CHF 19.99",
        tags=["subscription", "spotify", "music", "streaming"],
        suggested_categories=["receipt", "invoice"],
    )


def _subscription_nzz() -> DocFixture:
    return DocFixture(
        images=[_make_page("NZZ Mediengruppe", [
            "ABO-RECHNUNG / SUBSCRIPTION INVOICE",
            "Abo-Nr: NZZ-2025-44821",
            "Abonnent: Hans Mueller",
            "Produkt: NZZ Digital Complete",
            "Periode: 01.01-31.12.2025",
            "Betrag: CHF 539.00",
        ], _c("navy"))],
        category="subscription", issuer="nzz", subject="digital_subscription_2025",
        ref_number="NZZ-2025-44821", date="2025-01-01",
        summary="NZZ digital subscription for 2025, CHF 539.00",
        amount="CHF 539.00", recipient="Hans Mueller",
        tags=["subscription", "nzz", "newspaper", "digital"],
        suggested_categories=["invoice", "receipt"],
    )


def _subscription_github() -> DocFixture:
    return DocFixture(
        images=[_make_page("GitHub Inc.", [
            "INVOICE",
            "Invoice: GH-2025-03-PRO",
            "Customer: Nate Barbey",
            "Plan: GitHub Pro",
            "Period: March 2025",
            "Amount: USD 4.00",
        ], _c("gray"))],
        category="subscription", issuer="github", subject="pro_plan_march",
        ref_number="GH-2025-03-PRO", date="2025-03-01",
        summary="GitHub Pro plan subscription for March 2025, USD 4.00",
        amount="USD 4.00", recipient="Nate Barbey",
        tags=["subscription", "github", "software", "developer"],
        suggested_categories=["invoice", "receipt"],
    )


# ────────────────────────────────────────────────────────────────
# DONATION (3)
# ────────────────────────────────────────────────────────────────

def _donation_red_cross() -> DocFixture:
    return DocFixture(
        images=[_make_page("Schweizerisches Rotes Kreuz", [
            "SPENDENQUITTUNG / DONATION RECEIPT",
            "Quittung Nr: SRK-2024-88421",
            "Spender: Nate Barbey",
            "Betrag: CHF 200.00",
            "Datum: 20.12.2024",
            "Steuerlich absetzbar",
        ], _c("red"))],
        category="donation", issuer="rotes_kreuz", subject="annual_donation_2024",
        ref_number="SRK-2024-88421", date="2024-12-20",
        summary="Red Cross donation receipt for CHF 200.00, tax deductible",
        amount="CHF 200.00", recipient="Nate Barbey",
        tags=["donation", "red_cross", "charity", "tax_deductible"],
        suggested_categories=["receipt", "tax"],
    )


def _donation_wwf() -> DocFixture:
    return DocFixture(
        images=[_make_page("WWF Schweiz", [
            "MITGLIEDSCHAFT & SPENDE 2025",
            "Mitgl.-Nr: WWF-CH-44821",
            "Mitglied: Laura Barbey",
            "Jahresbeitrag: CHF 84.00",
            "Zusatzspende: CHF 50.00",
            "Total: CHF 134.00",
        ], _c("dark_green"))],
        category="donation", issuer="wwf", subject="membership_donation_2025",
        ref_number="WWF-CH-44821", date="2025-01-15",
        summary="WWF Switzerland membership and donation for 2025, CHF 134.00",
        amount="CHF 134.00", recipient="Laura Barbey",
        tags=["donation", "wwf", "membership", "environment"],
        suggested_categories=["membership", "receipt"],
    )


def _donation_church() -> DocFixture:
    return DocFixture(
        images=[_make_page("Ref. Kirchgemeinde Zurich", [
            "SPENDENBESCHEINIGUNG 2024",
            "Spender: Familia Mueller",
            "Gesamtspenden 2024: CHF 500.00",
            "Davon Kirchensteuer: CHF 0.00",
            "Fuer Ihre Steuererklaerung",
        ], _c("purple"))],
        category="donation", issuer="kirchgemeinde_zh", subject="church_donations_2024",
        ref_number=None, date="2025-01-31",
        summary="Church donation certificate for 2024, CHF 500.00 total",
        amount="CHF 500.00",
        tags=["donation", "church", "certificate", "tax"],
        suggested_categories=["receipt", "tax", "certificate"],
    )


# ────────────────────────────────────────────────────────────────
# INVESTMENT (3)
# ────────────────────────────────────────────────────────────────

def _investment_portfolio() -> DocFixture:
    return DocFixture(
        images=[
            _make_page("Swissquote Bank", [
                "PORTFOLIO STATEMENT Q1 2025",
                "Depot-Nr: SQ-44821-CHF",
                "Inhaber: Nate Barbey",
                "Aktien: CHF 45,200.00",
                "ETFs: CHF 82,400.00",
                "Cash: CHF 5,100.00",
            ], _c("red"), page_label="1/2"),
            _make_page("Swissquote Bank", [
                "Total Depotwert: CHF 132,700.00",
                "Performance YTD: +4.2%",
                "Gebuehren Q1: CHF 89.00",
                "Naechster Auszug: 30.06.2025",
            ], _c("red"), page_label="2/2"),
        ],
        category="investment", issuer="swissquote", subject="portfolio_q1_2025",
        ref_number="SQ-44821-CHF", date="2025-03-31",
        summary="Swissquote portfolio statement Q1 2025, CHF 132,700.00 total",
        amount="CHF 132,700.00", recipient="Nate Barbey",
        tags=["investment", "swissquote", "portfolio", "stocks", "etf"],
        suggested_categories=["bank", "personal"],
    )


def _investment_trade_confirm() -> DocFixture:
    return DocFixture(
        images=[_make_page("Interactive Brokers", [
            "TRADE CONFIRMATION",
            "Ref: IB-2025-03-88421",
            "Account: U-44821",
            "BUY: 50x VWRL.SW @ CHF 108.20",
            "Total: CHF 5,410.00",
            "Commission: CHF 1.50",
            "Settlement: 2025-03-20",
        ], _c("navy"))],
        category="investment", issuer="interactive_brokers", subject="etf_purchase_vwrl",
        ref_number="IB-2025-03-88421", date="2025-03-18",
        summary="Trade confirmation: 50 VWRL ETF shares purchased at CHF 108.20",
        amount="CHF 5,410.00", recipient="Nate Barbey",
        tags=["investment", "trade", "etf", "vwrl"],
        suggested_categories=["bank", "receipt"],
    )


def _investment_dividend() -> DocFixture:
    return DocFixture(
        images=[_make_page("Nestle SA", [
            "DIVIDENDENABRECHNUNG",
            "Ref: NESN-DIV-2025",
            "Aktionaer: Nate Barbey",
            "Aktien: 100x NESN",
            "Dividende: CHF 3.00 / Aktie",
            "Brutto: CHF 300.00",
            "Verrechnungssteuer 35%: CHF -105.00",
            "Netto: CHF 195.00",
        ], _c("gray"))],
        category="investment", issuer="nestle", subject="dividend_2025",
        ref_number="NESN-DIV-2025", date="2025-04-10",
        summary="Nestle dividend payment for 100 shares, CHF 195.00 net",
        amount="CHF 195.00", recipient="Nate Barbey",
        tags=["investment", "dividend", "nestle", "tax"],
        suggested_categories=["bank", "tax"],
    )


# ────────────────────────────────────────────────────────────────
# PENSION (3)
# ────────────────────────────────────────────────────────────────

def _pension_bvg_statement() -> DocFixture:
    return DocFixture(
        images=[_make_page("Vita Sammelstiftung", [
            "BVG-VORSORGEAUSWEIS 2024",
            "Versicherte: Sophie Dubois",
            "Vers.-Nr: VITA-2024-88412",
            "Altersguthaben: CHF 124,500.00",
            "Arbeitgeberbeitraege: CHF 8,400.00",
            "Arbeitnehmerbeitraege: CHF 6,300.00",
        ], _c("blue"))],
        category="pension", issuer="vita", subject="bvg_statement_2024",
        ref_number="VITA-2024-88412", date="2024-12-31",
        summary="BVG pension statement 2024 from Vita, CHF 124,500 balance",
        amount="CHF 124,500.00", recipient="Sophie Dubois",
        tags=["pension", "bvg", "vita", "retirement"],
        suggested_categories=["tax", "personal"],
    )


def _pension_ahv_statement() -> DocFixture:
    return DocFixture(
        images=[_make_page("AHV/IV Ausgleichskasse", [
            "INDIVIDUELLES KONTO - AUSZUG",
            "AHV-Nr: 756.1234.5678.97",
            "Name: Mueller, Hans Peter",
            "Beitraege 2024: CHF 12,450.00",
            "Kumuliert seit 1998: CHF 248,900.00",
        ], _c("navy"))],
        category="pension", issuer="ahv_ausgleichskasse", subject="contribution_statement",
        ref_number="756.1234.5678.97", date="2025-02-15",
        summary="AHV individual account statement showing cumulative contributions",
        recipient="Hans Mueller",
        tags=["pension", "ahv", "social_security", "contributions"],
        suggested_categories=["government", "tax"],
    )


def _pension_vested_benefits() -> DocFixture:
    return DocFixture(
        images=[_make_page("Freizuegigkeitsstiftung", [
            "FREIZUEGIGKEITSKONTO AUSZUG",
            "Konto: FZK-2025-44821",
            "Inhaber: Marc Zimmermann",
            "Guthaben per 31.12.2024: CHF 67,200.00",
            "Zins 2024: 1.25%",
            "Bezug fruehestens: 2055",
        ], _c("teal"))],
        category="pension", issuer="freizuegigkeitsstiftung", subject="vested_benefits_2024",
        ref_number="FZK-2025-44821", date="2024-12-31",
        summary="Vested benefits account statement, CHF 67,200 balance",
        amount="CHF 67,200.00", recipient="Marc Zimmermann",
        tags=["pension", "vested_benefits", "freizuegigkeit"],
        suggested_categories=["bank", "tax"],
    )


# ────────────────────────────────────────────────────────────────
# CERTIFICATE (3)
# ────────────────────────────────────────────────────────────────

def _certificate_marriage() -> DocFixture:
    return DocFixture(
        images=[_make_page("Zivilstandsamt Lausanne", [
            "HEIRATSURKUNDE / MARRIAGE CERTIFICATE",
            "Register Nr: LS-EHE-2018-0442",
            "Ehemann: Nate Barbey",
            "Ehefrau: Laura Martin",
            "Eheschliessung: 20. Juni 2018",
            "Ort: Lausanne VD",
        ], _c("navy"))],
        category="certificate", issuer="zivilstandsamt_lausanne", subject="marriage_certificate",
        ref_number="LS-EHE-2018-0442", date="2018-06-20",
        summary="Marriage certificate for Nate Barbey and Laura Martin, June 2018",
        tags=["certificate", "marriage", "civil_status"],
        suggested_categories=["personal", "government"],
    )


def _certificate_first_aid() -> DocFixture:
    return DocFixture(
        images=[_make_page("Samariter Schweiz", [
            "KURSBESTAETIGUNG / COURSE CERTIFICATE",
            "Kurs: Nothelfer (First Aid)",
            "Teilnehmer: Leo Barbey",
            "Datum: 15.02.2025",
            "Kursleiter: P. Rossi, dipl. Rettungssanitaeter",
            "Gueltig fuer Fuehrerschein-Antrag",
        ], _c("red"))],
        category="certificate", issuer="samariter", subject="first_aid_course",
        ref_number=None, date="2025-02-15",
        summary="First aid course certificate for driver's license application",
        recipient="Leo Barbey",
        tags=["certificate", "first_aid", "course", "driving"],
        suggested_categories=["education", "automobile"],
    )


def _certificate_language() -> DocFixture:
    return DocFixture(
        images=[_make_page("Goethe-Institut", [
            "ZERTIFIKAT DEUTSCH B2",
            "Zertifikat-Nr: GI-B2-2025-4421",
            "Kandidat: Elena Petrova",
            "Pruefungsdatum: 28.03.2025",
            "Ergebnis: Bestanden (82/100)",
            "CEFR Level: B2",
        ], _c("green"))],
        category="certificate", issuer="goethe_institut", subject="german_b2_certificate",
        ref_number="GI-B2-2025-4421", date="2025-03-28",
        summary="Goethe-Institut German B2 language certificate, passed with 82/100",
        recipient="Elena Petrova",
        tags=["certificate", "german", "language", "b2"],
        suggested_categories=["education", "personal"],
    )


# ────────────────────────────────────────────────────────────────
# PERMIT (3)
# ────────────────────────────────────────────────────────────────

def _permit_building() -> DocFixture:
    return DocFixture(
        images=[_make_page("Bauamt Stadt Zurich", [
            "BAUBEWILLIGUNG / BUILDING PERMIT",
            "Bewilligungs-Nr: BA-ZH-2025-0088",
            "Bauherr: Thomas Keller",
            "Objekt: Technikumstrasse 9, 8400 Winterthur",
            "Projekt: Terrassenanbau",
            "Bewilligt am: 01.03.2025",
        ], _c("navy"))],
        category="permit", issuer="bauamt_zurich", subject="terrace_building_permit",
        ref_number="BA-ZH-2025-0088", date="2025-03-01",
        summary="Building permit for terrace extension at Technikumstrasse 9",
        recipient="Thomas Keller",
        tags=["permit", "building", "construction", "zurich"],
        suggested_categories=["government", "housing"],
    )


def _permit_work() -> DocFixture:
    return DocFixture(
        images=[_make_page("SECO", [
            "ARBEITSBEWILLIGUNG / WORK PERMIT",
            "Ref: SECO-ARB-2025-0042",
            "Name: Petrova, Elena",
            "Nationalitaet: Russisch",
            "Arbeitgeber: SwissTech Solutions AG",
            "Gueltig: 01.01.2025 - 31.12.2025",
        ], _c("red"))],
        category="permit", issuer="seco", subject="work_permit_2025",
        ref_number="SECO-ARB-2025-0042", date="2025-01-01",
        summary="Work permit for Elena Petrova at SwissTech Solutions, 2025",
        recipient="Elena Petrova",
        tags=["permit", "work", "seco", "employment"],
        suggested_categories=["government", "employment"],
    )


def _permit_parking() -> DocFixture:
    return DocFixture(
        images=[_make_page("Stadt Zurich - Tiefbauamt", [
            "PARKKARTE / PARKING PERMIT",
            "Nr: PK-ZH-2025-4421",
            "Fahrzeug: VD 12345",
            "Zone: Kreis 4 (Blaue Zone)",
            "Gueltig: 01.01.2025 - 31.12.2025",
            "Gebuehr: CHF 300.00",
        ], _c("blue"))],
        category="permit", issuer="stadt_zurich_tiefbau", subject="parking_permit_kreis4",
        ref_number="PK-ZH-2025-4421", date="2025-01-01",
        summary="City parking permit for Kreis 4, CHF 300.00",
        amount="CHF 300.00",
        tags=["permit", "parking", "zurich"],
        suggested_categories=["automobile", "government"],
    )


# ────────────────────────────────────────────────────────────────
# REGISTRATION (3)
# ────────────────────────────────────────────────────────────────

def _registration_company() -> DocFixture:
    return DocFixture(
        images=[_make_page("Handelsregisteramt Kt. Zurich", [
            "HANDELSREGISTERAUSZUG",
            "Firmennr: CHE-123.456.789",
            "Firma: Barbey Consulting GmbH",
            "Sitz: Zurich",
            "Gruendung: 01.09.2023",
            "Geschaeftsfuehrer: Nate Barbey",
        ], _c("navy"))],
        category="registration", issuer="handelsregisteramt_zh", subject="company_registration",
        ref_number="CHE-123.456.789", date="2023-09-01",
        summary="Commercial register excerpt for Barbey Consulting GmbH",
        recipient="Nate Barbey",
        tags=["registration", "company", "handelsregister"],
        suggested_categories=["government", "legal"],
    )


def _registration_dog() -> DocFixture:
    return DocFixture(
        images=[_make_page("Stadt Zurich - Hundekontrolle", [
            "HUNDEHALTER-REGISTRIERUNG",
            "Reg.-Nr: HK-ZH-2024-0088",
            "Halter: Laura Barbey",
            "Hund: Luna (Labrador Retriever)",
            "Chip-Nr: 756098100012345",
            "Steuer: CHF 210.00/Jahr",
        ], _c("green"))],
        category="registration", issuer="hundekontrolle_zh", subject="dog_registration",
        ref_number="HK-ZH-2024-0088", date="2024-03-01",
        summary="Dog registration for Luna (Labrador) in Zurich",
        amount="CHF 210.00/year", recipient="Laura Barbey",
        tags=["registration", "dog", "pet", "zurich"],
        suggested_categories=["government", "personal"],
    )


def _registration_vehicle_import() -> DocFixture:
    return DocFixture(
        images=[_make_page("EZV / BAZG", [
            "EINFUHRDEKLARATION / IMPORT DECLARATION",
            "Ref: EZV-2025-IMP-0042",
            "Importeur: Nate Barbey",
            "Gegenstand: Fahrrad (E-Bike)",
            "Herkunft: Deutschland",
            "Zoll: CHF 0.00 (EU Freihandel)",
            "MWST: CHF 58.00",
        ], _c("red"))],
        category="registration", issuer="bazg", subject="ebike_import_declaration",
        ref_number="EZV-2025-IMP-0042", date="2025-02-10",
        summary="Customs import declaration for e-bike from Germany",
        amount="CHF 58.00", recipient="Nate Barbey",
        tags=["registration", "import", "customs", "ebike"],
        suggested_categories=["government", "receipt"],
    )


# ────────────────────────────────────────────────────────────────
# MEMBERSHIP (3)
# ────────────────────────────────────────────────────────────────

def _membership_gym() -> DocFixture:
    return DocFixture(
        images=[_make_page("Fitness Park Zurich", [
            "MITGLIEDSCHAFTSBESTAETIGUNG",
            "Mitgl.-Nr: FPZ-2025-4421",
            "Mitglied: Nate Barbey",
            "Abo: Premium (12 Monate)",
            "Gueltig: 01.01-31.12.2025",
            "Monatsbeitrag: CHF 99.00",
        ], _c("orange"))],
        category="membership", issuer="fitness_park", subject="gym_premium_2025",
        ref_number="FPZ-2025-4421", date="2025-01-01",
        summary="Fitness Park premium gym membership for 2025",
        amount="CHF 99.00/month", recipient="Nate Barbey",
        tags=["membership", "gym", "fitness"],
        suggested_categories=["subscription", "receipt"],
    )


def _membership_tcs() -> DocFixture:
    return DocFixture(
        images=[_make_page("TCS Touring Club Schweiz", [
            "MITGLIEDSCHAFTSAUSWEIS 2025",
            "Mitgl.-Nr: TCS-88421",
            "Mitglied: Nate Barbey",
            "Kategorie: Individual Plus",
            "Pannenhilfe inkl.",
            "Jahresbeitrag: CHF 152.00",
        ], _c("orange"))],
        category="membership", issuer="tcs", subject="touring_club_2025",
        ref_number="TCS-88421", date="2025-01-01",
        summary="TCS touring club membership 2025 with roadside assistance",
        amount="CHF 152.00", recipient="Nate Barbey",
        tags=["membership", "tcs", "automobile", "touring"],
        suggested_categories=["automobile", "subscription"],
    )


def _membership_professional() -> DocFixture:
    return DocFixture(
        images=[_make_page("Swiss Engineering STV", [
            "MITGLIEDSCHAFTSBESTAETIGUNG 2025",
            "Mitgl.-Nr: STV-2025-0442",
            "Mitglied: Thomas Keller",
            "Fachgruppe: Maschinenbau",
            "Jahresbeitrag: CHF 280.00",
            "Inkl. Fachzeitschrift",
        ], _c("navy"))],
        category="membership", issuer="swiss_engineering", subject="professional_membership_2025",
        ref_number="STV-2025-0442", date="2025-01-01",
        summary="Swiss Engineering professional association membership 2025",
        amount="CHF 280.00", recipient="Thomas Keller",
        tags=["membership", "engineering", "professional"],
        suggested_categories=["subscription", "education"],
    )


# ────────────────────────────────────────────────────────────────
# MANUAL (2)
# ────────────────────────────────────────────────────────────────

def _manual_bosch_washer() -> DocFixture:
    return DocFixture(
        images=[
            _make_page("Bosch Hausgeraete", [
                "BEDIENUNGSANLEITUNG",
                "Serie 8 Waschmaschine WAX32M41CH",
                "Kapitel 1: Sicherheitshinweise",
                "Vor der ersten Benutzung lesen",
                "Max. Beladung: 9 kg",
            ], _c("blue"), page_label="Seite 1 von 3"),
            _make_page("Bosch Hausgeraete", [
                "Kapitel 2: Programmuebersicht",
                "Baumwolle 60: fuer stark verschmutzte Waesche",
                "Pflegeleicht 40: fuer Mischgewebe",
                "Wolle/Seide: Handwaescheprogramm",
            ], _c("blue"), page_label="Seite 2 von 3"),
            _make_page("Bosch Hausgeraete", [
                "Kapitel 3: Fehlerbehebung",
                "E18: Abpumpen nicht moeglich",
                "E21: Motor blockiert",
                "Service: 0848 840 040",
            ], _c("blue"), page_label="Seite 3 von 3"),
        ],
        category="manual", issuer="bosch", subject="washing_machine_manual",
        ref_number="WAX32M41CH", date=None,
        summary="Operating manual for Bosch Serie 8 washing machine",
        tags=["manual", "bosch", "washing_machine", "appliance"],
        suggested_categories=["warranty", "other"],
    )


def _manual_router_quickstart() -> DocFixture:
    return DocFixture(
        images=[_make_page("Swisscom", [
            "INTERNET-BOX QUICK START",
            "1. Anschliessen: DSL-Kabel in TAE-Dose",
            "2. Strom: Netzteil einstecken",
            "3. Warten: bis LED gruen leuchtet (ca. 5 Min)",
            "4. WLAN: Netzwerkname auf Rueckseite",
            "Support: 0800 800 800",
        ], _c("blue"))],
        category="manual", issuer="swisscom", subject="router_quickstart",
        ref_number=None, date=None,
        summary="Swisscom Internet-Box quick start guide",
        tags=["manual", "swisscom", "router", "internet"],
        suggested_categories=["utilities", "other"],
    )


# ────────────────────────────────────────────────────────────────
# OTHER (2)
# ────────────────────────────────────────────────────────────────

def _other_handwritten_note() -> DocFixture:
    return DocFixture(
        images=[_make_page("Handwritten Note", [
            "Shopping list:",
            "- Milk, Bread, Cheese",
            "- Call dentist Monday",
            "- Pick up kids 16:00",
            "- Laura birthday gift!",
        ], _c("gray"))],
        category="other", issuer="personal_note", subject="shopping_list_and_reminders",
        ref_number=None, date=None,
        summary="Handwritten personal note with shopping list and reminders",
        tags=["personal", "note", "shopping"],
        suggested_categories=["personal"],
    )


def _other_flyer() -> DocFixture:
    return DocFixture(
        images=[_make_page("Quartierverein Zurich 4", [
            "QUARTIERFEST 2025",
            "Samstag, 21. Juni 2025",
            "Helvetiaplatz, 14-22 Uhr",
            "Live Musik, Food Trucks",
            "Kinderprogramm",
            "Eintritt frei!",
        ], _c("purple"))],
        category="other", issuer="quartierverein_zh4", subject="neighborhood_festival",
        ref_number=None, date="2025-06-21",
        summary="Flyer for neighborhood festival in Zurich Kreis 4",
        tags=["other", "flyer", "event", "zurich"],
        suggested_categories=["personal"],
    )


# ────────────────────────────────────────────────────────────────
# RISK DOCUMENTS (for risk detection testing)
# ────────────────────────────────────────────────────────────────

def _other_menu() -> DocFixture:
    return DocFixture(
        images=[_make_page("Restaurant Zeughauskeller", [
            "MITTAGSMENU / LUNCH MENU",
            "Montag, 14. April 2025",
            "Suppe: Kuerbiscremesuppe",
            "Hauptgang: Zuercher Geschnetzeltes",
            "mit Roesti und Gemuese",
            "Dessert: Schokoladenmousse",
            "Preis: CHF 28.50 inkl. Getraenk",
        ], _c("maroon"))],
        category="other", issuer="zeughauskeller", subject="lunch_menu_monday",
        ref_number=None, date="2025-04-14",
        summary="Monday lunch menu from Restaurant Zeughauskeller, CHF 28.50",
        amount="CHF 28.50",
        tags=["other", "menu", "restaurant", "lunch"],
        suggested_categories=["personal", "receipt"],
    )


def _other_meeting_minutes() -> DocFixture:
    return DocFixture(
        images=[_make_page("Barbey Consulting GmbH", [
            "SITZUNGSPROTOKOLL / MEETING MINUTES",
            "Datum: 10.04.2025, 14:00-15:30",
            "Teilnehmer: N. Barbey, S. Dubois, T. Keller",
            "Thema: Q2 Roadmap Planning",
            "Beschluesse: Launch Phase 2 by June",
            "Naechstes Meeting: 24.04.2025",
        ], _c("gray"))],
        category="other", issuer="barbey_consulting", subject="meeting_minutes_q2",
        ref_number=None, date="2025-04-10",
        summary="Meeting minutes for Q2 roadmap planning at Barbey Consulting",
        tags=["other", "meeting", "minutes", "business"],
        suggested_categories=["letter", "personal"],
    )


def _invoice_plumber() -> DocFixture:
    return DocFixture(
        images=[_make_page("Sanitaer Brunner AG", [
            "RECHNUNG / INVOICE",
            "Rechnungs-Nr: SB-2025-0147",
            "Datum: 2025-03-25",
            "Kunde: Luca Fontana",
            "Reparatur Wasserhahn Kueche",
            "Arbeit 1.5h: CHF 195.00",
            "Material: CHF 85.00",
            "Total: CHF 280.00",
        ], _c("teal"))],
        category="invoice", issuer="sanitaer_brunner", subject="kitchen_faucet_repair",
        ref_number="SB-2025-0147", date="2025-03-25",
        summary="Plumber invoice for kitchen faucet repair, CHF 280.00",
        amount="CHF 280.00", recipient="Luca Fontana",
        tags=["invoice", "plumber", "repair", "housing"],
        suggested_categories=["housing", "receipt"],
    )


def _receipt_parking() -> DocFixture:
    return DocFixture(
        images=[_make_page("Parkhaus Urania Zurich", [
            "PARKGEBUEHR / PARKING FEE",
            "Datum: 08.04.2025",
            "Einfahrt: 09:15  Ausfahrt: 12:30",
            "Dauer: 3h 15min",
            "Betrag: CHF 18.00",
            "Bezahlt: Kreditkarte",
        ], _c("navy"))],
        category="receipt", issuer="parkhaus_urania", subject="parking_fee_april",
        ref_number=None, date="2025-04-08",
        summary="Parking receipt from Parkhaus Urania, CHF 18.00",
        amount="CHF 18.00",
        tags=["receipt", "parking", "zurich"],
        suggested_categories=["automobile", "personal"],
    )


def _risk_phishing_invoice() -> DocFixture:
    return DocFixture(
        images=[_make_page("Amaz0n Support", [
            "URGENT: Your account will be closed!",
            "Invoice: AMZ-URGENT-999",
            "Please pay immediately: USD 499.99",
            "Send payment via Bitcoin to:",
            "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            "You have 24 hours to respond!",
        ], _c("red"))],
        category="other", issuer="amaz0n_support", subject="phishing_attempt",
        ref_number="AMZ-URGENT-999", date="2025-04-01",
        summary="Suspicious phishing email impersonating Amazon with Bitcoin payment",
        amount="USD 499.99",
        risk_level="high",
        risks=["phishing_language", "bitcoin_payment", "fake_urgency", "misspelled_brand"],
        tags=["scam", "phishing", "bitcoin", "urgent"],
        suggested_categories=["invoice"],
    )


def _risk_hidden_fees_contract() -> DocFixture:
    return DocFixture(
        images=[
            _make_page("SuperDeal Telecom", [
                "MOBILE CONTRACT - SPECIAL OFFER",
                "Plan: Unlimited Everything",
                "Monthly: CHF 19.99 (first 3 months)",
                "Free Samsung Galaxy included!",
                "Contract duration: 24 months",
            ], _c("green"), page_label="1/2"),
            _make_page("SuperDeal Telecom", [
                "Terms (continued):",
                "After promotional period: CHF 79.99/month",
                "Early termination: CHF 500.00",
                "Device return: original packaging required",
                "Auto-renewal: 12 months",
            ], _c("green"), page_label="2/2"),
        ],
        category="contract", issuer="superdeal_telecom", subject="mobile_contract_hidden_fees",
        ref_number="SDT-2025-PROMO",
        date="2025-04-01",
        summary="Mobile contract with hidden fee escalation after promo period",
        amount="CHF 19.99 (then CHF 79.99)",
        risk_level="medium",
        risks=["price_escalation", "high_early_termination_fee", "auto_renewal_trap"],
        tags=["contract", "mobile", "hidden_fees", "promo"],
        suggested_categories=["subscription", "utilities"],
    )


# ────────────────────────────────────────────────────────────────
# REGISTRY — all 100 fixtures
# ────────────────────────────────────────────────────────────────

_ALL_GENERATORS = [
    # Invoice (5)
    _invoice_vodafone, _invoice_swisscom, _invoice_dentist, _invoice_amazon, _invoice_garage,
    # Receipt (4)
    _receipt_migros, _receipt_restaurant, _receipt_apple, _receipt_pharmacy,
    # Contract (4)
    _contract_bmw, _contract_employment, _contract_rental, _contract_phone,
    # Letter (4)
    _letter_business, _letter_bank_notification, _letter_complaint, _letter_termination,
    # Medical (4)
    _medical_prescription, _medical_lab_results, _medical_referral, _medical_hospital_bill,
    # Tax (4)
    _tax_declaration, _tax_assessment, _tax_receipt_3a, _tax_withholding,
    # Insurance (4)
    _insurance_policy, _insurance_claim, _insurance_auto, _insurance_renewal,
    # Bank (4)
    _bank_statement, _bank_credit_card, _bank_mortgage_conf, _bank_account_opening,
    # Government (4)
    _gov_residence_permit, _gov_voting, _gov_fine, _gov_birth_registration,
    # Personal (3)
    _personal_passport_copy, _personal_cv, _personal_family_booklet,
    # Automobile (4)
    _auto_registration, _auto_service_report, _auto_vignette, _auto_parking_fine,
    # Housing (3)
    _housing_nebenkosten, _housing_renovation_quote, _housing_building_insurance,
    # Education (3)
    _education_transcript, _education_diploma, _education_course_invoice,
    # Employment (3)
    _employment_payslip, _employment_reference, _employment_termination,
    # Travel (3)
    _travel_booking, _travel_hotel, _travel_insurance,
    # Utilities (4)
    _utilities_electricity, _utilities_water, _utilities_internet, _utilities_serafe,
    # Legal (3)
    _legal_power_of_attorney, _legal_court_summons, _legal_notarized_copy,
    # Warranty (3)
    _warranty_apple, _warranty_bosch, _warranty_bmw,
    # Subscription (3)
    _subscription_spotify, _subscription_nzz, _subscription_github,
    # Donation (3)
    _donation_red_cross, _donation_wwf, _donation_church,
    # Investment (3)
    _investment_portfolio, _investment_trade_confirm, _investment_dividend,
    # Pension (3)
    _pension_bvg_statement, _pension_ahv_statement, _pension_vested_benefits,
    # Certificate (3)
    _certificate_marriage, _certificate_first_aid, _certificate_language,
    # Permit (3)
    _permit_building, _permit_work, _permit_parking,
    # Registration (3)
    _registration_company, _registration_dog, _registration_vehicle_import,
    # Membership (3)
    _membership_gym, _membership_tcs, _membership_professional,
    # Manual (2)
    _manual_bosch_washer, _manual_router_quickstart,
    # Other (4)
    _other_handwritten_note, _other_flyer, _other_menu, _other_meeting_minutes,
    # Extra invoice + receipt (2)
    _invoice_plumber, _receipt_parking,
    # Risk (2)
    _risk_phishing_invoice, _risk_hidden_fees_contract,
]

# Lazy-initialized cache
_ALL_FIXTURES: list[DocFixture] | None = None


def ALL_FIXTURES() -> list[DocFixture]:
    """Return all 100 document fixtures (cached after first call)."""
    global _ALL_FIXTURES
    if _ALL_FIXTURES is None:
        _ALL_FIXTURES = [gen() for gen in _ALL_GENERATORS]
    return _ALL_FIXTURES


def fixtures_by_category(category: str) -> list[DocFixture]:
    """Return all fixtures matching a category."""
    return [f for f in ALL_FIXTURES() if f.category == category]


def make_batch(
    docs: list[DocFixture] | None = None,
    n: int = 5,
    interleave: bool = False,
) -> BatchFixture:
    """Create a BatchFixture from given docs or random selection.

    Args:
        docs: Specific fixtures to use. If None, picks n random ones.
        n: Number of documents if docs is None.
        interleave: If True, shuffle pages from different documents.
    """
    if docs is None:
        all_f = ALL_FIXTURES()
        docs = random.sample(all_f, min(n, len(all_f)))

    if interleave and len(docs) > 1:
        # Build interleaved page order
        pages: list[tuple[int, int]] = []
        for doc_idx, doc in enumerate(docs):
            for page_idx in range(doc.num_pages):
                pages.append((doc_idx, page_idx))
        random.shuffle(pages)
        return BatchFixture(documents=docs, page_order=pages)

    return BatchFixture(documents=docs)
