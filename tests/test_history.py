"""Tests for SQLite scan history tracking."""

import json

import pytest

from auto_scan.history import _get_db, record_scan, search_history, find_by_hash


# ── Database creation ─────────────────────────────────────────────

class TestDatabase:
    """Verify database creation and schema."""

    def test_creates_db_file(self, tmp_path):
        conn = _get_db(tmp_path)
        conn.close()
        db_path = tmp_path / ".auto_scan_history.db"
        assert db_path.exists()

    def test_creates_scans_table(self, tmp_path):
        conn = _get_db(tmp_path)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        conn.close()
        table_names = [t["name"] for t in tables]
        assert "scans" in table_names

    def test_idempotent_creation(self, tmp_path):
        """Calling _get_db twice should not error."""
        conn1 = _get_db(tmp_path)
        conn1.close()
        conn2 = _get_db(tmp_path)
        conn2.close()


# ── Recording scans ───────────────────────────────────────────────

class TestRecordScan:
    """Test inserting scan records."""

    def test_returns_row_id(self, tmp_path):
        row_id = record_scan(
            output_dir=tmp_path,
            filename="test.pdf",
            folder="invoice",
            tags=["test"],
            category="invoice",
            summary="A test invoice",
            doc_date="2024-03-15",
            risk_level="none",
            risks=[],
            pages=1,
            output_path="/tmp/test.pdf",
            image_hash="abc123",
        )
        assert isinstance(row_id, int)
        assert row_id > 0

    def test_sequential_ids(self, tmp_path):
        kwargs = dict(
            output_dir=tmp_path, filename="test.pdf", folder="invoice",
            tags=[], category="invoice", summary="", doc_date=None,
            risk_level="none", risks=[], pages=1, output_path="/tmp/t.pdf",
        )
        id1 = record_scan(**kwargs)
        id2 = record_scan(**kwargs)
        assert id2 == id1 + 1


# ── Searching history ─────────────────────────────────────────────

class TestSearchHistory:
    """Test history search functionality."""

    @pytest.fixture
    def populated_db(self, tmp_path):
        """Insert several test records."""
        record_scan(
            tmp_path, "2024-invoice-vodafone.pdf", "invoice",
            ["vodafone", "telecom"], "invoice",
            "Monthly Vodafone bill", "2024-03-15",
            "none", [], 1, "/tmp/a.pdf", "hash_a",
        )
        record_scan(
            tmp_path, "2024-contract-bmw.pdf", "contract",
            ["bmw", "automotive"], "contract",
            "BMW X3 sale contract", "2024-06-10",
            "low", ["review recommended"], 3, "/tmp/b.pdf", "hash_b",
        )
        record_scan(
            tmp_path, "2024-medical-blood-test.pdf", "medical",
            ["medical", "lab"], "medical",
            "Blood test results", "2024-11-05",
            "none", [], 1, "/tmp/c.pdf", "hash_c",
        )
        return tmp_path

    def test_empty_query_returns_all(self, populated_db):
        results = search_history(populated_db)
        assert len(results) == 3

    def test_search_by_filename(self, populated_db):
        results = search_history(populated_db, query="vodafone")
        assert len(results) == 1
        assert results[0]["filename"] == "2024-invoice-vodafone.pdf"

    def test_search_by_category(self, populated_db):
        results = search_history(populated_db, query="medical")
        assert len(results) == 1

    def test_search_by_summary(self, populated_db):
        results = search_history(populated_db, query="BMW")
        assert len(results) == 1

    def test_search_by_tags(self, populated_db):
        results = search_history(populated_db, query="telecom")
        assert len(results) == 1

    def test_limit_parameter(self, populated_db):
        results = search_history(populated_db, limit=1)
        assert len(results) == 1

    def test_returns_dicts(self, populated_db):
        results = search_history(populated_db)
        assert all(isinstance(r, dict) for r in results)

    def test_no_results_returns_empty(self, populated_db):
        results = search_history(populated_db, query="nonexistent_xyz")
        assert results == []


# ── Hash lookup ───────────────────────────────────────────────────

class TestFindByHash:
    """Test duplicate detection by image hash."""

    def test_finds_existing_hash(self, tmp_path):
        record_scan(
            tmp_path, "test.pdf", "invoice", [], "invoice", "",
            None, "none", [], 1, "/tmp/t.pdf", "abc123",
        )
        result = find_by_hash(tmp_path, "abc123")
        assert result is not None
        assert result["filename"] == "test.pdf"

    def test_returns_none_for_unknown_hash(self, tmp_path):
        result = find_by_hash(tmp_path, "nonexistent_hash")
        assert result is None

    def test_returns_dict(self, tmp_path):
        record_scan(
            tmp_path, "test.pdf", "invoice", [], "invoice", "",
            None, "none", [], 1, "/tmp/t.pdf", "def456",
        )
        result = find_by_hash(tmp_path, "def456")
        assert isinstance(result, dict)
