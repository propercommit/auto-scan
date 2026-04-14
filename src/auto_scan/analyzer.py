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
Analyze this scanned document. Return ONLY valid JSON (no markdown):

"category": one of: {categories}
"filename": YYYY-MM-DD_category_who_what_details.pdf — include entity names, product/model, amounts, ref numbers. Examples: "2022-06-10_contract_bmw_x3_g01_sale.pdf", "2024-03-15_invoice_vodafone_march_89eur.pdf". Use doc date if visible, else {today}. Lowercase, underscores.
"summary": one sentence
"date": YYYY-MM-DD or null
"key_fields": relevant extracted key-value pairs (vendor, amount, parties, ref numbers, etc.)
"suggested_categories": 3-5 best matching categories from the list above
"tags": 5-15 lowercase keywords from the content — type, companies, people, products, models, topics. E.g. BMW X3 sale: ["contract","sales","bmw","x3","g01","automobile","purchase"]
"risk_level": "none"|"low"|"medium"|"high"
"risks": [] if clean, else short strings flagging: scams, misleading terms, hidden fees, unfair clauses, inconsistencies, phishing signals"""


@dataclass
class DocumentInfo:
    category: str
    filename: str
    summary: str
    date: str | None
    key_fields: dict = field(default_factory=dict)
    suggested_categories: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
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

    client = anthropic.Anthropic(api_key=config.api_key, timeout=120.0)

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
    except anthropic.APITimeoutError as e:
        raise AnalysisError("Claude API timed out. Try again or use fewer pages.") from e
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
        tags=data.get("tags", []),
        risk_level=data.get("risk_level", "none"),
        risks=data.get("risks", []),
    )

    print(f"  Category: {doc_info.category}", file=sys.stderr)
    print(f"  Filename: {doc_info.filename}", file=sys.stderr)
    print(f"  Summary:  {doc_info.summary}", file=sys.stderr)
    if doc_info.tags:
        print(f"  Tags:     {', '.join(doc_info.tags)}", file=sys.stderr)
    if doc_info.risks:
        print(f"  Risk:     {doc_info.risk_level} ({len(doc_info.risks)} issue(s))", file=sys.stderr)

    return doc_info


# ── Batch analysis ──────────────────────────────────────────────────

BATCH_ANALYSIS_PROMPT = """\
You are analyzing {num_pages} scanned pages that may come from MULTIPLE different documents, possibly interleaved/mixed together.

STEP 1 — GROUP PAGES BY DOCUMENT. This is the most critical step. Two pages belong to the SAME document ONLY if they share ALL of:
- Same physical paper style, weight, color
- Same font family, font size, text styling (bold, italic patterns)
- Same page layout, margins, column structure
- Same letterhead, logo, header/footer design
- Same sender/author/organization
- Consistent page numbering sequence (page 1/3, 2/3, 3/3)
- Same visual "look and feel" overall

Pages that discuss related topics but look visually different (different fonts, different letterheads, different layouts) are SEPARATE documents. A bank statement and a bank letter are two documents even though both are from the same bank — unless they share identical formatting. Prioritize visual/structural similarity over topical similarity.

STEP 2 — Classify each group.
Return ONLY valid JSON (no markdown) — an array:

[{{"pages": [1, 2], "category": "...", "filename": "YYYY-MM-DD_cat_who_what.pdf", "summary": "...", "date": "YYYY-MM-DD or null", "key_fields": {{}}, "suggested_categories": [], "tags": [], "risk_level": "none"|"low"|"medium"|"high", "risks": []}}]

Categories: {categories}
Pages numbered 1–{num_pages}. Every page in exactly one group. Date from doc or {today}. Lowercase underscores in filenames. Tags: 5-15 lowercase keywords. Filename: include entity names, amounts, ref numbers."""


def analyze_batch(images: list[bytes], config: Config) -> list[tuple[list[int], DocumentInfo]]:
    """Analyze a batch of scanned pages, group by document, classify each group.

    Returns a list of (page_indices, DocumentInfo) tuples where page_indices
    are 0-based indices into the images list.
    """
    if len(images) > 50:
        raise AnalysisError("Batch scan supports up to 50 pages. Please scan fewer pages.")

    print(f"Batch analyzing {len(images)} pages...", file=sys.stderr)
    client = anthropic.Anthropic(api_key=config.api_key, timeout=180.0)

    content: list[dict] = []
    for img_data in images:
        # Use smaller images for batch to reduce token cost
        resized = _resize_for_api(img_data, max_dim=1200)
        b64 = base64.standard_b64encode(resized).decode("ascii")
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        })

    content.append({
        "type": "text",
        "text": BATCH_ANALYSIS_PROMPT.format(
            today=date.today().isoformat(),
            categories=", ".join(ALL_CATEGORIES),
            num_pages=len(images),
        ),
    })

    try:
        message = client.messages.create(
            model=config.claude_model,
            max_tokens=4096,
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.APITimeoutError as e:
        raise AnalysisError("Claude API timed out. Try again or use fewer pages.") from e
    except anthropic.APIError as e:
        raise AnalysisError(f"Claude API error: {e}") from e

    response_text = message.content[0].text.strip()
    if response_text.startswith("```"):
        lines = response_text.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        response_text = "\n".join(lines)

    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as e:
        raise AnalysisError(
            f"Failed to parse batch response: {e}\n{response_text}"
        ) from e

    if not isinstance(data, list):
        raise AnalysisError(f"Expected JSON array from batch analysis, got {type(data).__name__}")

    results = []
    for doc in data:
        pages = [p - 1 for p in doc.get("pages", [])]  # 1-indexed → 0-indexed
        doc_info = DocumentInfo(
            category=doc.get("category", "other"),
            filename=doc.get("filename", f"{date.today().isoformat()}_document.pdf"),
            summary=doc.get("summary", ""),
            date=doc.get("date"),
            key_fields=doc.get("key_fields", {}),
            suggested_categories=doc.get("suggested_categories", []),
            tags=doc.get("tags", []),
            risk_level=doc.get("risk_level", "none"),
            risks=doc.get("risks", []),
        )
        results.append((pages, doc_info))
        print(f"  Doc {len(results)}: {doc_info.filename} (pages {doc.get('pages', [])})", file=sys.stderr)

    print(f"Detected {len(results)} document(s)", file=sys.stderr)
    return results
