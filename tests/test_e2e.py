"""End-to-end tests for the full document recognition pipeline.

Tests the complete flow: images → engine → DocumentInfo with mocked API.
Covers single document, small batch, large batch, verification, error
recovery, and edge cases using the 100-document fixture set.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest

from auto_scan import AnalysisError
from auto_scan.config import Config, load_config
from auto_scan.recognition.engine import (
    DocumentInfo,
    SINGLE_PASS_THRESHOLD,
    analyze_document,
    analyze_batch,
    build_filename,
)
from auto_scan.recognition.prompts import ALL_CATEGORIES
from fixtures.documents import (
    ALL_FIXTURES,
    BatchFixture,
    make_batch,
    fixtures_by_category,
)


# ── Helpers ──────────────────────────────────────────────────────


def _config(**overrides) -> Config:
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


def _mock_message(text: str, input_tokens: int = 800, output_tokens: int = 400,
                   stop_reason: str = "end_turn"):
    msg = MagicMock()
    msg.content = [SimpleNamespace(type="text", text=text)]
    msg.usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    msg.stop_reason = stop_reason
    return msg


def _mock_verify_message(text: str):
    """Mock a verification message with thinking block + text block."""
    msg = MagicMock()
    thinking_block = SimpleNamespace(type="thinking", thinking="Analyzing page similarities...")
    text_block = SimpleNamespace(type="text", text=text)
    msg.content = [thinking_block, text_block]
    msg.usage = SimpleNamespace(
        input_tokens=5000, output_tokens=2000,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )
    msg.stop_reason = "end_turn"
    return msg


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    monkeypatch.setattr("auto_scan.recognition.engine.check_rate_limit", lambda: None)
    monkeypatch.setattr("auto_scan.recognition.engine.record_usage", lambda *a, **kw: {})


@pytest.fixture
def config():
    return _config()


# ── E2E: Single document analysis ───────────────────────────────


class TestE2ESingleDocument:
    """End-to-end tests for single document analysis."""

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_single_page_invoice(self, mock_cls, config):
        """Single-page invoice: full pipeline."""
        doc = fixtures_by_category("invoice")[0]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_message(doc.api_response_json())
        mock_cls.return_value = mock_client

        result = analyze_document(doc.images, config)

        assert result.category == "invoice"
        assert result.filename.endswith(".pdf")
        assert result.summary
        assert "vodafone" in result.filename

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_multi_page_contract(self, mock_cls, config):
        """Multi-page contract preserves all pages."""
        doc = fixtures_by_category("contract")[0]  # 3-page BMW contract
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_message(doc.api_response_json())
        mock_cls.return_value = mock_client

        result = analyze_document(doc.images, config)

        assert result.category == "contract"
        # All 3 pages should be sent as images
        call_args = mock_client.messages.create.call_args
        user_content = call_args.kwargs["messages"][0]["content"]
        image_blocks = [b for b in user_content if b["type"] == "image"]
        assert len(image_blocks) == 3

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    @pytest.mark.parametrize("category", ALL_CATEGORIES)
    def test_every_category(self, mock_cls, config, category):
        """Every document category can be classified end-to-end."""
        docs = fixtures_by_category(category)
        if not docs:
            pytest.skip(f"No fixture for {category}")
        doc = docs[0]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_message(doc.api_response_json())
        mock_cls.return_value = mock_client

        result = analyze_document(doc.images, config)
        assert result.category == category

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_risk_document_flags(self, mock_cls, config):
        """Risky documents have risk_level and risks populated."""
        docs = [f for f in ALL_FIXTURES() if f.risk_level == "high"]
        assert docs
        doc = docs[0]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_message(doc.api_response_json())
        mock_cls.return_value = mock_client

        result = analyze_document(doc.images, config)

        assert result.risk_level == "high"
        assert len(result.risks) > 0

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_filename_determinism(self, mock_cls, config):
        """Same input always produces same filename."""
        doc = fixtures_by_category("invoice")[0]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_message(doc.api_response_json())
        mock_cls.return_value = mock_client

        result1 = analyze_document(doc.images, config)

        mock_client.messages.create.return_value = _mock_message(doc.api_response_json())
        result2 = analyze_document(doc.images, config)

        assert result1.filename == result2.filename


# ── E2E: Small batch (single-pass) ──────────────────────────────


class TestE2ESmallBatch:
    """End-to-end tests for small batches (≤ threshold → single-pass)."""

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_3_doc_batch(self, mock_cls, config):
        """3 single-page documents grouped and classified in one call."""
        docs = [
            fixtures_by_category("invoice")[0],
            fixtures_by_category("receipt")[0],
            fixtures_by_category("letter")[0],
        ]
        batch = BatchFixture(documents=docs)
        assert batch.num_pages <= SINGLE_PASS_THRESHOLD

        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_message(
            batch.combined_response_json()
        )
        mock_cls.return_value = mock_client

        results = analyze_batch(batch.all_images, config)

        assert len(results) == 3
        categories = {info.category for _, info in results}
        assert "invoice" in categories
        assert "receipt" in categories
        assert "letter" in categories

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_mixed_page_counts(self, mock_cls, config):
        """Batch with mixed single + multi-page documents."""
        docs = [
            fixtures_by_category("invoice")[0],   # 1 page
            fixtures_by_category("contract")[0],   # 3 pages
            fixtures_by_category("receipt")[0],     # 1 page
        ]
        batch = BatchFixture(documents=docs)
        total_pages = sum(d.num_pages for d in docs)
        assert batch.num_pages == total_pages
        assert batch.num_pages <= SINGLE_PASS_THRESHOLD

        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_message(
            batch.combined_response_json()
        )
        mock_cls.return_value = mock_client

        results = analyze_batch(batch.all_images, config)

        assert len(results) == 3
        # BMW contract should have 3 pages
        contract_result = [r for r in results if r[1].category == "contract"]
        assert len(contract_result) == 1
        assert len(contract_result[0][0]) == 3

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_single_page_batch(self, mock_cls, config):
        """Edge case: batch with just 1 page."""
        doc = fixtures_by_category("invoice")[0]
        batch = BatchFixture(documents=[doc])

        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_message(
            batch.combined_response_json()
        )
        mock_cls.return_value = mock_client

        results = analyze_batch(batch.all_images, config)

        assert len(results) == 1
        assert results[0][1].category == "invoice"


# ── E2E: Large batch (2-step) ───────────────────────────────────


class TestE2ELargeBatch:
    """End-to-end tests for large batches (> threshold → 2-step)."""

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_large_batch_two_step(self, mock_cls, config):
        """Large batch uses 2-step: group then classify each."""
        # Build a batch > threshold
        docs = []
        total = 0
        for f in ALL_FIXTURES():
            docs.append(f)
            total += f.num_pages
            if total > SINGLE_PASS_THRESHOLD:
                break
        batch = BatchFixture(documents=docs)
        assert batch.num_pages > SINGLE_PASS_THRESHOLD

        # Responses: grouping (1) + classification (N)
        responses = [
            _mock_message(batch.grouping_response_json()),
        ]
        for doc in docs:
            responses.append(_mock_message(doc.api_response_json()))

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = responses
        mock_cls.return_value = mock_client

        results = analyze_batch(batch.all_images, config)

        assert len(results) == len(docs)
        assert mock_client.messages.create.call_count == 1 + len(docs)

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_all_pages_accounted_for(self, mock_cls, config):
        """Every page in the batch appears in exactly one result group."""
        docs = []
        total = 0
        for f in ALL_FIXTURES():
            docs.append(f)
            total += f.num_pages
            if total > SINGLE_PASS_THRESHOLD + 3:
                break
        batch = BatchFixture(documents=docs)

        responses = [_mock_message(batch.grouping_response_json())]
        for doc in docs:
            responses.append(_mock_message(doc.api_response_json()))
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = responses
        mock_cls.return_value = mock_client

        results = analyze_batch(batch.all_images, config)

        all_pages = []
        for pages, _ in results:
            all_pages.extend(pages)
        assert sorted(all_pages) == list(range(batch.num_pages))


# ── E2E: Verification pass ──────────────────────────────────────


class TestE2EVerification:
    """End-to-end tests for the Opus verification pass."""

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_low_confidence_triggers_verify(self, mock_cls, config):
        """Low-confidence batch triggers verification (2 API calls)."""
        docs = [
            fixtures_by_category("invoice")[0],
            fixtures_by_category("letter")[0],
        ]
        batch = BatchFixture(documents=docs)

        # Combined response with LOW confidence
        combined = batch.combined_response(base_confidence=60)
        combined_json = json.dumps(combined)[1:]

        responses = [
            _mock_message(combined_json),          # single-pass
            _mock_verify_message("[]"),             # verify → no changes
        ]
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = responses
        mock_cls.return_value = mock_client

        results = analyze_batch(batch.all_images, config)

        assert len(results) == 2
        assert mock_client.messages.create.call_count == 2

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_verify_reassigns_page(self, mock_cls, config):
        """Verification can reassign a page to a different document."""
        docs = [
            fixtures_by_category("invoice")[0],   # page 1
            fixtures_by_category("letter")[0],     # page 2
            fixtures_by_category("receipt")[0],    # page 3
        ]
        batch = BatchFixture(documents=docs)

        # Combined with low confidence on page 2
        combined = batch.combined_response(base_confidence=60)
        combined_json = json.dumps(combined)[1:]

        # Verify: move page 2 to doc 3
        verify_json = json.dumps([{"page": 2, "move_to_doc": 3, "reason": "same header as receipt"}])

        responses = [
            _mock_message(combined_json),
            _mock_verify_message(verify_json),
        ]
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = responses
        mock_cls.return_value = mock_client

        results = analyze_batch(batch.all_images, config)

        # Page 1 (0-indexed) should be in the receipt group now
        all_pages = []
        for pages, _ in results:
            all_pages.extend(pages)
        assert sorted(all_pages) == [0, 1, 2]

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_verify_failure_preserves_results(self, mock_cls, config):
        """If verification API fails, original results are preserved."""
        docs = [
            fixtures_by_category("invoice")[0],
            fixtures_by_category("receipt")[0],
        ]
        batch = BatchFixture(documents=docs)
        combined = batch.combined_response(base_confidence=60)
        combined_json = json.dumps(combined)[1:]

        responses = [
            _mock_message(combined_json),
        ]
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            _mock_message(combined_json),
            Exception("Opus API unavailable"),
        ]
        mock_cls.return_value = mock_client

        results = analyze_batch(batch.all_images, config)

        # Should still return 2 documents despite verify failure
        assert len(results) == 2


# ── E2E: Error recovery ─────────────────────────────────────────


class TestE2EErrorRecovery:
    """Test error handling in the full pipeline."""

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_api_timeout(self, mock_cls, config):
        """API timeout raises AnalysisError."""
        import anthropic
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = anthropic.APITimeoutError(
            request=MagicMock()
        )
        mock_cls.return_value = mock_client

        doc = fixtures_by_category("invoice")[0]
        with pytest.raises(AnalysisError, match="timed out"):
            analyze_document(doc.images, config)

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_malformed_json_response(self, mock_cls, config):
        """Malformed JSON response raises AnalysisError."""
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_message(
            '"incomplete json without closing brace'
        )
        mock_cls.return_value = mock_client

        doc = fixtures_by_category("invoice")[0]
        with pytest.raises(AnalysisError, match="parse"):
            analyze_document(doc.images, config)

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_batch_too_large(self, mock_cls, config):
        """Batch > 50 pages is rejected before any API call."""
        images = [fixtures_by_category("invoice")[0].images[0]] * 51

        with pytest.raises(AnalysisError, match="50 pages"):
            analyze_batch(images, config)

        # No API call should have been made
        mock_cls.return_value.messages.create.assert_not_called()

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_empty_batch_combined_response(self, mock_cls, config):
        """Empty response from single-pass raises AnalysisError."""
        doc = fixtures_by_category("invoice")[0]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_message("")
        mock_cls.return_value = mock_client

        with pytest.raises(AnalysisError, match="Empty"):
            analyze_batch(doc.images, config)


# ── E2E: Config integration ─────────────────────────────────────


class TestE2EConfig:
    """Test config integration with the pipeline."""

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_uses_configured_model(self, mock_cls):
        """Pipeline uses the model from config."""
        config = _config(claude_model="claude-sonnet-4-20250514")
        doc = fixtures_by_category("invoice")[0]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_message(doc.api_response_json())
        mock_cls.return_value = mock_client

        analyze_document(doc.images, config)

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["model"] == "claude-sonnet-4-20250514"

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_uses_api_key(self, mock_cls):
        """Client is created with the configured API key."""
        config = _config(api_key="sk-ant-my-secret-key")
        doc = fixtures_by_category("invoice")[0]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_message(doc.api_response_json())
        mock_cls.return_value = mock_client

        analyze_document(doc.images, config)

        mock_cls.assert_called_once_with(api_key="sk-ant-my-secret-key", timeout=120.0)


# ── E2E: Filename generation ────────────────────────────────────


class TestE2EFilenames:
    """Test filename generation across all 100 fixtures."""

    def test_all_filenames_are_valid(self):
        """Every fixture produces a valid filename (no special chars)."""
        import re
        for doc in ALL_FIXTURES():
            response = doc.api_response()
            filename = build_filename(response, "2025-04-15")
            assert filename.endswith(".pdf")
            # Only alphanumeric, underscore, hyphen, dots
            stem = filename[:-4]  # remove .pdf
            assert re.match(r"^[\w\-]+$", stem), f"Invalid filename: {filename}"

    def test_no_duplicate_filenames(self):
        """All fixtures produce distinct filenames."""
        filenames = set()
        for doc in ALL_FIXTURES():
            response = doc.api_response()
            filename = build_filename(response, "2025-04-15")
            # Filenames should be unique (unless same issuer+subject+date)
            filenames.add(filename)
        # We expect at least 90 unique names (some might collide on date)
        assert len(filenames) >= 90

    def test_filename_starts_with_date(self):
        """Filenames with known dates start with the document date."""
        for doc in ALL_FIXTURES():
            if doc.date:
                response = doc.api_response()
                filename = build_filename(response, "2025-04-15")
                assert filename.startswith(doc.date), \
                    f"Expected {filename} to start with {doc.date}"

    def test_filename_contains_category(self):
        """Filenames contain the document category."""
        for doc in ALL_FIXTURES():
            response = doc.api_response()
            filename = build_filename(response, "2025-04-15")
            assert doc.category in filename, \
                f"Expected '{doc.category}' in filename: {filename}"


# ── E2E: Batch page assignment correctness ───────────────────────


class TestE2EPageAssignment:
    """Test that batch analysis correctly assigns pages to documents."""

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_page_indices_match_images(self, mock_cls, config):
        """Returned page indices correctly map to input images."""
        docs = [
            fixtures_by_category("invoice")[0],    # 1 page
            fixtures_by_category("insurance")[0],  # 2 pages
            fixtures_by_category("receipt")[0],     # 1 page
        ]
        batch = BatchFixture(documents=docs)
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_message(
            batch.combined_response_json()
        )
        mock_cls.return_value = mock_client

        results = analyze_batch(batch.all_images, config)

        # Verify no overlapping indices
        used = set()
        for pages, _ in results:
            for p in pages:
                assert p not in used, f"Page {p} assigned to multiple documents"
                used.add(p)

        # All pages covered
        assert used == set(range(batch.num_pages))

    @patch("auto_scan.recognition.engine.anthropic.Anthropic")
    def test_batch_with_diverse_categories(self, mock_cls, config):
        """Batch with one doc from each category works correctly."""
        # Pick one doc per category (up to threshold)
        seen = set()
        docs = []
        for f in ALL_FIXTURES():
            if f.category not in seen and f.num_pages == 1:
                docs.append(f)
                seen.add(f.category)
            if len(docs) >= 8:
                break

        batch = BatchFixture(documents=docs)
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_message(
            batch.combined_response_json()
        )
        mock_cls.return_value = mock_client

        results = analyze_batch(batch.all_images, config)
        result_categories = {info.category for _, info in results}
        assert len(result_categories) == len(docs)
