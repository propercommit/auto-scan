"""Model instructions for document recognition.

This file contains ONLY the text sent to the AI — no code logic.
Edit these prompts to change how the model classifies, names, and
groups documents. The engine (engine.py) reads from here.

Placeholders filled at runtime:
    {today}        — current date (YYYY-MM-DD)
    {categories}   — comma-separated category list
    {num_pages}    — number of scanned pages (batch/verify)
    {num_uncertain}, {uncertain_list}, {initial_grouping} — verify only

Pipeline (batch uses a 2-step approach for higher accuracy):
    1. ANALYSIS_PROMPT        — classify + extract a single document (1–20 pages)
    2. BATCH_GROUPING_PROMPT  — group pages from a mixed stack (grouping only)
       then ANALYSIS_PROMPT   — classify each group separately (reuses step 1)
    3. VERIFY_PROMPT          — second pass on uncertain groupings (Opus + extended thinking)
    4. RISK_PROMPT            — optional separate pass for risk/scam analysis (future)
"""

# ── Document categories ───────────────────────────────────────────
# The AI picks from this list. Add new categories here and they
# automatically appear in the prompt and the GUI dropdown.

ALL_CATEGORIES = [
    "invoice", "receipt", "contract", "letter", "medical", "tax",
    "insurance", "bank", "government", "personal", "automobile",
    "housing", "education", "employment", "travel", "utilities",
    "legal", "warranty", "subscription", "donation", "investment",
    "pension", "certificate", "permit", "registration", "membership",
    "manual", "other",
]


# ── Single-document classification ────────────────────────────────
# Used by analyze_document() for one document (1–20 pages).
# The model returns structured fields; the engine builds the filename
# deterministically via build_filename() — no model-generated filenames.

ANALYSIS_PROMPT = """\
You are a document classification expert. Analyze this scanned document.

Read the ENTIRE document — headers, footers, letterheads, logos, stamps, \
handwriting, tables, fine print.

Identify and extract:
  - Document TYPE (invoice, contract, insurance policy, medical record, etc.)
  - ISSUER: the company, organization, or person who created/sent this
  - RECIPIENT: who it is addressed to or about
  - SUBJECT: what is this about (product, service, transaction, claim, etc.)
  - KEY IDENTIFIERS: reference numbers, policy numbers, invoice numbers, \
dates, amounts

Return ONLY valid JSON (no markdown):

{{
  "category": "<best match from [{categories}]>",
  "issuer": "<company or person who issued this>",
  "subject": "<brief subject: product, service, transaction>",
  "ref_number": "<primary reference/invoice/policy number or null>",
  "summary": "<one clear sentence describing the document>",
  "date": "<YYYY-MM-DD from the document, or null if not visible>",
  "key_fields": {{
    "amount": "<total amount with currency if present, else null>",
    "recipient": "<addressed to whom>",
    "<other relevant key>": "<value>"
  }},
  "suggested_categories": ["<2nd best>", "<3rd best>", "<4th best>"],
  "tags": ["<5-15 lowercase keywords from actual content>"],
  "risk_level": "none"|"low"|"medium"|"high",
  "risks": []
}}

FIELD RULES:
- "category": pick from [{categories}]. Use "other" only if nothing fits.
- "issuer": the entity name as printed. Normalize to a clean short form \
(e.g. "Vodafone" not "Vodafone GmbH Kundenservice").
- "subject": be specific — "march_mobile_bill" not just "bill".
- "ref_number": the most prominent identifier on the document. null if none.
- "date": the document's own date, NOT today ({today}). null if not found.
- "suggested_categories": 3-5 best matching categories ordered by relevance.
- "tags": 5-15 lowercase keywords drawn from ACTUAL content: issuer name, \
document type, product/model names, people names, topics, locations. \
Exclude generic words like "document" or "paper".
- "risk_level": "high" for scams/phishing, "medium" for hidden fees or \
unfair terms, "low" for minor issues, "none" if clean.
- "risks": [] if clean, else short strings: scams, misleading terms, \
hidden fees, unfair clauses, inconsistencies, phishing signals.

EXAMPLE — a Vodafone mobile invoice:
{{
  "category": "invoice",
  "issuer": "vodafone",
  "subject": "mobile_bill_march",
  "ref_number": "INV-2024-88431",
  "summary": "Vodafone monthly mobile invoice for March 2024, CHF 89.00",
  "date": "2024-03-15",
  "key_fields": {{
    "amount": "CHF 89.00",
    "recipient": "Nate Barbey",
    "account_number": "VF-CH-4821"
  }},
  "suggested_categories": ["utilities", "subscription", "receipt"],
  "tags": ["vodafone", "invoice", "mobile", "march", "chf_89", "telecom"],
  "risk_level": "none",
  "risks": []
}}

EXAMPLE — a BMW sales contract:
{{
  "category": "contract",
  "issuer": "bmw_morges",
  "subject": "x3_30e_purchase",
  "ref_number": "KV-2025-1192",
  "summary": "Purchase contract for BMW X3 30e xDrive from BMW Morges",
  "date": "2025-02-20",
  "key_fields": {{
    "amount": "CHF 72,400.00",
    "recipient": "Nate Barbey",
    "vin": "WBA123456789",
    "model": "BMW X3 30e xDrive"
  }},
  "suggested_categories": ["automobile", "legal", "receipt"],
  "tags": ["bmw", "contract", "x3_30e", "purchase", "morges", "automobile"],
  "risk_level": "none",
  "risks": []
}}"""


# ── Batch page grouping (step 1 of 2-step pipeline) ──────────────
# Used by analyze_batch(). The model's ONLY job here is to figure out
# which pages belong together. Classification happens in step 2 by
# reusing ANALYSIS_PROMPT per group. Keeping grouping separate reduces
# cognitive load and improves accuracy on both tasks.

BATCH_GROUPING_PROMPT = """\
You receive {num_pages} scanned pages from a mixed stack. Pages from \
DIFFERENT documents are shuffled together. Each image is labeled with \
its page number (1–{num_pages}).

Your ONLY job: figure out which pages belong to the SAME document.
Do NOT classify or extract data — just group the pages.

═══ READ EVERY PAGE ═══
For each page, note:
  a) Document TYPE visible on the page (invoice, letter, policy, form…)
  b) Header / letterhead / logo
  c) Reference numbers (invoice #, policy #, contract #, account #)
  d) Sender and recipient
  e) Page numbering ("Page 2 of 4", "2/4", footers)
  f) Language, formatting, visual style
  g) What the page is actually about

═══ GROUPING RULES ═══
Two pages belong to the SAME document ONLY when ALL match:
  ✓ Same document TYPE
  ✓ Same reference / policy / contract / invoice number
  ✓ Same sender AND recipient
  ✓ Compatible page numbering (no duplicate page numbers)
  ✓ Same formatting, letterhead, visual style
  ✓ Content continues from one page to the next

These are ALWAYS separate documents:
  ✗ Different document types (insurance policy ≠ sales contract)
  ✗ A cover letter and the enclosed form → 2 docs
  ✗ A receipt and the invoice for the same purchase → 2 docs
  ✗ Different headers or letterheads → separate
  ✗ A renewal notice and the original policy → 2 docs
  ✗ Documents in different languages → likely separate
  ✗ A terms & conditions insert and the main document → 2 docs

WHEN IN DOUBT → SPLIT. Over-splitting is always safer than wrong merges.

═══ OUTPUT ═══
Return ONLY valid JSON (no markdown) — an array of page groups:

[
  {{
    "pages": [1, 3],
    "confidence": 95,
    "page_confidence": {{"1": 95, "3": 75}},
    "reasoning": "Same AXA letterhead, policy #AX-4821 on both, page 3 is Page 2 of 2"
  }},
  {{
    "pages": [2],
    "confidence": 98,
    "page_confidence": {{"2": 98}},
    "reasoning": "Standalone Vodafone invoice with unique INV-88431"
  }}
]

CONFIDENCE (0–100, per-page and per-group):
  90–100: Strong evidence — matching reference numbers, explicit page numbering, \
continuous content
  70–89: Likely correct — similar style and content, but no definitive shared identifier
  50–69: Uncertain — weak evidence, could plausibly belong elsewhere
  Below 50: Very weak — little evidence for this grouping

"reasoning": one sentence explaining WHY these pages are grouped (or why a \
single page is standalone). Cite specific evidence: reference numbers, \
headers, page numbering.

Every page (1–{num_pages}) must appear in exactly one group. No page may be \
omitted or duplicated."""


# ── Verification (second pass with extended thinking) ─────────────
# Used when batch confidence is below 90%. Sent to Opus with
# extended thinking enabled for deeper reasoning about page grouping.

VERIFY_PROMPT = """\
You are a document verification expert performing a THOROUGH second-pass review.

A first-pass model sorted {num_pages} scanned pages into document groups but was uncertain about {num_uncertain} page(s): {uncertain_list}.

INITIAL GROUPING:
{initial_grouping}

You are seeing ALL {num_pages} pages so you can compare every detail. For each UNCERTAIN page listed above, perform a deep analysis:

═══ STEP 1: READ EVERY DETAIL ON THE UNCERTAIN PAGE ═══
  - Full header/letterhead text, logo, and colors
  - All reference numbers (invoice #, policy #, contract #, account #, claim #)
  - Sender and recipient names, addresses
  - Page numbering ("Page 2 of 4", "2/4", sequential footers)
  - Document type and subject matter
  - Language, formatting, paper style, stamps, signatures
  - Dates, amounts, key identifiers

═══ STEP 2: COMPARE WITH EVERY OTHER PAGE ═══
For each uncertain page, compare it against ALL other pages in the batch:
  - Does the header/letterhead EXACTLY match any other page?
  - Do reference numbers match? (Same policy # = same document)
  - Is the content a continuation? (Page 2 picks up where page 1 left off)
  - Is the page numbering compatible? (Two "page 1" pages = different documents)
  - Same sender AND same recipient AND same document type?

═══ STEP 3: DECIDE ═══
  - KEEP in current group: strong evidence it belongs (matching ref #, continuous content)
  - MOVE to another group: clearly matches a different document (same header/ref #)
  - NEW document: doesn't belong to any existing group

Return ONLY a valid JSON array of reassignments (empty [] if all assignments are correct):
[{{"page": 3, "move_to_doc": 1, "reason": "matching policy number AX-4821 and identical AXA letterhead"}}]

"move_to_doc" is the 1-based document number from the initial grouping, or "new" to create a separate document.
Only include pages that need to be MOVED. Omit pages whose assignment is correct."""


# ── Risk analysis (optional separate pass) ────────────────────────
# Run AFTER classification, only when the user opts in or the
# category warrants it (contracts, invoices, insurance, legal).
# Keeping this separate avoids inflating token cost on every document.

RISK_PROMPT = """\
You are a consumer protection analyst. Review this document for anything \
the recipient should be aware of.

Document type: {category}
Summary: {summary}

Analyze for:
  - SCAM SIGNALS: phishing language, fake urgency, suspicious sender, \
mismatched branding, requests for unusual payment methods
  - HIDDEN COSTS: fees buried in fine print, auto-renewal traps, \
escalation clauses, penalty fees
  - UNFAIR TERMS: one-sided cancellation, liability waivers, \
unreasonable notice periods, binding arbitration
  - INCONSISTENCIES: amounts that don't add up, dates that conflict, \
terms that contradict each other
  - MISSING ELEMENTS: unsigned where signature expected, missing dates, \
incomplete fields

Return ONLY valid JSON (no markdown):

{{
  "risk_level": "none"|"low"|"medium"|"high",
  "findings": [
    {{
      "type": "hidden_fee"|"scam_signal"|"unfair_term"|"inconsistency"|"missing_element",
      "severity": "info"|"warning"|"critical",
      "description": "<what you found>",
      "location": "<where in the document>"
    }}
  ],
  "recommendation": "<one sentence: what should the recipient do?>"
}}

If the document is clean, return:
{{"risk_level": "none", "findings": [], "recommendation": "No issues found."}}"""


# ── System messages ───────────────────────────────────────────────
# Short system prompts that set the model's output format.

SYSTEM_SINGLE = (
    "You are a document classification API. You ONLY output valid JSON objects. "
    "Never output explanations, thinking, or markdown — ONLY the JSON object."
)

SYSTEM_BATCH_GROUPING = (
    "You are a document page-grouping API. "
    "Your job is to determine which scanned pages belong to the same document. "
    "Output ONLY a valid JSON array of page groups. "
    "No classification, no data extraction — just grouping. "
    "No explanations, no markdown fences, no preamble."
)

SYSTEM_VERIFY = (
    "You are a document verification expert. Think carefully through "
    "each uncertain page, comparing it against all other pages. "
    "After thinking, output ONLY a valid JSON array — no explanations."
)

SYSTEM_RISK = (
    "You are a consumer protection analyst. "
    "Output ONLY a valid JSON object. "
    "No explanations, no markdown fences, no preamble."
)
