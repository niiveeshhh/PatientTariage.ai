"""
SQLite store - Blueprint 20.

"Chosen because it is a FILE - nothing to install, nothing to start, nothing to fail
on demo day."  Append-only audit table with the same hash-chain construction as the
in-memory reference chain in app/audit/chain.py.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, List, Optional

from app.audit.chain import (
    AuditRecord, GENESIS_HASH, VerificationResult, compute_hash, verify_chain,
)

DEFAULT_DB = os.path.join("data", "patienttriage.sqlite3")

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    record_id  INTEGER PRIMARY KEY,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    event_type TEXT,
    patient_ref TEXT,
    timestamp_min REAL
);
CREATE TABLE IF NOT EXISTS access_log (
    record_id  INTEGER PRIMARY KEY,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL,
    payload    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_audit_patient ON audit(patient_ref);
CREATE INDEX IF NOT EXISTS idx_audit_event ON audit(event_type);
"""


class Store:
    """Durable, append-only.  UPDATE and DELETE are never issued against `audit`."""

    def __init__(self, path: str = DEFAULT_DB):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------------
    def head_hash(self, table: str = "audit") -> str:
        row = self.conn.execute(
            f"SELECT hash FROM {table} ORDER BY record_id DESC LIMIT 1").fetchone()
        return row["hash"] if row else GENESIS_HASH

    def append(self, payload: Dict[str, Any], table: str = "audit") -> AuditRecord:
        """Durable append.  The commit happens BEFORE this returns, which is what
        makes 'written before display' a fact rather than an intention."""
        n = self.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        record_id = n + 1
        prev = self.head_hash(table)
        h = compute_hash(record_id, prev, payload)
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        if table == "audit":
            self.conn.execute(
                "INSERT INTO audit (record_id, prev_hash, hash, payload, event_type,"
                " patient_ref, timestamp_min) VALUES (?,?,?,?,?,?,?)",
                (record_id, prev, h, blob, payload.get("event_type"),
                 payload.get("patient_ref"), payload.get("timestamp_min")))
        else:
            self.conn.execute(
                f"INSERT INTO {table} (record_id, prev_hash, hash, payload)"
                " VALUES (?,?,?,?)", (record_id, prev, h, blob))
        self.conn.commit()
        return AuditRecord(record_id=record_id, prev_hash=prev, hash=h, payload=payload)

    def records(self, table: str = "audit", patient_ref: Optional[str] = None,
                limit: Optional[int] = None) -> List[AuditRecord]:
        sql = f"SELECT * FROM {table}"
        args: List[Any] = []
        if patient_ref:
            sql += " WHERE patient_ref = ?"
            args.append(patient_ref)
        sql += " ORDER BY record_id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [
            AuditRecord(record_id=r["record_id"], prev_hash=r["prev_hash"],
                        hash=r["hash"], payload=json.loads(r["payload"]))
            for r in self.conn.execute(sql, args)
        ]

    def verify(self, table: str = "audit") -> VerificationResult:
        return verify_chain(self.records(table))

    def count(self, table: str = "audit") -> int:
        return self.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]

    # ------------------------------------------------------------------
    def tamper(self, record_id: int, field_path: str, new_value: Any) -> bool:
        """WOW moment 5.  Mutates a stored payload WITHOUT recomputing its hash -
        exactly what an after-the-fact editor would do.  Detection is NOT hard-coded:
        verify() recomputes sha256 over the stored payload."""
        row = self.conn.execute(
            "SELECT payload FROM audit WHERE record_id = ?", (record_id,)).fetchone()
        if row is None:
            return False
        payload = json.loads(row["payload"])
        parts = field_path.split(".")
        cursor: Any = payload
        for p in parts[:-1]:
            if not isinstance(cursor, dict) or p not in cursor:
                return False
            cursor = cursor[p]
        if not isinstance(cursor, dict):
            return False
        cursor[parts[-1]] = new_value
        self.conn.execute(
            "UPDATE audit SET payload = ? WHERE record_id = ?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
             record_id))
        self.conn.commit()
        return True

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                          (key, json.dumps(value, default=str)))
        self.conn.commit()

    def get_meta(self, key: str) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def close(self) -> None:
        self.conn.close()


def persist_chain(chain, store: Store, table: str = "audit") -> int:
    """Flush an in-memory chain to disk, preserving record ids and hashes."""
    written = 0
    existing = store.count(table)
    for rec in chain.records[existing:]:
        store.append(rec.payload, table)
        written += 1
    return written
