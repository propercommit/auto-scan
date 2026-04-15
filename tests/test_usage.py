"""Tests for API token usage tracking, budgets, and rate limiting."""

import json
import time
from pathlib import Path

import pytest

from auto_scan.usage import (
    _usage_path,
    _load_usage,
    _save_usage,
    record_usage,
    get_usage,
    check_budget,
    check_rate_limit,
    reset_daily_usage,
    COST_PER_INPUT_TOKEN,
    COST_PER_OUTPUT_TOKEN,
    MODEL_PRICING,
    MIN_API_INTERVAL,
)


@pytest.fixture(autouse=True)
def isolate_usage(tmp_path, monkeypatch):
    """Redirect usage file to tmp dir and reset global state."""
    usage_file = tmp_path / "usage.json"
    monkeypatch.setattr("auto_scan.usage._usage_path", lambda: usage_file)
    # Reset the global rate limit timestamp
    monkeypatch.setattr("auto_scan.usage._last_api_call", 0.0)


# ── Usage tracking ────────────────────────────────────────────────

class TestRecordUsage:
    """Test token recording."""

    def test_records_tokens(self):
        result = record_usage(1000, 500)
        assert result["input_tokens"] == 1000
        assert result["output_tokens"] == 500
        assert result["api_calls"] == 1

    def test_accumulates(self):
        record_usage(1000, 500)
        result = record_usage(2000, 1000)
        assert result["input_tokens"] == 3000
        assert result["output_tokens"] == 1500
        assert result["api_calls"] == 2

    def test_history_entries(self):
        record_usage(100, 50)
        record_usage(200, 100)
        usage = get_usage()
        assert len(usage["history"]) == 2
        assert usage["history"][0]["input"] == 100
        assert usage["history"][1]["input"] == 200


class TestGetUsage:
    """Test usage retrieval and cost calculation."""

    def test_empty_usage(self):
        usage = get_usage()
        assert usage["total_tokens"] == 0
        assert usage["estimated_cost"] == 0

    def test_cost_calculation(self):
        record_usage(1_000_000, 100_000)
        usage = get_usage()
        expected_cost = round(
            1_000_000 * COST_PER_INPUT_TOKEN + 100_000 * COST_PER_OUTPUT_TOKEN,
            4,
        )
        assert usage["estimated_cost"] == expected_cost
        assert usage["total_tokens"] == 1_100_000


# ── Model-aware pricing ─────────────────────────────────────────

class TestModelPricing:
    """Test per-model cost tracking."""

    def test_sonnet_default(self):
        """No model → Sonnet pricing."""
        record_usage(1_000_000, 100_000)
        usage = get_usage()
        expected = round(
            1_000_000 * MODEL_PRICING["sonnet"]["input"]
            + 100_000 * MODEL_PRICING["sonnet"]["output"],
            4,
        )
        assert usage["estimated_cost"] == expected

    def test_opus_pricing(self):
        """Opus model should use higher rates."""
        record_usage(1_000_000, 100_000, model="claude-opus-4-20250514")
        usage = get_usage()
        expected = round(
            1_000_000 * MODEL_PRICING["opus"]["input"]
            + 100_000 * MODEL_PRICING["opus"]["output"],
            4,
        )
        assert usage["estimated_cost"] == expected

    def test_haiku_pricing(self):
        """Haiku model should use lower rates."""
        record_usage(1_000_000, 100_000, model="claude-haiku-3")
        usage = get_usage()
        expected = round(
            1_000_000 * MODEL_PRICING["haiku"]["input"]
            + 100_000 * MODEL_PRICING["haiku"]["output"],
            4,
        )
        assert usage["estimated_cost"] == expected

    def test_mixed_models_accumulate(self):
        """Multiple calls with different models accumulate correctly."""
        record_usage(100_000, 10_000, model="claude-sonnet-4-20250514")
        record_usage(50_000, 5_000, model="claude-opus-4-20250514")
        usage = get_usage()

        sonnet_cost = (
            100_000 * MODEL_PRICING["sonnet"]["input"]
            + 10_000 * MODEL_PRICING["sonnet"]["output"]
        )
        opus_cost = (
            50_000 * MODEL_PRICING["opus"]["input"]
            + 5_000 * MODEL_PRICING["opus"]["output"]
        )
        expected = round(sonnet_cost + opus_cost, 4)
        assert usage["estimated_cost"] == expected

    def test_history_includes_model(self):
        """History entries should record model name and per-call cost."""
        record_usage(100, 50, model="claude-opus-4-20250514")
        usage = get_usage()
        entry = usage["history"][0]
        assert entry["model"] == "claude-opus-4-20250514"
        assert entry["cost"] > 0

    def test_unknown_model_defaults_to_sonnet(self):
        """Unrecognized model name falls back to Sonnet pricing."""
        record_usage(1_000_000, 100_000, model="some-future-model")
        usage = get_usage()
        expected = round(
            1_000_000 * MODEL_PRICING["sonnet"]["input"]
            + 100_000 * MODEL_PRICING["sonnet"]["output"],
            4,
        )
        assert usage["estimated_cost"] == expected


# ── Budget enforcement ────────────────────────────────────────────

class TestCheckBudget:
    """Test daily budget enforcement."""

    def test_within_budget(self):
        record_usage(100, 50)
        ok, usage = check_budget(1000)
        assert ok is True

    def test_over_budget(self):
        record_usage(900, 200)
        ok, usage = check_budget(1000)
        assert ok is False

    def test_unlimited_budget(self):
        record_usage(999_999, 999_999)
        ok, usage = check_budget(0)
        assert ok is True


# ── Rate limiting ─────────────────────────────────────────────────

class TestCheckRateLimit:
    """Test minimum interval between API calls."""

    def test_first_call_passes(self):
        # First call should never fail
        check_rate_limit()

    def test_rapid_second_call_raises(self):
        check_rate_limit()
        with pytest.raises(RuntimeError, match="Rate limit"):
            check_rate_limit()


# ── Reset ─────────────────────────────────────────────────────────

class TestResetDailyUsage:
    """Test usage counter reset."""

    def test_reset_clears_counters(self):
        record_usage(5000, 2000)
        reset_daily_usage()
        usage = get_usage()
        assert usage["total_tokens"] == 0
        assert usage["api_calls"] == 0
        assert usage["history"] == []
