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

ANALYSIS_PROMPT = """\
You are a document classification and naming assistant. Analyze this scanned document and return a JSON response with:

1. "category": one of: invoice, receipt, contract, letter, medical, tax, insurance, bank, government, personal, manual, other
2. "filename": a descriptive filename using the pattern: YYYY-MM-DD_category_description.pdf
   - Use the document date if visible, otherwise use today's date ({today})
   - Keep the description short (2-5 words, lowercase, underscores)
   - Examples: "2024-03-15_invoice_acme_corp.pdf", "2025-01-10_receipt_amazon.pdf"
3. "summary": one-sentence description of the document
4. "date": the document date in YYYY-MM-DD format, or null if not found
5. "key_fields": object with extracted key-value pairs relevant to the category
   (e.g., for invoice: {{"vendor": "...", "amount": "...", "invoice_number": "..."}})

Return ONLY valid JSON, no markdown formatting or code blocks."""


@dataclass
class DocumentInfo:
    category: str
    filename: str
    summary: str
    date: str | None
    key_fields: dict = field(default_factory=dict)


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
            "text": ANALYSIS_PROMPT.format(today=date.today().isoformat()),
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
    )

    print(f"  Category: {doc_info.category}", file=sys.stderr)
    print(f"  Filename: {doc_info.filename}", file=sys.stderr)
    print(f"  Summary:  {doc_info.summary}", file=sys.stderr)

    return doc_info
