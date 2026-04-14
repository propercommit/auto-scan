"""API token usage tracking, budget enforcement, and rate limiting."""

from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime
from pathlib import Path

_lock = threading.Lock()
_last_api_call: float = 0.0

# Minimum seconds between API calls
MIN_API_INTERVAL = 5

# Claude Sonnet 4 pricing (USD per token)
COST_PER_INPUT_TOKEN = 3.0 / 1_000_000
COST_PER_OUTPUT_TOKEN = 15.0 / 1_000_000


def _usage_path() -> Path:
    """Return the path to the daily usage JSON file."""
    return Path.home() / ".auto_scan" / "usage.json"


def _load_usage() -> dict:
    """Load today's usage from disk. Resets if the date has changed."""
    path = _usage_path()
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if data.get("date") == date.today().isoformat():
                return data
        except Exception:
            pass
    return {
        "date": date.today().isoformat(),
        "input_tokens": 0,
        "output_tokens": 0,
        "api_calls": 0,
        "history": [],
    }


def _save_usage(usage: dict) -> None:
    """Persist usage data to disk."""
    path = _usage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(usage, indent=2) + "\n")


def record_usage(input_tokens: int, output_tokens: int) -> dict:
    """Record tokens consumed by an API call. Returns updated usage."""
    with _lock:
        usage = _load_usage()
        usage["input_tokens"] += input_tokens
        usage["output_tokens"] += output_tokens
        usage["api_calls"] += 1
        if "history" not in usage:
            usage["history"] = []
        usage["history"].append({
            "time": datetime.now().strftime("%H:%M"),
            "input": input_tokens,
            "output": output_tokens,
            "cumulative": usage["input_tokens"] + usage["output_tokens"],
        })
        _save_usage(usage)
        return dict(usage)


def get_usage() -> dict:
    """Get today's usage with total tokens and estimated cost."""
    with _lock:
        usage = _load_usage()
    total = usage["input_tokens"] + usage["output_tokens"]
    cost = (
        usage["input_tokens"] * COST_PER_INPUT_TOKEN
        + usage["output_tokens"] * COST_PER_OUTPUT_TOKEN
    )
    history = usage.get("history", [])
    return {
        **usage,
        "total_tokens": total,
        "estimated_cost": round(cost, 4),
        "history": history,
    }


def check_budget(max_tokens: int) -> tuple[bool, dict]:
    """Check if today's usage is within budget.

    Args:
        max_tokens: Maximum total tokens allowed per day. 0 means unlimited.

    Returns:
        (is_within_budget, usage_dict)
    """
    usage = get_usage()
    if max_tokens <= 0:
        return True, usage
    return usage["total_tokens"] < max_tokens, usage


def check_rate_limit() -> None:
    """Enforce minimum interval between API calls.

    Raises RuntimeError if called too soon after the last API call.
    """
    global _last_api_call
    with _lock:
        now = time.time()
        elapsed = now - _last_api_call
        if _last_api_call > 0 and elapsed < MIN_API_INTERVAL:
            wait = MIN_API_INTERVAL - elapsed
            raise RuntimeError(
                f"Rate limit: please wait {wait:.0f}s between scans."
            )
        _last_api_call = now


def reset_daily_usage() -> None:
    """Reset today's usage counters to zero."""
    with _lock:
        usage = {
            "date": date.today().isoformat(),
            "input_tokens": 0,
            "output_tokens": 0,
            "api_calls": 0,
            "history": [],
        }
        _save_usage(usage)
