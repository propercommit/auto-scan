"""Document analysis using Claude Vision API."""

from __future__ import annotations

import base64
import io
import json
import sys
from dataclasses import dataclass, field
from datetime import date

import anthropic
from PIL import Image

from auto_scan import AnalysisError
from auto_scan.config import Config

ALL_CATEGORIES = [
    "invoice", "receipt", "contract", "letter", "medical", "tax",
    "insurance", "bank", "government", "personal", "automobile",
    "housing", "education", "employment", "travel", "utilities",
    "manual", "other",
]

ANALYSIS_PROMPT = """\
Analyze this scanned document. Return ONLY valid JSON (no markdown) with these fields:

"category": one of: {categories}
"filename": YYYY-MM-DD_category_who_what_details.pdf — include all key identifiers a human needs to find this file later. Examples:
  - "2022-06-10_contract_bmw_x3_g01_sale_agreement.pdf"
  - "2024-03-15_invoice_vodafone_march_89eur.pdf"
  - "2025-01-10_receipt_amazon_lenovo_t14_laptop.pdf"
  - "2024-11-20_insurance_allianz_car_policy_renewal.pdf"
  - "2023-09-01_tax_2022_annual_return.pdf"
  - "2024-07-22_medical_dr_mueller_blood_test_results.pdf"
  Use document date if visible, else {today}. Lowercase, underscores, no spaces. Include: entity/company name, product/model, amounts, reference numbers — whatever makes this file uniquely identifiable at a glance.
"summary": one sentence
"date": YYYY-MM-DD or null
"key_fields": relevant key-value pairs (vendor, amount, reference number, parties, etc.)
"suggested_categories": 3-5 best matching categories, most relevant first
"risk_level": "none", "low", "medium", or "high"
"risks": array of short strings, each describing one concern found in the document. Look for:
  - Scam indicators (fake logos, urgency pressure, unusual payment methods)
  - Misleading or vague terms and conditions
  - Hidden fees, auto-renewal traps, penalty clauses
  - Unusually one-sided or unfair contract terms
  - Inconsistencies (mismatched dates, names, amounts)
  - Phishing signals (suspicious sender, links, requests for personal info)
  Empty array if no risks found."""


@dataclass
class DocumentInfo:
    category: str
    filename: str
    summary: str
    date: str | None
    key_fields: dict = field(default_factory=dict)
    suggested_categories: list[str] = field(default_factory=list)
    risk_level: str = "none"
    risks: list[str] = field(default_factory=list)


def _resize_for_api(image_data: bytes, max_dim: int = 1568) -> bytes:
    """Resize image if either dimension exceeds max_dim, preserving aspect ratio."""
    img = Image.open(io.BytesIO(image_data))
    w, h = img.size

    if w <= max_dim and h <= max_dim:
        return image_data

    scale = max_dim / max(w, h)
    new_size = (int(w * scale), int(h * scale))
    img = img.resize(new_size, Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def analyze_document(images: list[bytes], config: Config) -> DocumentInfo:
    """Send scanned page images to Claude Vision for classification and naming."""
    print("Analyzing document with AI...", file=sys.stderr)

    client = anthropic.Anthropic(api_key=config.api_key)

    # Build message content: one image block per page + text prompt
    content: list[dict] = []
    for i, img_data in enumerate(images[:20]):  # Claude supports up to 20 images
        resized = _resize_for_api(img_data)
        b64 = base64.standard_b64encode(resized).decode("ascii")
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": b64,
                },
            }
        )

    content.append(
        {
            "type": "text",
            "text": ANALYSIS_PROMPT.format(
                today=date.today().isoformat(),
                categories=", ".join(ALL_CATEGORIES),
            ),
        }
    )

    try:
        message = client.messages.create(
            model=config.claude_model,
            max_tokens=1024,
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.APIError as e:
        raise AnalysisError(f"Claude API error: {e}") from e

    # Parse the JSON response
    response_text = message.content[0].text.strip()

    # Strip markdown code fences if present
    if response_text.startswith("```"):
        lines = response_text.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        response_text = "\n".join(lines)

    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as e:
        raise AnalysisError(
            f"Failed to parse Claude response as JSON: {e}\nResponse: {response_text}"
        ) from e

    doc_info = DocumentInfo(
        category=data.get("category", "other"),
        filename=data.get("filename", f"{date.today().isoformat()}_other_document.pdf"),
        summary=data.get("summary", ""),
        date=data.get("date"),
        key_fields=data.get("key_fields", {}),
        suggested_categories=data.get("suggested_categories", []),
        risk_level=data.get("risk_level", "none"),
        risks=data.get("risks", []),
    )

    print(f"  Category: {doc_info.category}", file=sys.stderr)
    print(f"  Filename: {doc_info.filename}", file=sys.stderr)
    print(f"  Summary:  {doc_info.summary}", file=sys.stderr)
    if doc_info.risks:
        print(f"  Risk:     {doc_info.risk_level} ({len(doc_info.risks)} issue(s))", file=sys.stderr)

    return doc_info
