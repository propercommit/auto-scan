"""Document analysis using Claude Vision API."""

from __future__ import annotations

import base64
import io
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date

import anthropic
from PIL import Image

from auto_scan import AnalysisError
from auto_scan.config import Config
from auto_scan.usage import check_rate_limit, record_usage

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


def _open_image(image_data: bytes) -> Image.Image:
    """Open image bytes, handling formats PIL can't directly open (e.g. PDF from scanner)."""
    try:
        img = Image.open(io.BytesIO(image_data))
        img.load()
        return img
    except Exception:
        pass
    # Scanner may have returned PDF — extract first page image via pikepdf
    if image_data[:5] == b"%PDF-":
        try:
            import pikepdf
            pdf = pikepdf.open(io.BytesIO(image_data))
            page = pdf.pages[0]
            for image_key in page.images:
                pil_img = page.images[image_key].as_pil_image()
                pdf.close()
                return pil_img
            pdf.close()
        except Exception:
            pass
    raise AnalysisError(
        "Cannot read scanned image. The scanner may have returned an unsupported format. "
        "Try setting document_format to image/jpeg in scanner settings."
    )


def _resize_for_api(image_data: bytes, max_dim: int = 1568) -> bytes:
    """Resize image if either dimension exceeds max_dim, preserving aspect ratio."""
    img = _open_image(image_data)
    w, h = img.size

    if w <= max_dim and h <= max_dim:
        return image_data

    scale = max_dim / max(w, h)
    new_size = (int(w * scale), int(h * scale))
    img = img.resize(new_size, Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _label_page(image_data: bytes, page_num: int, max_dim: int = 1568) -> bytes:
    """Resize an image and burn a page-number label into the top-left corner."""
    from PIL import ImageDraw, ImageFont

    img = _open_image(image_data)
    w, h = img.size
    if w > max_dim or h > max_dim:
        scale = max_dim / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    # Ensure RGB for drawing
    if img.mode != "RGB":
        img = img.convert("RGB")

    draw = ImageDraw.Draw(img)
    label = f" PAGE {page_num} "
    font_size = max(18, img.width // 30)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
    except (OSError, IOError):
        try:
            font = ImageFont.truetype("arial.ttf", font_size)
        except (OSError, IOError):
            font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), label, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    margin = 8
    draw.rectangle([margin, margin, margin + tw + 12, margin + th + 8], fill=(0, 0, 0))
    draw.text((margin + 6, margin + 4), label, fill=(255, 255, 255), font=font)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def analyze_document(images: list[bytes], config: Config) -> DocumentInfo:
    """Send scanned page images to Claude Vision for classification and naming."""
    check_rate_limit()
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
            max_tokens=2048,
            system=(
                "You are a document classification API. You ONLY output valid JSON objects. "
                "Never output explanations, thinking, or markdown — ONLY the JSON object."
            ),
            messages=[
                {"role": "user", "content": content},
                {"role": "assistant", "content": "{"},
            ],
        )
    except anthropic.APITimeoutError as e:
        raise AnalysisError("Claude API timed out. Try again or use fewer pages.") from e
    except anthropic.APIError as e:
        raise AnalysisError(f"Claude API error: {e}") from e

    # Record token usage
    record_usage(message.usage.input_tokens, message.usage.output_tokens)
    print(
        f"  Tokens: {message.usage.input_tokens:,} in + {message.usage.output_tokens:,} out",
        file=sys.stderr,
    )

    # Parse the JSON response — prepend "{" from assistant prefill
    response_text = ""
    for block in message.content:
        if hasattr(block, "text"):
            response_text += block.text
    response_text = "{" + response_text.strip()

    # Strip markdown code fences if present
    if response_text.startswith("```"):
        lines = response_text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        response_text = "\n".join(lines).strip()

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


def _repair_truncated_json(text: str) -> str:
    """Attempt to repair JSON truncated by max_tokens.

    Finds the last complete object in an array and closes the array.
    """
    # Find the last complete object by looking for the last "},"  or "}" before truncation
    # Strategy: try progressively shorter prefixes until one parses
    # First try: close any open strings, objects, arrays
    for trim in range(min(200, len(text)), 0, -1):
        candidate = text[:len(text) - trim]
        # Find last complete }, then close the array
        last_brace = candidate.rfind("}")
        if last_brace > 0:
            attempt = candidate[:last_brace + 1].rstrip().rstrip(",") + "]"
            try:
                json.loads(attempt)
                return attempt
            except json.JSONDecodeError:
                continue
    return text  # give up, return as-is


# ── Batch analysis ──────────────────────────────────────────────────

BATCH_ANALYSIS_PROMPT = """\
You are a document sorting expert. You have {num_pages} scanned pages from a mixed stack. Pages from DIFFERENT documents may be shuffled together. Each image is labeled with its page number in the top-left corner.

═══ STEP 1: READ EVERY PAGE ═══
For each page, identify:
  a) Document TYPE visible in the title/header (e.g. "Insurance Policy", "Sales Agreement", "Invoice", "Bank Statement")
  b) Company/organization name in the letterhead or header
  c) Reference/policy/contract/invoice NUMBER
  d) Page numbering if visible (e.g. "Page 2 of 4")
  e) The TOPIC — what is this page actually about?

═══ STEP 2: GROUP BY DOCUMENT IDENTITY ═══
Two pages belong to the SAME document ONLY when ALL of these match:
  - Same document TYPE (an insurance policy ≠ a sales contract, even from the same company)
  - Same reference/policy/contract number
  - Same sender AND recipient pair
  - Compatible page numbering (1,2,3 not 1,1)
  - Same formatting and letterhead

CRITICAL — these are ALWAYS separate documents:
  ✗ An insurance document and a sales document → 2 docs, even if same company
  ✗ A cover letter and the enclosed form → 2 docs
  ✗ Different document types with different headers → separate
  ✗ Pages about different topics or transactions → separate
  ✗ A renewal notice and the original policy → 2 docs

WHEN IN DOUBT → SPLIT. It is much better to over-split (too many small docs) than to wrongly merge pages from different documents.

The CONTENT and DOCUMENT TYPE on the page is the strongest signal. Two pages that say "Insurance" in the header must be grouped under insurance, not sales — regardless of what other pages look like.

═══ STEP 3: CLASSIFY AND OUTPUT ═══
Return ONLY valid JSON (no markdown) — an array of document groups:

[{{"pages": [1, 2], "category": "...", "filename": "YYYY-MM-DD_cat_who_what.pdf", "summary": "...", "date": "YYYY-MM-DD or null", "key_fields": {{}}, "suggested_categories": [], "tags": [], "risk_level": "none"|"low"|"medium"|"high", "risks": []}}]

Categories: {categories}
Pages numbered 1–{num_pages}. Every page must appear in exactly one group. Use the document date if visible, otherwise {today}. Filenames: lowercase underscores, include entity names, amounts, ref numbers. Tags: 5-15 lowercase keywords from the content."""


def analyze_batch(images: list[bytes], config: Config) -> list[tuple[list[int], DocumentInfo]]:
    """Analyze a batch of scanned pages, group by document, classify each group.

    Returns a list of (page_indices, DocumentInfo) tuples where page_indices
    are 0-based indices into the images list.
    """
    if len(images) > 50:
        raise AnalysisError("Batch scan supports up to 50 pages. Please scan fewer pages.")

    check_rate_limit()
    print(f"Batch analyzing {len(images)} pages...", file=sys.stderr)
    client = anthropic.Anthropic(api_key=config.api_key, timeout=180.0)

    content: list[dict] = []
    for i, img_data in enumerate(images):
        labeled = _label_page(img_data, i + 1, max_dim=1568)
        b64 = base64.standard_b64encode(labeled).decode("ascii")
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
            max_tokens=8192,
            system=(
                "You are a document classification API. You ONLY output valid JSON arrays. "
                "Never output explanations, thinking, or markdown — ONLY the JSON array. "
                "Read the text on every page to classify correctly. "
                "A page with 'Insurance' in the header is insurance, not sales. "
                "Split different document types into separate groups."
            ),
            messages=[
                {"role": "user", "content": content},
                {"role": "assistant", "content": "["},
            ],
        )
    except anthropic.APITimeoutError as e:
        raise AnalysisError("Claude API timed out. Try again or use fewer pages.") from e
    except anthropic.APIError as e:
        raise AnalysisError(f"Claude API error: {e}") from e

    # Record token usage
    record_usage(message.usage.input_tokens, message.usage.output_tokens)
    print(
        f"  Tokens: {message.usage.input_tokens:,} in + {message.usage.output_tokens:,} out",
        file=sys.stderr,
    )

    # Extract text from response — prepend "[" from assistant prefill
    response_text = ""
    for block in message.content:
        if hasattr(block, "text"):
            response_text += block.text
    response_text = "[" + response_text.strip()

    if response_text == "[":
        stop = message.stop_reason
        raise AnalysisError(
            f"Empty response from Claude (stop_reason={stop}). "
            f"The document may be too large — try scanning fewer pages."
        )

    # Strip markdown code fences
    if response_text.startswith("```"):
        lines = response_text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        response_text = "\n".join(lines).strip()

    # Handle truncated JSON (max_tokens hit)
    if message.stop_reason == "max_tokens":
        print("  Warning: response truncated (max_tokens), attempting repair", file=sys.stderr)
        # Try to close any open arrays/objects
        response_text = _repair_truncated_json(response_text)

    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as e:
        # Log first 500 chars for debugging
        preview = response_text[:500] if response_text else "(empty)"
        raise AnalysisError(
            f"Failed to parse batch response: {e}\nResponse preview: {preview}"
        ) from e

    if not isinstance(data, list):
        # If it's a single object, wrap it
        if isinstance(data, dict) and "pages" in data:
            data = [data]
        else:
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
