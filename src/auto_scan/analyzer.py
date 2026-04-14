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
    "legal", "warranty", "subscription", "donation", "investment",
    "pension", "certificate", "permit", "registration", "membership",
    "manual", "other",
]

ANALYSIS_PROMPT = """\
You are a document classification expert. Analyze this scanned document carefully.

Read the ENTIRE document — headers, footers, letterheads, logos, stamps, handwriting, tables, fine print. Identify:
- What TYPE of document this is (invoice, contract, insurance policy, medical record, bank statement, tax form, certificate, permit, letter, receipt, etc.)
- WHO issued it (company, organization, government body, individual)
- WHO is it addressed to or about
- WHAT is the subject (product, service, transaction, claim, application, etc.)
- KEY identifiers: reference numbers, policy numbers, invoice numbers, dates, amounts

Return ONLY valid JSON (no markdown):

"category": pick the best match from [{categories}]. If none fits well, use "other" and let the filename be descriptive.
"filename": YYYY-MM-DD_category_who_what_details.pdf — be as specific as possible. Include entity names, product/model names, amounts, ref numbers. Examples: "2022-06-10_contract_bmw_x3_g01_sale.pdf", "2024-03-15_invoice_vodafone_march_89eur.pdf", "2025-01-20_insurance_axa_home_policy_renewal.pdf", "2024-11-05_medical_dr_mueller_blood_test_results.pdf". Use the document date if visible, otherwise {today}. Lowercase, underscores, no special characters.
"summary": one clear sentence describing the document
"date": YYYY-MM-DD or null
"key_fields": relevant extracted key-value pairs (vendor, amount, parties, ref numbers, policy numbers, account numbers, etc.)
"suggested_categories": 3-5 best matching categories from the list above, ordered by relevance
"tags": 5-15 lowercase keywords from the actual content — document type, company names, people names, product/model names, topics, amounts, locations. Be specific, not generic.
"risk_level": "none"|"low"|"medium"|"high"
"risks": [] if clean, else short strings flagging: scams, misleading terms, hidden fees, unfair clauses, inconsistencies, phishing signals, unusual urgency"""


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
    confidence: int = 100
    page_confidence: dict[int, int] = field(default_factory=dict)


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
    """Resize image if needed and strip EXIF metadata before sending to API."""
    img = _open_image(image_data)
    w, h = img.size

    if w > max_dim or h > max_dim:
        scale = max_dim / max(w, h)
        new_size = (int(w * scale), int(h * scale))
        img = img.resize(new_size, Image.LANCZOS)

    # Always re-encode to strip EXIF/metadata (scanner serial, timestamps, etc.)
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


def _maybe_redact(image_data: bytes, redact_enabled: bool, redact_patterns: set[str] | None = None) -> bytes:
    """Optionally redact sensitive information from image before sending to API."""
    if not redact_enabled:
        return image_data
    from auto_scan.redactor import redact_image
    result = redact_image(image_data, enabled_patterns=redact_patterns)
    return result.redacted_image


def analyze_document(
    images: list[bytes], config: Config,
    redact: bool = False, redact_patterns: set[str] | None = None,
) -> DocumentInfo:
    """Send scanned page images to Claude Vision for classification and naming."""
    check_rate_limit()
    print("Analyzing document with AI...", file=sys.stderr)

    client = anthropic.Anthropic(api_key=config.api_key, timeout=120.0)

    # Build message content: one image block per page + text prompt
    content: list[dict] = []
    for i, img_data in enumerate(images[:20]):  # Claude supports up to 20 images
        safe_img = _maybe_redact(img_data, redact, redact_patterns)
        resized = _resize_for_api(safe_img)
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
You are a document sorting and classification expert. You receive {num_pages} scanned pages from a mixed stack fed through an automatic document feeder. Pages from DIFFERENT documents are likely shuffled together. Each image is labeled with its page number in the top-left corner.

Your job: read every page, figure out which pages belong together as one document, and classify each document.

═══ STEP 1: THOROUGHLY READ EVERY PAGE ═══
For each page, carefully examine:
  a) Document TYPE — what kind of document is this? (invoice, insurance policy, sales contract, bank statement, medical report, tax form, letter, certificate, permit, rental agreement, payslip, receipt, warranty card, prescription, court document, birth/marriage/death certificate, transcript, membership card, donation receipt, etc.)
  b) Header/letterhead — company name, logo, organization, government body
  c) Reference numbers — invoice #, policy #, contract #, order #, case #, account #, claim #, file #
  d) Sender and recipient — who wrote this and to whom
  e) Page numbering — "Page 2 of 4", "2/4", sequential numbering
  f) Language and formatting — font, layout, paper style, stamps, signatures
  g) Content topic — what is this page actually about?

═══ STEP 2: GROUP BY DOCUMENT IDENTITY ═══
Two pages belong to the SAME document ONLY when ALL of these match:
  - Same document TYPE (an insurance policy ≠ a sales contract, even if from the same company)
  - Same reference/policy/contract/invoice number
  - Same sender AND recipient pair
  - Compatible page numbering (page 1,2,3 — not two page 1s)
  - Same formatting, letterhead, and visual style
  - Continuous narrative or data (page 2 continues where page 1 left off)

CRITICAL — these are ALWAYS separate documents:
  ✗ An insurance document and a sales contract → 2 docs, even if same company
  ✗ A cover letter and the enclosed form or attachment → 2 docs
  ✗ Different document types with different headers → separate
  ✗ Pages about different topics, transactions, or time periods → separate
  ✗ A renewal notice and the original policy → 2 docs
  ✗ A receipt and the invoice for the same purchase → 2 docs
  ✗ Documents in different languages (unless clearly one bilingual document) → likely separate
  ✗ A terms & conditions insert and the main document → 2 docs

WHEN IN DOUBT → SPLIT. It is much better to over-split (create too many small documents) than to wrongly merge pages from different documents into one.

The CONTENT and DOCUMENT TYPE visible on the page is the strongest grouping signal. Trust what you see on each page, not assumptions about what "should" be together.

═══ STEP 3: CLASSIFY AND OUTPUT ═══
For each document group, determine the best category, generate a descriptive filename, and extract key information.

Return ONLY valid JSON (no markdown) — an array of document groups:

[{{"pages": [1, 2], "page_confidence": {{"1": 95, "2": 80}}, "confidence": 87, "category": "...", "filename": "YYYY-MM-DD_category_who_what.pdf", "summary": "...", "date": "YYYY-MM-DD or null", "key_fields": {{}}, "suggested_categories": [], "tags": [], "risk_level": "none"|"low"|"medium"|"high", "risks": []}}]

CONFIDENCE SCORING (0–100):
  - "page_confidence": for EACH page, how certain you are it belongs to THIS document group
  - "confidence": overall confidence for the entire document grouping
  - 90–100: Very clear — same header, reference number, continuous content
  - 70–89: Likely — similar style and content, minor ambiguity
  - 50–69: Uncertain — could plausibly belong to another group
  - Below 50: Weak — little evidence for this grouping

FILENAME RULES:
  - Format: YYYY-MM-DD_category_who_what_details.pdf
  - Be specific: include entity names, product names, amounts, reference numbers
  - Examples: "2025-03-15_invoice_vodafone_march_89eur.pdf", "2024-06-10_contract_bmw_x3_sale.pdf", "2025-01-20_insurance_axa_home_policy_H847291.pdf", "2024-11-05_medical_dr_chen_referral_cardiology.pdf"
  - Lowercase, underscores, no special characters

Categories: {categories}
If none of these categories fits well, use "other" and make the filename descriptive.

Pages numbered 1–{num_pages}. Every page must appear in exactly one group. Use the document date if visible, otherwise {today}. Tags: 5-15 lowercase keywords from actual content (entity names, product names, topics, amounts, locations — be specific, not generic)."""


def analyze_batch(
    images: list[bytes], config: Config,
    redact: bool = False, redact_patterns: set[str] | None = None,
) -> list[tuple[list[int], DocumentInfo]]:
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
        safe_img = _maybe_redact(img_data, redact, redact_patterns)
        labeled = _label_page(safe_img, i + 1, max_dim=1568)
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

    results = _parse_batch_results(data)

    for pages, doc_info in results:
        page_nums = [p + 1 for p in pages]
        print(f"  Doc: {doc_info.filename} (pages {page_nums}, confidence {doc_info.confidence}%)", file=sys.stderr)

    # ── Verification pass for low-confidence pages ──────────────────
    CONFIDENCE_THRESHOLD = 75
    uncertain_pages = []
    for pages, doc_info in results:
        for p in pages:
            page_num = p + 1
            pc = doc_info.page_confidence.get(page_num, doc_info.confidence)
            if pc < CONFIDENCE_THRESHOLD:
                uncertain_pages.append(page_num)

    if uncertain_pages:
        print(f"  {len(uncertain_pages)} uncertain page(s) (confidence < {CONFIDENCE_THRESHOLD}%), running verification...", file=sys.stderr)
        results = _verify_uncertain_pages(
            images, results, uncertain_pages, content, config, client,
            redact=redact, redact_patterns=redact_patterns,
        )

    print(f"Detected {len(results)} document(s)", file=sys.stderr)
    return results


def _parse_batch_results(data: list[dict]) -> list[tuple[list[int], DocumentInfo]]:
    """Parse batch analysis JSON into (pages, DocumentInfo) tuples."""
    results = []
    for doc in data:
        pages = [p - 1 for p in doc.get("pages", [])]
        page_conf_raw = doc.get("page_confidence", {})
        page_confidence = {int(k): int(v) for k, v in page_conf_raw.items()}
        confidence = int(doc.get("confidence", 100))
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
            confidence=confidence,
            page_confidence=page_confidence,
        )
        results.append((pages, doc_info))
    return results


VERIFY_PROMPT = """\
You are verifying uncertain page assignments in a document sorting task.

The first model sorted {num_pages} scanned pages into document groups, but was uncertain about {num_uncertain} page(s): {uncertain_list}.

Its initial grouping was:
{initial_grouping}

For EACH uncertain page, look at the image carefully and decide:
  - Does it belong to the document group it was assigned to?
  - Or should it be moved to a different group?
  - Or should it be its own separate document?

Return ONLY valid JSON — an array of reassignments (empty array if all assignments are correct):
[{{"page": 3, "move_to_doc": 1, "reason": "short explanation"}}]

"move_to_doc" is the 1-based document number from the initial grouping, or "new" to create a separate document.
If a page's assignment is correct, do NOT include it in the array."""


def _verify_uncertain_pages(
    images: list[bytes],
    results: list[tuple[list[int], DocumentInfo]],
    uncertain_pages: list[int],
    original_content: list[dict],
    config: Config,
    client: anthropic.Anthropic,
    redact: bool = False,
    redact_patterns: set[str] | None = None,
) -> list[tuple[list[int], DocumentInfo]]:
    """Re-analyze uncertain pages with a verification model."""

    # Build a summary of the initial grouping for context
    grouping_lines = []
    for i, (pages, doc_info) in enumerate(results):
        page_nums = [p + 1 for p in pages]
        confs = [f"p{p}:{doc_info.page_confidence.get(p, doc_info.confidence)}%" for p in page_nums]
        grouping_lines.append(
            f"  Doc {i + 1}: pages {page_nums} — {doc_info.category} — \"{doc_info.summary}\" ({', '.join(confs)})"
        )

    verify_text = VERIFY_PROMPT.format(
        num_pages=sum(len(pages) for pages, _ in results),
        num_uncertain=len(uncertain_pages),
        uncertain_list=", ".join(str(p) for p in uncertain_pages),
        initial_grouping="\n".join(grouping_lines),
    )

    # Send only the uncertain page images + context (redacted if enabled)
    verify_content: list[dict] = []
    for p in uncertain_pages:
        safe_img = _maybe_redact(images[p - 1], redact, redact_patterns)
        labeled = _label_page(safe_img, p, max_dim=1568)
        b64 = base64.standard_b64encode(labeled).decode("ascii")
        verify_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        })
    verify_content.append({"type": "text", "text": verify_text})

    # Use Opus for verification — more capable model for uncertain pages
    verify_model = "claude-opus-4-20250514"

    try:
        check_rate_limit()
        message = client.messages.create(
            model=verify_model,
            max_tokens=2048,
            system="You are a document verification API. Output ONLY valid JSON arrays. No explanations.",
            messages=[
                {"role": "user", "content": verify_content},
                {"role": "assistant", "content": "["},
            ],
        )
    except Exception as e:
        print(f"  Verification failed ({e}), keeping original assignments", file=sys.stderr)
        return results

    record_usage(message.usage.input_tokens, message.usage.output_tokens)
    print(
        f"  Verify tokens: {message.usage.input_tokens:,} in + {message.usage.output_tokens:,} out",
        file=sys.stderr,
    )

    response_text = "[" + "".join(
        block.text for block in message.content if hasattr(block, "text")
    ).strip()

    try:
        reassignments = json.loads(response_text)
    except json.JSONDecodeError:
        print("  Verification response not parseable, keeping original", file=sys.stderr)
        return results

    if not isinstance(reassignments, list) or not reassignments:
        print("  Verification confirmed all assignments", file=sys.stderr)
        return results

    # Apply reassignments
    for fix in reassignments:
        page = fix.get("page")
        target = fix.get("move_to_doc")
        reason = fix.get("reason", "")
        if not page:
            continue

        page_0 = page - 1  # 0-indexed

        # Find which doc currently has this page
        src_idx = None
        for i, (pages, _) in enumerate(results):
            if page_0 in pages:
                src_idx = i
                break
        if src_idx is None:
            continue

        if target == "new":
            # Create a new single-page document
            results[src_idx][0].remove(page_0)
            new_doc = DocumentInfo(
                category="other",
                filename=f"{date.today().isoformat()}_page_{page}.pdf",
                summary=f"Separated by verification: {reason}",
                date=None,
                confidence=50,
                page_confidence={page: 50},
            )
            results.append(([page_0], new_doc))
            print(f"  Verify: page {page} → new document ({reason})", file=sys.stderr)
        else:
            dst_idx = int(target) - 1  # 1-indexed → 0-indexed
            if 0 <= dst_idx < len(results) and dst_idx != src_idx:
                results[src_idx][0].remove(page_0)
                results[dst_idx][0].append(page_0)
                results[dst_idx][0].sort()
                print(f"  Verify: page {page} → doc {target} ({reason})", file=sys.stderr)

    # Remove empty document groups
    results = [(pages, doc_info) for pages, doc_info in results if pages]

    return results
