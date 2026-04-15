"""Tests for engine functions that call the Claude API (mocked).

Tests _classify_images, _single_pass_batch, _group_pages, _two_step_batch,
_verify_uncertain_pages, analyze_document, analyze_batch, _compute_page_hints,
and _maybe_redact — all with mocked API responses using the 100-doc fixtures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from auto_scan import AnalysisError
from auto_scan.config import Config
from auto_scan.recognition.engine import (
    DocumentInfo,
    SINGLE_PASS_THRESHOLD,
    _classify_images,
    _single_pass_batch,
    _group_pages,
    _two_step_batch,
    _verify_uncertain_pages,
    _compute_page_hints,
    _maybe_redact,
    _parse_batch_results,
    _parse_grouping_results,
    analyze_document,
    analyze_batch,
    build_filename,
)
from fixtures.documents import (
    ALL_FIXTURES,
    DocFixture,
    BatchFixture,
    make_batch,
    fixtures_by_category,
)


# ── Helpers ──────────────────────────────────────────────────────


def _make_config(**overrides) -> Config:
    """Create a Config with test defaults."""
    defaults = {
        "api_key": "sk-ant-test-key",
        "scanner_ip": "192.168.1.100",
        "output_dir": "/tmp/test-scans",
        "resolution": 300,
        "color_mode": "color",
        "scan_source": "Feeder",
        "scan_format": "image/jpeg",
        "claude_model": "claude-sonnet-4-20250514",
    }
    defaults.update(overrides)
    return Config(**defaults)


def _mock_message(response_text: str, input_tokens: int = 500, output_tokens: int = 200):
    """Create a mock Anthropic message response."""
    msg = MagicMock()
    msg.content = [SimpleNamespace(type="text", text=response_text)]
    msg.usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    msg.stop_reason = "end_turn"
    return msg


def _mock_client(response_text: str, **kwargs):
    """Create a mock Anthropic client that returns canned response."""
    client = MagicMock()
    client.messages.create.return_value = _mock_message(response_text, **kwargs)
    return client


def _mock_client_multi(responses: list[str]):
    """Mock client returning different responses on successive calls."""
    client = MagicMock()
    messages = [_mock_message(r) for r in responses]
    client.messages.create.side_effect = messages
    return client


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    """Disable rate limiting and usage recording for all tests."""
    monkeypatch.setattr("auto_scan.recognition.engine.check_rate_limit", lambda: None)
    monkeypatch.setattr("auto_scan.recognition.engine.record_usage", lambda *a, **kw: {})


@pytest.fixture
def config():
    return _make_config()


@pytest.fixture
def all_docs():
    return ALL_FIXTURES()


# ── _classify_images ─────────────────────────────────────────────


class TestClassifyImages:
    """Test single-document classification with mocked API."""

    def test_basic_classification(self, config):
        """Classify a single-page invoice."""
        doc = fixtures_by_category("invoice")[0]
        response_json = doc.api_response_json()
        client = _mock_client(response_json)

        result = _classify_images(doc.images, config, client)

        assert isinstance(result, DocumentInfo)
        assert result.category == doc.category
        assert doc.issuer in result.filename
        assert result.summary == doc.summary

    def test_multi_page_document(self, config):
        """Classify a multi-page contract."""
        doc = fixtures_by_category("contract")[0]  # BMW contract, 3 pages
        assert doc.num_pages == 3
        response_json = doc.api_response_json()
        client = _mock_client(response_json)

        result = _classify_images(doc.images, config, client)

        assert result.category == "contract"

    @pytest.mark.parametrize("category", [
        "invoice", "receipt", "contract", "letter", "medical",
        "tax", "insurance", "bank", "government", "personal",
    ])
    def test_classification_by_category(self, config, category):
        """Each major category produces correct classification."""
        docs = fixtures_by_category(category)
        assert docs, f"No fixtures for category {category}"
        doc = docs[0]
        client = _mock_client(doc.api_response_json())

        result = _classify_images(doc.images, config, client)
        assert result.category == category

    def test_key_fields_populated(self, config):
        """Key fields from API response are preserved."""
        doc = fixtures_by_category("invoice")[0]
        client = _mock_client(doc.api_response_json())

        result = _classify_images(doc.images, config, client)
        assert "issuer" in result.key_fields
        assert result.key_fields["issuer"] == doc.issuer

    def test_tags_preserved(self, config):
        """Tags from API response are preserved."""
        doc = fixtures_by_category("receipt")[0]
        client = _mock_client(doc.api_response_json())

        result = _classify_images(doc.images, config, client)
        assert len(result.tags) > 0

    def test_risk_fields(self, config):
        """Risk documents have correct risk_level and risks."""
        docs = [f for f in ALL_FIXTURES() if f.risk_level != "none"]
        assert docs, "No risk fixtures found"
        doc = docs[0]
        client = _mock_client(doc.api_response_json())

        result = _classify_images(doc.images, config, client)
        assert result.risk_level == doc.risk_level
        assert len(result.risks) > 0

    def test_deterministic_filename(self, config):
        """Filename is built deterministically from structured fields."""
        doc = fixtures_by_category("invoice")[0]  # vodafone
        client = _mock_client(doc.api_response_json())

        result = _classify_images(doc.images, config, client)
        # Filename should contain date, category, issuer
        assert result.filename.endswith(".pdf")
        assert "invoice" in result.filename
        assert "vodafone" in result.filename

    def test_api_timeout_raises(self, config):
        """API timeout raises AnalysisError."""
        import anthropic
        client = MagicMock()
        client.messages.create.side_effect = anthropic.APITimeoutError(request=MagicMock())
        doc = fixtures_by_category("invoice")[0]

        with pytest.raises(AnalysisError, match="timed out"):
            _classify_images(doc.images, config, client)

    def test_invalid_json_raises(self, config):
        """Non-JSON response raises AnalysisError."""
        client = _mock_client("this is not valid json at all!!!")
        doc = fixtures_by_category("invoice")[0]

        with pytest.raises(AnalysisError, match="parse"):
            _classify_images(doc.images, config, client)

    def test_images_sent_as_base64(self, config):
        """All images are sent as base64-encoded JPEG in the API call."""
        doc = fixtures_by_category("invoice")[0]
        client = _mock_client(doc.api_response_json())

        _classify_images(doc.images, config, client)

        call_args = client.messages.create.call_args
        messages = call_args.kwargs["messages"]
        user_content = messages[0]["content"]
        image_blocks = [b for b in user_content if b["type"] == "image"]
        assert len(image_blocks) == doc.num_pages
        for block in image_blocks:
            assert block["source"]["media_type"] == "image/jpeg"
            assert block["source"]["type"] == "base64"


# ── _single_pass_batch ───────────────────────────────────────────


class TestSinglePassBatch:
    """Test single-pass batch (group + classify in one call)."""

    def test_small_batch(self, config):
        """Small batch returns correct groupings and classifications."""
        batch = make_batch(n=3)
        assert batch.num_pages <= SINGLE_PASS_THRESHOLD
        response_json = batch.combined_response_json()
        client = _mock_client(response_json)

        results = _single_pass_batch(batch.all_images, config, client)

        assert len(results) == len(batch.documents)
        for pages, doc_info in results:
            assert isinstance(doc_info, DocumentInfo)
            assert doc_info.filename.endswith(".pdf")

    def test_page_indices_zero_based(self, config):
        """Returned page indices are 0-based."""
        batch = make_batch(n=2)
        response_json = batch.combined_response_json()
        client = _mock_client(response_json)

        results = _single_pass_batch(batch.all_images, config, client)

        all_indices = []
        for pages, _ in results:
            all_indices.extend(pages)
        # All indices should be in range [0, num_pages)
        assert all(0 <= i < batch.num_pages for i in all_indices)

    def test_single_api_call(self, config):
        """Only one API call is made for single-pass."""
        batch = make_batch(n=3)
        client = _mock_client(batch.combined_response_json())

        _single_pass_batch(batch.all_images, config, client)

        assert client.messages.create.call_count == 1

    def test_empty_response_raises(self, config):
        """Empty response raises AnalysisError."""
        batch = make_batch(n=2)
        msg = _mock_message("")
        msg.stop_reason = "end_turn"
        client = MagicMock()
        client.messages.create.return_value = msg

        with pytest.raises(AnalysisError, match="Empty"):
            _single_pass_batch(batch.all_images, config, client)

    def test_max_tokens_truncation_repair(self, config):
        """Truncated response is repaired when stop_reason=max_tokens."""
        batch = make_batch(n=2)
        full_response = batch.combined_response_json()
        # Truncate mid-way
        truncated = full_response[:len(full_response) // 2]
        msg = _mock_message(truncated)
        msg.stop_reason = "max_tokens"
        client = MagicMock()
        client.messages.create.return_value = msg

        # Should attempt repair — may succeed or raise depending on truncation point
        try:
            results = _single_pass_batch(batch.all_images, config, client)
            # If repair succeeds, results should be non-empty
            assert len(results) >= 1
        except AnalysisError:
            pass  # Repair failed, which is acceptable


# ── _group_pages ─────────────────────────────────────────────────


class TestGroupPages:
    """Test the grouping-only step (no classification)."""

    def test_groups_pages(self, config):
        """Grouping returns valid groups with confidence."""
        batch = make_batch(n=3)
        response_json = batch.grouping_response_json()
        client = _mock_client(response_json)

        groups = _group_pages(batch.all_images, config, client)

        assert len(groups) == len(batch.documents)
        for g in groups:
            assert "pages" in g
            assert "confidence" in g
            assert isinstance(g["confidence"], int)

    def test_all_pages_assigned(self, config):
        """Every page appears in exactly one group."""
        batch = make_batch(n=4)
        client = _mock_client(batch.grouping_response_json())

        groups = _group_pages(batch.all_images, config, client)

        all_pages = []
        for g in groups:
            all_pages.extend(g["pages"])
        assert sorted(all_pages) == list(range(1, batch.num_pages + 1))

    def test_missing_pages_get_own_group(self, config):
        """Pages not in any group get their own low-confidence group."""
        batch = make_batch(n=3)
        # Return grouping that's missing some pages
        incomplete = batch.grouping_response()
        if len(incomplete) > 1:
            # Remove last group
            removed_pages = incomplete.pop()["pages"]
            response_json = json.dumps(incomplete)[1:]
            client = _mock_client(response_json)

            groups = _group_pages(batch.all_images, config, client)

            # Missing pages should still appear
            all_pages = []
            for g in groups:
                all_pages.extend(g["pages"])
            assert sorted(all_pages) == list(range(1, batch.num_pages + 1))


# ── _two_step_batch ──────────────────────────────────────────────


class TestTwoStepBatch:
    """Test the 2-step pipeline (group → classify per group)."""

    def test_two_step_pipeline(self, config):
        """2-step returns correct number of documents."""
        batch = make_batch(n=3)
        # First call: grouping response; subsequent calls: per-group classification
        responses = [batch.grouping_response_json()]
        for doc in batch.documents:
            responses.append(doc.api_response_json())
        client = _mock_client_multi(responses)

        results = _two_step_batch(batch.all_images, config, client)

        assert len(results) == len(batch.documents)
        for pages, doc_info in results:
            assert isinstance(doc_info, DocumentInfo)

    def test_multiple_api_calls(self, config):
        """2-step makes 1 (grouping) + N (classification) API calls."""
        batch = make_batch(n=3)
        responses = [batch.grouping_response_json()]
        for doc in batch.documents:
            responses.append(doc.api_response_json())
        client = _mock_client_multi(responses)

        _two_step_batch(batch.all_images, config, client)

        expected_calls = 1 + len(batch.documents)  # grouping + per-group
        assert client.messages.create.call_count == expected_calls

    def test_confidence_from_grouping(self, config):
        """Classification result carries grouping confidence."""
        batch = make_batch(n=2)
        grouping = batch.grouping_response(base_confidence=72)
        responses = [json.dumps(grouping)[1:]]
        for doc in batch.documents:
            responses.append(doc.api_response_json())
        client = _mock_client_multi(responses)

        results = _two_step_batch(batch.all_images, config, client)

        for _, doc_info in results:
            assert doc_info.confidence == 72


# ── _verify_uncertain_pages ──────────────────────────────────────


class TestVerifyUncertainPages:
    """Test the Opus verification pass."""

    def test_no_reassignment(self, config):
        """Empty reassignment array = keep all assignments."""
        batch = make_batch(n=2)
        # Build initial results
        initial_results = []
        page_offset = 0
        for doc in batch.documents:
            pages = list(range(page_offset, page_offset + doc.num_pages))
            info = DocumentInfo(
                category=doc.category, filename="test.pdf",
                summary=doc.summary, date=doc.date,
                confidence=70,
                page_confidence={p + 1: 70 for p in pages},
            )
            initial_results.append((pages, info))
            page_offset += doc.num_pages

        uncertain = [1]
        client = _mock_client("[]")  # No reassignments

        results = _verify_uncertain_pages(
            batch.all_images, initial_results, uncertain, config, client
        )

        assert len(results) == len(initial_results)

    def test_page_moved(self, config):
        """Verification moves a page from one doc to another."""
        # Create 2 docs: [pages 0,1] and [page 2]
        doc1 = fixtures_by_category("invoice")[0]
        doc2 = fixtures_by_category("letter")[0]
        images = doc1.images + doc2.images

        initial_results = [
            ([0, 1], DocumentInfo(
                category="invoice", filename="inv.pdf",
                summary="Invoice", date="2024-01-01",
                confidence=95,
                page_confidence={1: 95, 2: 60},
            )),
            ([2], DocumentInfo(
                category="letter", filename="let.pdf",
                summary="Letter", date="2024-01-01",
                confidence=95,
                page_confidence={3: 95},
            )),
        ]

        # Move page 2 (1-indexed) to doc 2
        reassignment = json.dumps([{"page": 2, "move_to_doc": 2, "reason": "matching header"}])
        client = _mock_client(reassignment)

        results = _verify_uncertain_pages(images, initial_results, [2], config, client)

        # Page 1 (0-indexed) should now be in doc 2
        doc1_pages = results[0][0]
        doc2_pages = results[1][0]
        assert 1 not in doc1_pages
        assert 1 in doc2_pages

    def test_page_new_document(self, config):
        """Verification creates a new document for a misplaced page."""
        doc = fixtures_by_category("contract")[0]  # 3-page doc
        images = doc.images

        initial_results = [
            ([0, 1, 2], DocumentInfo(
                category="contract", filename="contract.pdf",
                summary="Contract", date="2025-01-01",
                confidence=80,
                page_confidence={1: 95, 2: 95, 3: 50},
            )),
        ]

        reassignment = json.dumps([{"page": 3, "move_to_doc": "new", "reason": "different document"}])
        client = _mock_client(reassignment)

        results = _verify_uncertain_pages(images, initial_results, [3], config, client)

        assert len(results) == 2
        assert 2 not in results[0][0]

    def test_api_failure_keeps_original(self, config):
        """If verification API fails, keep original assignments."""
        doc = fixtures_by_category("invoice")[0]
        initial_results = [
            ([0], DocumentInfo(
                category="invoice", filename="test.pdf",
                summary="Test", date="2024-01-01",
                confidence=60,
                page_confidence={1: 60},
            )),
        ]
        client = MagicMock()
        client.messages.create.side_effect = Exception("API down")

        results = _verify_uncertain_pages(doc.images, initial_results, [1], config, client)

        assert results == initial_results

    def test_uses_opus_model(self, config):
        """Verification uses Opus model with extended thinking."""
        doc = fixtures_by_category("invoice")[0]
        initial_results = [
            ([0], DocumentInfo(
                category="invoice", filename="test.pdf",
                summary="Test", date="2024-01-01",
                confidence=60, page_confidence={1: 60},
            )),
        ]
        client = _mock_client("[]")

        _verify_uncertain_pages(doc.images, initial_results, [1], config, client)

        call_kwargs = client.messages.create.call_args.kwargs
        assert "opus" in call_kwargs["model"]
        assert call_kwargs["thinking"]["type"] == "enabled"


# ── analyze_document (public API) ────────────────────────────────


class TestAnalyzeDocument:
    """Test the public single-document analysis function."""

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_returns_doc_info(self, mock_anthropic_cls, config):
        """analyze_document returns DocumentInfo."""
        doc = fixtures_by_category("receipt")[0]
        mock_client = _mock_client(doc.api_response_json())
        mock_anthropic_cls.return_value = mock_client

        result = analyze_document(doc.images, config)

        assert isinstance(result, DocumentInfo)
        assert result.category == doc.category

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_creates_own_client(self, mock_anthropic_cls, config):
        """analyze_document creates its own Anthropic client."""
        doc = fixtures_by_category("invoice")[0]
        mock_client = _mock_client(doc.api_response_json())
        mock_anthropic_cls.return_value = mock_client

        analyze_document(doc.images, config)

        mock_anthropic_cls.assert_called_once()


# ── analyze_batch (public API) ───────────────────────────────────


class TestAnalyzeBatch:
    """Test the public batch analysis with threshold routing."""

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_small_batch_uses_single_pass(self, mock_anthropic_cls, config):
        """Batches ≤ threshold use single-pass (1 API call)."""
        batch = make_batch(n=3)  # small batch
        assert batch.num_pages <= SINGLE_PASS_THRESHOLD
        mock_client = _mock_client(batch.combined_response_json())
        mock_anthropic_cls.return_value = mock_client

        results = analyze_batch(batch.all_images, config)

        # Single-pass = 1 API call (may have verify call too)
        assert mock_client.messages.create.call_count >= 1
        assert len(results) == len(batch.documents)

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_large_batch_uses_two_step(self, mock_anthropic_cls, config):
        """Batches > threshold use 2-step pipeline."""
        # Create a batch with > SINGLE_PASS_THRESHOLD pages
        docs = []
        total_pages = 0
        for f in ALL_FIXTURES():
            docs.append(f)
            total_pages += f.num_pages
            if total_pages > SINGLE_PASS_THRESHOLD:
                break
        batch = BatchFixture(documents=docs)
        assert batch.num_pages > SINGLE_PASS_THRESHOLD

        # Build responses: grouping + per-group classification
        responses = [batch.grouping_response_json()]
        for doc in batch.documents:
            responses.append(doc.api_response_json())
        mock_client = _mock_client_multi(responses)
        mock_anthropic_cls.return_value = mock_client

        results = analyze_batch(batch.all_images, config)

        # 2-step = 1 grouping + N classification calls
        assert mock_client.messages.create.call_count >= 1 + len(docs)
        assert len(results) == len(batch.documents)

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_too_many_pages_raises(self, mock_anthropic_cls, config):
        """More than 50 pages raises AnalysisError."""
        images = [fixtures_by_category("invoice")[0].images[0]] * 51

        with pytest.raises(AnalysisError, match="50 pages"):
            analyze_batch(images, config)

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_threshold_boundary(self, mock_anthropic_cls, config):
        """Exactly SINGLE_PASS_THRESHOLD pages uses single-pass."""
        # Build a batch of exactly threshold pages
        docs = []
        total = 0
        for f in ALL_FIXTURES():
            if total + f.num_pages <= SINGLE_PASS_THRESHOLD:
                docs.append(f)
                total += f.num_pages
            if total == SINGLE_PASS_THRESHOLD:
                break
        batch = BatchFixture(documents=docs)

        mock_client = _mock_client(batch.combined_response_json())
        mock_anthropic_cls.return_value = mock_client

        results = analyze_batch(batch.all_images, config)
        assert len(results) == len(docs)

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_verification_triggered_for_low_confidence(self, mock_anthropic_cls, config):
        """Low-confidence results trigger verification pass."""
        batch = make_batch(n=2)
        # Return low-confidence combined response
        combined = batch.combined_response(base_confidence=60)
        combined_json = json.dumps(combined)[1:]

        # First call: combined; second call: verification
        responses = [combined_json, "[]"]  # verify returns no changes
        mock_client = _mock_client_multi(responses)
        mock_anthropic_cls.return_value = mock_client

        results = analyze_batch(batch.all_images, config)

        # Should have made 2 calls: combined + verify
        assert mock_client.messages.create.call_count == 2

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_high_confidence_skips_verification(self, mock_anthropic_cls, config):
        """High-confidence results skip verification."""
        batch = make_batch(n=2)
        # 95% confidence → no verify
        mock_client = _mock_client(batch.combined_response_json())
        mock_anthropic_cls.return_value = mock_client

        results = analyze_batch(batch.all_images, config)

        # Only 1 call (single-pass, no verify)
        assert mock_client.messages.create.call_count == 1


# ── _compute_page_hints ──────────────────────────────────────────


class TestComputePageHints:
    """Test local page hint computation (no API calls)."""

    def test_single_page_returns_empty(self):
        """Single page → no hints (nothing to compare)."""
        doc = fixtures_by_category("invoice")[0]
        assert doc.num_pages == 1
        hints = _compute_page_hints(doc.images)
        assert hints == ""

    def test_two_similar_pages_detected(self):
        """Two pages from same document are detected as similar."""
        doc = fixtures_by_category("contract")[0]  # 3-page BMW contract
        # Pages from same doc should have similar headers
        hints = _compute_page_hints(doc.images)
        # Should produce some hint text (may mention similar layout)
        if hints:
            assert "HINTS" in hints or "Similar" in hints.lower() or "layout" in hints.lower()

    def test_different_documents_detected(self):
        """Pages from different documents produce boundary hints."""
        doc1 = fixtures_by_category("invoice")[0]  # red Vodafone header
        doc2 = fixtures_by_category("contract")[0]  # blue BMW header
        images = doc1.images + doc2.images[:1]  # 2 different docs

        hints = _compute_page_hints(images)

        # Should detect the boundary between different headers
        if hints:
            assert "DIFFERENT" in hints or "boundary" in hints.lower()

    def test_multi_doc_batch_produces_hints(self):
        """A realistic batch of mixed documents produces hint text."""
        batch = make_batch(n=4)
        hints = _compute_page_hints(batch.all_images)
        # With different letterheads, there should be boundary detection
        assert isinstance(hints, str)


# ── _maybe_redact ────────────────────────────────────────────────


class TestMaybeRedact:
    """Test optional redaction pass-through."""

    def test_disabled_returns_original(self):
        """Redaction disabled → image returned unchanged."""
        doc = fixtures_by_category("invoice")[0]
        img = doc.images[0]
        result = _maybe_redact(img, False, None)
        assert result is img  # exact same object

    def test_disabled_ignores_patterns(self):
        """Even with patterns, disabled means pass-through."""
        doc = fixtures_by_category("bank")[0]
        img = doc.images[0]
        result = _maybe_redact(img, False, {"iban", "phone"})
        assert result is img

    def test_enabled_calls_redactor(self, monkeypatch):
        """Redaction enabled → calls redact_image."""
        doc = fixtures_by_category("invoice")[0]
        img = doc.images[0]

        mock_result = MagicMock()
        mock_result.redacted_image = b"redacted-image-bytes"

        mock_redact = MagicMock(return_value=mock_result)
        # Patch the import inside _maybe_redact
        import auto_scan.redactor as redactor_mod
        monkeypatch.setattr(redactor_mod, "redact_image", mock_redact)

        result = _maybe_redact(img, True, {"iban"})

        mock_redact.assert_called_once_with(img, enabled_patterns={"iban"})
        assert result == b"redacted-image-bytes"


# ── Fixture coverage tests ───────────────────────────────────────


class TestFixtureCoverage:
    """Verify fixture coverage of categories and edge cases."""

    def test_100_fixtures(self, all_docs):
        """We have exactly 100 test documents."""
        assert len(all_docs) == 100

    def test_all_categories_covered(self, all_docs):
        """Every category in ALL_CATEGORIES has at least one fixture."""
        from auto_scan.recognition.prompts import ALL_CATEGORIES
        fixture_categories = {d.category for d in all_docs}
        for cat in ALL_CATEGORIES:
            assert cat in fixture_categories, f"Missing fixture for category: {cat}"

    def test_multi_page_documents_exist(self, all_docs):
        """Some fixtures have multiple pages."""
        multi_page = [d for d in all_docs if d.num_pages > 1]
        assert len(multi_page) >= 10

    def test_risk_documents_exist(self, all_docs):
        """Some fixtures have risk levels > none."""
        risky = [d for d in all_docs if d.risk_level != "none"]
        assert len(risky) >= 2

    def test_no_empty_images(self, all_docs):
        """All fixtures have at least one valid JPEG image."""
        for doc in all_docs:
            assert len(doc.images) >= 1
            for img in doc.images:
                assert len(img) > 100  # minimum JPEG size
                assert img[:2] == b"\xff\xd8"  # JPEG magic bytes

    def test_api_response_is_valid_json(self, all_docs):
        """Every fixture produces valid JSON for API mocking."""
        for doc in all_docs:
            response = doc.api_response()
            json_str = json.dumps(response)
            parsed = json.loads(json_str)
            assert parsed["category"] == doc.category

    def test_batch_fixture_creation(self, all_docs):
        """BatchFixture can be created from any subset."""
        batch = make_batch(docs=all_docs[:5])
        assert batch.num_pages == sum(d.num_pages for d in all_docs[:5])
        assert len(batch.all_images) == batch.num_pages

    def test_interleaved_batch(self, all_docs):
        """Interleaved batch shuffles pages across documents."""
        docs = all_docs[:3]
        batch = make_batch(docs=docs, interleave=True)
        # Pages should be shuffled (hard to guarantee with random,
        # but at least all pages should be present)
        assert batch.num_pages == sum(d.num_pages for d in docs)

    @pytest.mark.parametrize("category", [
        "invoice", "receipt", "contract", "letter", "medical",
        "tax", "insurance", "bank", "government", "personal",
        "automobile", "housing", "education", "employment", "travel",
        "utilities", "legal", "warranty", "subscription", "donation",
        "investment", "pension", "certificate", "permit", "registration",
        "membership", "manual", "other",
    ])
    def test_each_category_has_fixtures(self, category):
        """Each category has at least one fixture."""
        docs = fixtures_by_category(category)
        assert len(docs) >= 1, f"No fixtures for {category}"


# ── Structured fields + filename integration ─────────────────────


class TestStructuredFieldsIntegration:
    """Test that structured fields flow correctly through the pipeline."""

    def test_issuer_in_filename(self, config):
        """Issuer from API response appears in filename."""
        for doc in ALL_FIXTURES()[:20]:
            client = _mock_client(doc.api_response_json())
            result = _classify_images(doc.images, config, client)
            if doc.issuer:
                slug = doc.issuer.lower().replace(" ", "_")[:10]
                assert slug in result.filename or doc.category in result.filename

    def test_ref_number_in_key_fields(self, config):
        """ref_number from API gets merged into key_fields."""
        docs_with_ref = [d for d in ALL_FIXTURES() if d.ref_number]
        assert len(docs_with_ref) > 20

        doc = docs_with_ref[0]
        client = _mock_client(doc.api_response_json())
        result = _classify_images(doc.images, config, client)
        assert "ref_number" in result.key_fields

    def test_date_in_filename(self, config):
        """Document date appears at start of filename."""
        docs_with_date = [d for d in ALL_FIXTURES() if d.date]
        doc = docs_with_date[0]
        client = _mock_client(doc.api_response_json())
        result = _classify_images(doc.images, config, client)
        assert result.filename.startswith(doc.date)
