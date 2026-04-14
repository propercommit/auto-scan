"""Scan history tracking via SQLite."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


def _get_db(output_dir: Path) -> sqlite3.Connection:
    db_path = output_dir / ".auto_scan_history.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    new_db = not db_path.exists()
    conn = sqlite3.connect(str(db_path))
    if new_db:
        os.chmod(db_path, 0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scanned_at TEXT NOT NULL,
            filename TEXT NOT NULL,
            folder TEXT NOT NULL,
            tags TEXT NOT NULL DEFAULT '[]',
            category TEXT NOT NULL DEFAULT 'other',
            summary TEXT NOT NULL DEFAULT '',
            date TEXT,
            risk_level TEXT NOT NULL DEFAULT 'none',
            risks TEXT NOT NULL DEFAULT '[]',
            pages INTEGER NOT NULL DEFAULT 1,
            output_path TEXT NOT NULL,
            image_hash TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_scans_hash ON scans(image_hash)
    """)
    conn.commit()
    return conn


def record_scan(
    output_dir: Path,
    filename: str,
    folder: str,
    tags: list[str],
    category: str,
    summary: str,
    doc_date: str | None,
    risk_level: str,
    risks: list[str],
    pages: int,
    output_path: str,
    image_hash: str | None = None,
) -> int:
    """Record a completed scan in the history database. Returns the row id."""
    conn = _get_db(output_dir)
    try:
        cur = conn.execute(
            """INSERT INTO scans
               (scanned_at, filename, folder, tags, category, summary, date,
                risk_level, risks, pages, output_path, image_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().isoformat(),
                filename,
                folder,
                json.dumps(tags),
                category,
                summary,
                doc_date,
                risk_level,
                json.dumps(risks),
                pages,
                output_path,
                image_hash,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def search_history(
    output_dir: Path,
    query: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Search scan history. Query matches filename, folder, tags, or summary."""
    conn = _get_db(output_dir)
    try:
        if query:
            rows = conn.execute(
                """SELECT * FROM scans
                   WHERE filename LIKE ? OR folder LIKE ?
                         OR tags LIKE ? OR summary LIKE ?
                   ORDER BY scanned_at DESC LIMIT ?""",
                (f"%{query}%",) * 4 + (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM scans ORDER BY scanned_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def find_by_hash(output_dir: Path, image_hash: str) -> dict | None:
    """Look up a previous scan by image hash. Returns the row or None."""
    conn = _get_db(output_dir)
    try:
        row = conn.execute(
            "SELECT * FROM scans WHERE image_hash = ? LIMIT 1",
            (image_hash,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
