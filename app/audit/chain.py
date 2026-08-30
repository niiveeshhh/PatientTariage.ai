"""
Tamper-evident audit chain - Blueprint 15.2.

    Each record embeds the cryptographic hash of its predecessor.  Verification
    recomputes the chain and reports THE FIRST INDEX AT WHICH IT BREAKS.  Editing
    any historical record is therefore detectable, and detectable AT A SPECIFIC
    POSITION.

    Write ordering: the audit record is committed DURABLY BEFORE the recommendation
    is displayed.  The log can never lag the screen; a nurse cannot have seen
    something the log does not contain.

    If the audit store is unavailable: clinical decisions are REFUSED, not queued.

    "This is a HASH CHAIN, NOT A BLOCKCHAIN.  There is no distributed consensus, no
    token and no chain of custody across untrusted parties, because none of those
    problems exist here.  Using the word 'blockchain' would be a decoration; this is
    the cheap, correct primitive for the actual requirement, which is detecting
    after-the-fact edits."
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

GENESIS_HASH = "0" * 64


class AuditStoreUnavailable(RuntimeError):
    """Blueprint 13.3: 'An unlogged clinical change is worse than a blocked one,
    because it is invisible afterwards.'"""


def canonical_json(payload: Dict[str, Any]) -> str:
    """Deterministic serialisation.  Sorted keys, no whitespace drift, so the same
    decision hashes identically on every machine and every run."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def compute_hash(record_id: int, prev_hash: str, payload: Dict[str, Any]) -> str:
    material = f"{record_id}|{prev_hash}|{canonical_json(payload)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class AuditRecord:
    record_id: int
    prev_hash: str
    hash: str
    payload: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "record_id": self.record_id,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
            "payload": self.payload,
        }

    def recompute(self) -> str:
        return compute_hash(self.record_id, self.prev_hash, self.payload)


@dataclass
class VerificationResult:
    valid: bool
    checked: int
    first_broken_index: Optional[int] = None
    detail: Optional[str] = None
    expected_hash: Optional[str] = None
    stored_hash: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "checked": self.checked,
            "first_broken_index": self.first_broken_index,
            "detail": self.detail,
            "expected_hash": self.expected_hash,
            "stored_hash": self.stored_hash,
        }


def verify_chain(records: Sequence[AuditRecord]) -> VerificationResult:
    """Recompute the chain and report the FIRST index at which it breaks.

    Two independent things are checked at every link:
      1. the stored hash still matches the stored payload   (content tampering)
      2. prev_hash still matches the predecessor's hash     (structural tampering)
    """
    prev = GENESIS_HASH
    for idx, rec in enumerate(records):
        if rec.prev_hash != prev:
            return VerificationResult(
                valid=False, checked=idx, first_broken_index=idx,
                detail=(f"link broken at index {idx} (record_id {rec.record_id}): "
                        f"prev_hash does not match the preceding record's hash"),
                expected_hash=prev, stored_hash=rec.prev_hash,
            )
        recomputed = rec.recompute()
        if recomputed != rec.hash:
            return VerificationResult(
                valid=False, checked=idx, first_broken_index=idx,
                detail=(f"payload altered at index {idx} (record_id {rec.record_id}): "
                        f"recomputed hash does not match the stored hash"),
                expected_hash=recomputed, stored_hash=rec.hash,
            )
        prev = rec.hash
    return VerificationResult(valid=True, checked=len(records),
                              detail=f"chain verified over {len(records)} records")


@dataclass
class AuditChain:
    """An append-only, hash-chained log.

    This in-memory chain is the reference implementation and the thing the property
    tests exercise; app/store/db.py persists exactly the same structure to SQLite.
    """
    records: List[AuditRecord] = field(default_factory=list)
    available: bool = True
    _write_latencies: List[float] = field(default_factory=list)

    @property
    def head_hash(self) -> str:
        return self.records[-1].hash if self.records else GENESIS_HASH

    def append(self, payload: Dict[str, Any]) -> AuditRecord:
        """Durable append.  Raises when the store is unavailable so the CALLER must
        decide - and the caller's only permitted decision is to REFUSE the clinical
        change (Blueprint 13.3)."""
        if not self.available:
            raise AuditStoreUnavailable(
                "Audit store unavailable - clinical decisions are REFUSED, not "
                "queued. Blueprint 13.3: an unlogged clinical change is worse than "
                "a blocked one, because it is invisible afterwards."
            )
        record_id = len(self.records) + 1
        prev = self.head_hash
        h = compute_hash(record_id, prev, payload)
        rec = AuditRecord(record_id=record_id, prev_hash=prev, hash=h, payload=payload)
        self.records.append(rec)
        return rec

    def verify(self) -> VerificationResult:
        return verify_chain(self.records)

    def for_patient(self, patient_ref: str) -> List[AuditRecord]:
        return [r for r in self.records
                if r.payload.get("patient_ref") == patient_ref]

    def tamper_for_demo(self, index: int, field_path: str, new_value: Any) -> bool:
        """WOW moment 5.  Blueprint 24: 'A judge can be invited to try to edit one
        during the demo.'

        This mutates a STORED PAYLOAD WITHOUT updating its hash - exactly what an
        after-the-fact editor would do - so that verify() genuinely detects it.
        Nothing about the detection is hard-coded: verify() recomputes sha256.
        """
        if index < 0 or index >= len(self.records):
            return False
        payload = self.records[index].payload
        parts = field_path.split(".")
        cursor: Any = payload
        for p in parts[:-1]:
            if not isinstance(cursor, dict) or p not in cursor:
                return False
            cursor = cursor[p]
        if not isinstance(cursor, dict):
            return False
        cursor[parts[-1]] = new_value
        return True

    def completeness(self, displayed_recommendation_count: int) -> float:
        """Blueprint 22.3: audit completeness = fraction of DISPLAYED
        recommendations with a durable record.  Must be 100%."""
        if displayed_recommendation_count <= 0:
            return 1.0
        n = sum(1 for r in self.records
                if r.payload.get("event_type") == "recommendation")
        return min(1.0, n / float(displayed_recommendation_count))


# ---------------------------------------------------------------------------
# The separate access-log chain - Blueprint 15.1 "access_log (separate chain)"
# ---------------------------------------------------------------------------

@dataclass
class AccessLogChain(AuditChain):
    """"Who read what, when, under which declared purpose."

    Held as a SEPARATE chain because DPDP Rule 6 requires visibility of access to
    personal data through logs, monitoring and review [S7], and because mixing read
    events into the clinical chain would make the clinical chain unreadable.
    """

    def log_access(self, actor: str, actor_role: str, purpose: str,
                   patient_ref: str, fields: List[str], granted: bool,
                   timestamp_min: float, denial_reason: Optional[str] = None
                   ) -> AuditRecord:
        return self.append({
            "event_type": "access",
            "actor": actor,
            "actor_role": actor_role,
            "purpose": purpose,
            "patient_ref": patient_ref,
            "fields": sorted(fields),
            "granted": granted,
            "denial_reason": denial_reason,
            "timestamp_min": round(timestamp_min, 4),
        })
