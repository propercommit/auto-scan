"""Document recognition engine — API calls, image prep, JSON parsing.

All model instructions live in prompts.py. This file handles the
mechanics: building API requests, parsing responses, verifying
uncertain pages, and orchestrating the recognition pipeline.
"""

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
from auto_scan.recognition.prompts import (
    ALL_CATEGORIES,
    ANALYSIS_PROMPT,
    BATCH_ANALYSIS_PROMPT,
    VERIFY_PROMPT,
    SYSTEM_SINGLE,
    SYSTEM_BATCH,
    SYSTEM_VERIFY,
)


def build_filename(fields: dict, today: str) -> str:
    """Build a deterministic filename from structured document fields.

    Pattern: {date}_{category}_{issuer}_{subject}[_{ref}].pdf

    The model returns structured fields (issuer, subject, ref_number);
    this function assembles them into a consistent filename. This avoids
    model hallucination of filename formats and ensures deterministic output.

    Examples:
        >>> build_filename({"date": "2024-03-15", "category": "invoice",
        ...     "issuer": "Vodafone", "subject": "mobile_bill_march"}, "2025-01-01")
        '2024-03-15_invoice_vodafone_mobile_bill_march.pdf'
    """
    def _slug(s: str, max_len: int = 40) -> str:
        """Normalize a string into a filename-safe slug."""
        s = s.lower().strip()
        s = re.sub(r"[^a-z0-9]+", "_", s)
        return s.strip("_")[:max_len]

    doc_date = fields.get("date") or today
    # Validate date format — fall back to today if invalid
    if not re.match(r"\d{4}-\d{2}-\d{2}$", str(doc_date)):
        doc_date = today

    category = _slug(fields.get("category", "other") or "other")
    issuer = _slug(fields.get("issuer", "") or "")
    subject = _slug(fields.get("subject", "") or "")
    ref = _slug(fields.get("ref_number", "") or "")

    parts = [doc_date, category]
    if issuer:
        parts.append(issuer)
    if subject:
        parts.append(subject)
    if ref:
        parts.append(ref)

    # Fallback: if only date + category, add "document"
    if len(parts) <= 2:
        parts.append("document")

    return "_".join(parts) + ".pdf"



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


def _region_histogram_similarity(img_a: Image.Image, img_b: Image.Image,
                                  top_frac: float, bottom_frac: float) -> float:
    """Compare a horizontal strip of two pages using color histogram intersection.

    This is sensitive to different colored letterheads, logos, and backgrounds.
    Returns 0–100 (100 = identical histograms).
    """
    def _crop(img: Image.Image) -> Image.Image:
        w, h = img.size
        return img.crop((0, int(h * top_frac), w, int(h * bottom_frac)))

    a = _crop(img_a).resize((400, 60)).convert("RGB")
    b = _crop(img_b).resize((400, 60)).convert("RGB")

    # Build RGB histograms (32 bins per channel)
    ha = a.histogram()  # 768 values: 256 R + 256 G + 256 B
    hb = b.histogram()

    # Reduce to 32 bins per channel for robustness
    def _reduce(hist: list[int], bins: int = 32) -> list[float]:
        step = 256 // bins
        reduced = []
        for ch in range(3):
            base = ch * 256
            for b_idx in range(bins):
                reduced.append(sum(hist[base + b_idx * step : base + (b_idx + 1) * step]))
            # Normalize to sum to 1
            total = sum(reduced[-bins:]) or 1
            for j in range(bins):
                reduced[-bins + j] /= total
        return reduced

    ha_r = _reduce(ha)
    hb_r = _reduce(hb)

    # Histogram intersection (sum of min values) — 1.0 = identical
    intersection = sum(min(a, b) for a, b in zip(ha_r, hb_r))
    # 3 channels, each normalized to 1.0 → max intersection = 3.0
    return round(intersection / 3.0 * 100.0, 1)


def _region_pixel_similarity(img_a: Image.Image, img_b: Image.Image,
                              top_frac: float, bottom_frac: float) -> float:
    """Compare a strip using pixel-level grayscale difference.

    Catches text/layout differences that histogram can miss.
    """
    def _crop(img: Image.Image) -> Image.Image:
        w, h = img.size
        return img.crop((0, int(h * top_frac), w, int(h * bottom_frac)))

    a = _crop(img_a).resize((500, 80)).convert("L")
    b = _crop(img_b).resize((500, 80)).convert("L")
    px_a, px_b = list(a.getdata()), list(b.getdata())
    diff = sum(abs(a - b) for a, b in zip(px_a, px_b)) / len(px_a)
    return round(max(0.0, 100.0 - diff * 100.0 / 80.0), 1)


def _page_similarity(img_a: Image.Image, img_b: Image.Image) -> float:
    """Compare two pages for document boundary detection.

    Combines color histogram similarity (catches different colored headers/logos)
    with pixel-level comparison (catches text/layout differences).
    Uses header (top 15%) and footer (bottom 10%) regions.
    Returns 0–100.
    """
    # Color histogram: sensitive to different colored backgrounds, logos
    h_hist = _region_histogram_similarity(img_a, img_b, 0.0, 0.15)
    f_hist = _region_histogram_similarity(img_a, img_b, 0.88, 1.0)

    # Pixel-level: catches text and layout differences
    h_pixel = _region_pixel_similarity(img_a, img_b, 0.0, 0.15)
    f_pixel = _region_pixel_similarity(img_a, img_b, 0.88, 1.0)

    # Use the MINIMUM of histogram and pixel similarity per region
    # (either signal detecting a difference is enough)
    header_sim = min(h_hist, h_pixel)
    footer_sim = min(f_hist, f_pixel)

    # Header dominates (70%) — it's the strongest document identity signal
    return round(header_sim * 0.70 + footer_sim * 0.30, 1)


def _detect_page_numbers(text: str) -> list[str]:
    """Extract page numbering patterns from OCR text."""
    patterns = [
        r"[Pp]age\s+(\d+)\s+(?:of|/)\s+(\d+)",          # Page 2 of 4
        r"[Ss]eite\s+(\d+)\s+von\s+(\d+)",               # Seite 2 von 4
        r"(\d+)\s*/\s*(\d+)",                              # 2/4
        r"[Pp]\.\s*(\d+)\s*/\s*(\d+)",                    # P. 2/4
        r"-\s*(\d+)\s*-",                                  # - 2 -
    ]
    found = []
    for pat in patterns:
        for m in re.finditer(pat, text):
            found.append(m.group(0).strip())
    return found


def _compute_page_hints(images: list[bytes]) -> str:
    """Pre-process pages locally to produce grouping hints for the AI.

    1. Visual header similarity between consecutive pages
    2. OCR-detected page numbering patterns

    Returns a formatted hint block to append to the batch prompt.
    """
    n = len(images)
    if n < 2:
        return ""

    # ── Step 1: Visual similarity (header + footer + logo) ─────────────
    pil_images = [_open_image(img) for img in images]
    similarities = []
    for i in range(n - 1):
        score = _page_similarity(pil_images[i], pil_images[i + 1])
        similarities.append((i + 1, i + 2, score))

    header_lines = []
    # Group consecutive similar pages into runs
    run_start = 1
    for i, (p1, p2, score) in enumerate(similarities):
        if score < 75:
            # End of a run — report the run and the boundary
            if p1 > run_start:
                header_lines.append(
                    f"  Pages {run_start}\u2013{p1}: Similar layout (likely same document)"
                )
            header_lines.append(
                f"  Pages {p1}\u2192{p2}: DIFFERENT layout ({score:.0f}% match) \u2014 likely document boundary"
            )
            run_start = p2
        elif i == len(similarities) - 1:
            # Final run
            if p2 > run_start:
                header_lines.append(
                    f"  Pages {run_start}\u2013{p2}: Similar layout (likely same document)"
                )

    # ── Step 2: OCR page numbering ─────────────────────────────────────
    page_num_lines = []
    try:
        import pytesseract
        for i, img in enumerate(pil_images):
            try:
                # OCR just the top and bottom 15% for speed
                w, h = img.size
                regions = [
                    img.crop((0, 0, w, int(h * 0.15))),
                    img.crop((0, int(h * 0.85), w, h)),
                ]
                text = ""
                for region in regions:
                    text += pytesseract.image_to_string(region, timeout=5) + "\n"
                nums = _detect_page_numbers(text)
                if nums:
                    page_num_lines.append(f"  Page {i + 1}: detected \"{nums[0]}\"")
            except Exception:
                continue
    except ImportError:
        pass  # pytesseract not installed — skip OCR hints

    # ── Build hint block ───────────────────────────────────────────────
    if not header_lines and not page_num_lines:
        return ""

    parts = ["\n\n\u2550\u2550\u2550 LOCAL ANALYSIS HINTS (pre-computed by image analysis) \u2550\u2550\u2550"]
    parts.append("Use these hints alongside your own reading. They may contain errors \u2014 your visual analysis of content takes priority.\n")

    if header_lines:
        parts.append("HEADER SIMILARITY (pages with matching letterheads/headers):")
        parts.extend(header_lines)
        parts.append("")

    if page_num_lines:
        parts.append("PAGE NUMBERING (detected by OCR on page edges):")
        parts.extend(page_num_lines)
        parts.append("")

    return "\n".join(parts)


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
            system=SYSTEM_SINGLE,
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

    # Merge top-level extraction fields (issuer, subject, ref_number)
    # into key_fields so they're preserved alongside other extracted data
    key_fields = data.get("key_fields", {})
    for extract_key in ("issuer", "subject", "ref_number"):
        val = data.get(extract_key)
        if val and extract_key not in key_fields:
            key_fields[extract_key] = val

    doc_info = DocumentInfo(
        category=data.get("category", "other"),
        filename=build_filename(data, date.today().isoformat()),
        summary=data.get("summary", ""),
        date=data.get("date"),
        key_fields=key_fields,
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

    # Pre-compute local grouping hints (header similarity + OCR page numbers)
    print("  Computing page grouping hints...", file=sys.stderr)
    page_hints = _compute_page_hints(images)
    if page_hints:
        print(f"  Hints ready ({page_hints.count(chr(10))} lines)", file=sys.stderr)

    content: list[dict] = []
    for i, img_data in enumerate(images):
        safe_img = _maybe_redact(img_data, redact, redact_patterns)
        labeled = _label_page(safe_img, i + 1, max_dim=1568)
        b64 = base64.standard_b64encode(labeled).decode("ascii")
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        })

    prompt_text = BATCH_ANALYSIS_PROMPT.format(
        today=date.today().isoformat(),
        categories=", ".join(ALL_CATEGORIES),
        num_pages=len(images),
    )
    if page_hints:
        prompt_text += page_hints

    content.append({"type": "text", "text": prompt_text})

    try:
        message = client.messages.create(
            model=config.claude_model,
            max_tokens=8192,
            system=SYSTEM_BATCH,
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
    CONFIDENCE_THRESHOLD = 90
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
    today = date.today().isoformat()
    results = []
    for doc in data:
        pages = [p - 1 for p in doc.get("pages", [])]
        page_conf_raw = doc.get("page_confidence", {})
        page_confidence = {int(k): int(v) for k, v in page_conf_raw.items()}
        confidence = int(doc.get("confidence", 100))

        # Merge top-level extraction fields into key_fields
        key_fields = doc.get("key_fields", {})
        for extract_key in ("issuer", "subject", "ref_number"):
            val = doc.get(extract_key)
            if val and extract_key not in key_fields:
                key_fields[extract_key] = val

        doc_info = DocumentInfo(
            category=doc.get("category", "other"),
            filename=build_filename(doc, today),
            summary=doc.get("summary", ""),
            date=doc.get("date"),
            key_fields=key_fields,
            suggested_categories=doc.get("suggested_categories", []),
            tags=doc.get("tags", []),
            risk_level=doc.get("risk_level", "none"),
            risks=doc.get("risks", []),
            confidence=confidence,
            page_confidence=page_confidence,
        )
        results.append((pages, doc_info))
    return results


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
    """Re-analyze uncertain pages with a stronger model using extended thinking.

    Sends ALL page images (not just uncertain ones) so the verification model
    can compare headers, reference numbers, and content across the full batch.
    Uses extended thinking for deeper reasoning about page assignments.
    """

    # Build a summary of the initial grouping for context
    grouping_lines = []
    for i, (pages, doc_info) in enumerate(results):
        page_nums = [p + 1 for p in pages]
        confs = [f"p{p}:{doc_info.page_confidence.get(p, doc_info.confidence)}%" for p in page_nums]
        grouping_lines.append(
            f"  Doc {i + 1}: pages {page_nums} — {doc_info.category} — \"{doc_info.summary}\" ({', '.join(confs)})"
        )

    verify_text = VERIFY_PROMPT.format(
        num_pages=len(images),
        num_uncertain=len(uncertain_pages),
        uncertain_list=", ".join(str(p) for p in uncertain_pages),
        initial_grouping="\n".join(grouping_lines),
    )

    # Send ALL page images so the model can compare across the full batch
    verify_content: list[dict] = []
    for i, img_data in enumerate(images):
        safe_img = _maybe_redact(img_data, redact, redact_patterns)
        labeled = _label_page(safe_img, i + 1, max_dim=1568)
        b64 = base64.standard_b64encode(labeled).decode("ascii")
        verify_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        })
    verify_content.append({"type": "text", "text": verify_text})

    # Use Opus with extended thinking for deeper analysis
    verify_model = "claude-opus-4-20250514"
    thinking_budget = 10_000

    try:
        check_rate_limit()
        message = client.messages.create(
            model=verify_model,
            max_tokens=16_000,
            thinking={
                "type": "enabled",
                "budget_tokens": thinking_budget,
            },
            temperature=1,  # required for extended thinking
            system=SYSTEM_VERIFY,
            messages=[{"role": "user", "content": verify_content}],
        )
    except Exception as e:
        print(f"  Verification failed ({e}), keeping original assignments", file=sys.stderr)
        return results

    # Log token usage (extended thinking reports input + output separately)
    usage = message.usage
    in_tok = usage.input_tokens
    out_tok = usage.output_tokens
    # Cache tokens may be available
    cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    record_usage(in_tok, out_tok)
    extra = ""
    if cache_create or cache_read:
        extra = f" (cache: +{cache_create:,} create, {cache_read:,} read)"
    print(
        f"  Verify tokens: {in_tok:,} in + {out_tok:,} out{extra}",
        file=sys.stderr,
    )

    # Extract text from response — skip thinking blocks, only use text blocks
    response_text = ""
    for block in message.content:
        if getattr(block, "type", None) == "text" and hasattr(block, "text"):
            response_text += block.text

    response_text = response_text.strip()

    # Strip markdown code fences if present
    if response_text.startswith("```"):
        lines = response_text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        response_text = "\n".join(lines).strip()

    if not response_text:
        print("  Verification returned empty response, keeping original", file=sys.stderr)
        return results

    try:
        reassignments = json.loads(response_text)
    except json.JSONDecodeError:
        print(f"  Verification response not parseable, keeping original", file=sys.stderr)
        print(f"    Response preview: {response_text[:200]}", file=sys.stderr)
        return results

    if not isinstance(reassignments, list) or not reassignments:
        print("  Verification confirmed all assignments ✓", file=sys.stderr)
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
