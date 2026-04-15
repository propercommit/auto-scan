"""Model instructions for document recognition.

This file contains ONLY the text sent to the AI — no code logic.
Edit these prompts to change how the model classifies, names, and
groups documents. The engine (engine.py) reads from here.

Placeholders filled at runtime:
    {today}        — current date (YYYY-MM-DD)
    {categories}   — comma-separated category list
    {num_pages}    — number of scanned pages (batch/verify)
    {num_uncertain}, {uncertain_list}, {initial_grouping} — verify only

Pipeline:
    1. ANALYSIS_PROMPT        — single document (1–20 pages) → classify + extract
    2. BATCH_ANALYSIS_PROMPT  — mixed stack → group pages + classify each group
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


# ── Batch sorting & classification ────────────────────────────────
# Used by analyze_batch() when the feeder scans a mixed stack.
# The model must group pages into documents AND classify each group.

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
For each document group, determine the best category, extract structured fields, and provide key information.

Return ONLY valid JSON (no markdown) — an array of document groups:

[{{"pages": [1, 2], "page_confidence": {{"1": 95, "2": 80}}, "confidence": 87, "category": "...", "issuer": "...", "subject": "...", "ref_number": "...", "summary": "...", "date": "YYYY-MM-DD or null", "key_fields": {{}}, "suggested_categories": [], "tags": [], "risk_level": "none"|"low"|"medium"|"high", "risks": []}}]

FIELD RULES:
  - "issuer": the entity name as printed, normalized to a clean short form \
(e.g. "Vodafone" not "Vodafone GmbH Kundenservice"). This is used to build the filename.
  - "subject": brief specific subject — "march_mobile_bill" not just "bill". \
This is used to build the filename.
  - "ref_number": the most prominent reference/invoice/policy number, or null.

CONFIDENCE SCORING (0–100):
  - "page_confidence": for EACH page, how certain you are it belongs to THIS document group
  - "confidence": overall confidence for the entire document grouping
  - 90–100: Very clear — same header, reference number, continuous content
  - 70–89: Likely — similar style and content, minor ambiguity
  - 50–69: Uncertain — could plausibly belong to another group
  - Below 50: Weak — little evidence for this grouping

Categories: {categories}
If none of these categories fits well, use "other" and make the subject descriptive.

Pages numbered 1–{num_pages}. Every page must appear in exactly one group. Use the document date if visible, otherwise {today}. Tags: 5-15 lowercase keywords from actual content (entity names, product names, topics, amounts, locations — be specific, not generic)."""


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

SYSTEM_BATCH = (
    "You are a document classification API. You ONLY output valid JSON arrays. "
    "Never output explanations, thinking, or markdown — ONLY the JSON array. "
    "Read the text on every page to classify correctly. "
    "A page with 'Insurance' in the header is insurance, not sales. "
    "Split different document types into separate groups."
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
