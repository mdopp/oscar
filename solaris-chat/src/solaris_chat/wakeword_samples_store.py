"""Metadata & Audit Storage for Wakeword Audio Samples (#1056).

Manages persistent sample metadata (filename, intended phrase, STT transcript, validity)
in solaris.db for sample reuse, quality audit, and selective deletion.
"""

from __future__ annotations

import os
import sqlite3
from pathlib import Path


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(db_path: str) -> None:
    with _connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wakeword_samples (
                id TEXT PRIMARY KEY,
                wakeword_id TEXT NOT NULL DEFAULT 'solaris',
                filename TEXT NOT NULL,
                resident_uid TEXT NOT NULL DEFAULT 'household',
                intended_phrase TEXT NOT NULL DEFAULT 'Solaris',
                stt_transcript TEXT,
                is_valid INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()


def add_sample(
    db_path: str,
    sample_id: str,
    filename: str,
    wakeword_id: str = "solaris",
    resident_uid: str = "household",
    intended_phrase: str = "Solaris",
    stt_transcript: str = "",
    is_valid: bool = True,
) -> dict:
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute("""
            INSERT INTO wakeword_samples (id, wakeword_id, filename, resident_uid, intended_phrase, stt_transcript, is_valid, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                stt_transcript = excluded.stt_transcript,
                is_valid = excluded.is_valid
        """, (sample_id, wakeword_id, filename, resident_uid, intended_phrase, stt_transcript, 1 if is_valid else 0))
        conn.commit()

    return get_sample(db_path, sample_id) or {}


def get_sample(db_path: str, sample_id: str) -> dict | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, wakeword_id, filename, resident_uid, intended_phrase, stt_transcript, is_valid, created_at FROM wakeword_samples WHERE id = ?",
            (sample_id,)
        ).fetchone()
        if row:
            return dict(row)
        return None


def list_samples(db_path: str, wakeword_id: str | None = None) -> list[dict]:
    init_db(db_path)
    with _connect(db_path) as conn:
        if wakeword_id:
            rows = conn.execute(
                "SELECT id, wakeword_id, filename, resident_uid, intended_phrase, stt_transcript, is_valid, created_at FROM wakeword_samples WHERE wakeword_id = ? ORDER BY created_at DESC",
                (wakeword_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, wakeword_id, filename, resident_uid, intended_phrase, stt_transcript, is_valid, created_at FROM wakeword_samples ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def get_suspicious_samples(db_path: str, wakeword_id: str | None = None) -> list[dict]:
    init_db(db_path)
    with _connect(db_path) as conn:
        if wakeword_id:
            rows = conn.execute(
                "SELECT id, wakeword_id, filename, resident_uid, intended_phrase, stt_transcript, is_valid, created_at FROM wakeword_samples WHERE wakeword_id = ? AND is_valid = 0 ORDER BY created_at DESC",
                (wakeword_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, wakeword_id, filename, resident_uid, intended_phrase, stt_transcript, is_valid, created_at FROM wakeword_samples WHERE is_valid = 0 ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def delete_sample(db_path: str, sample_id: str) -> bool:
    init_db(db_path)
    sample = get_sample(db_path, sample_id)
    if sample and sample.get("filename") and os.path.exists(sample["filename"]):
        try:
            os.remove(sample["filename"])
        except Exception:
            pass

    with _connect(db_path) as conn:
        conn.execute("DELETE FROM wakeword_samples WHERE id = ?", (sample_id,))
        conn.commit()
    return True
