"""
The adapter seam - Blueprint 6.10, 16.4.

"A SINGLE ADAPTER INTERFACE; every tier normalises into the same internal patient
object, SO THE ENGINE NEVER KNOWS WHICH TIER IT IS RUNNING ON."

    L1 standalone     - built. This is the demo.
    L2 file / REST    - built (adapter seam plus a CSV path).
    L3 EHR read       - DEMONSTRATED, not connected: a synthetic FHIR R4 bundle is
                        imported and proven to produce BYTE-IDENTICAL output.
    L4 real-time      - DESIGNED ONLY. Explicitly out of scope.

Fallback (Blueprint complexity 18): "Adapter failure -> the patient is created from
whatever the nurse types, at L1. THE SYSTEM NEVER BLOCKS CARE ON AN INTEGRATION."
"""

from __future__ import annotations

import csv
import io
from typing import Any, Dict, Iterable, List, Optional, Protocol

from app.core.models import Patient


class AdapterError(Exception):
    """Raised on a malformed source.  Callers drop the patient to L1 rather than
    blocking care."""


class Adapter(Protocol):
    tier: str
    name: str

    def to_patient(self, source: Any, now_min: float) -> Patient: ...


TIERS = {
    "L1": {"name": "Standalone", "status": "BUILT - this is the demo",
           "data_path": "Nurse types into the tool; simulator seeds the cohort. "
                        "No external dependency.",
           "proves": "The product is useful on day one at a hospital with NO "
                     "INTEGRATION AT ALL - which is most of them."},
    "L2": {"name": "File / REST drop", "status": "BUILT (adapter seam plus CSV path)",
           "data_path": "CSV or JSON batch import of registration and vitals; REST "
                        "endpoint for pushed observations.",
           "proves": "Even a hospital whose 'integration' is a nightly export can "
                     "connect."},
    "L3": {"name": "EHR read", "status": "DEMONSTRATED, NOT CONNECTED",
           "data_path": "HL7 v2 ADT for registration and movement; FHIR R4 Patient, "
                        "Encounter, Observation, Condition. In India the ABDM "
                        "implementation guide profiles FHIR R4 [S16].",
           "proves": "The integration story is concrete and standards-based, and the "
                     "equivalence test proves the seam actually works - WITHOUT "
                     "pretending we have a hospital."},
    "L4": {"name": "Real-time hospital integration", "status": "DESIGNED ONLY - "
                                                              "EXPLICITLY OUT OF SCOPE",
           "data_path": "Event-driven: device streams, bed management, staff roster, "
                        "SMART-on-FHIR launch.",
           "proves": "There is a credible path from prototype to production that does "
                     "not require rewriting the engine - because the engine never "
                     "knows which tier it is on."},
}


def normalised_signature(patient: Patient, now_min: float) -> Dict[str, Any]:
    """The canonical form used by the ADAPTER EQUIVALENCE TEST.

    Blueprint 6.10: "the same patient expressed natively and as a FHIR bundle must
    produce BYTE-IDENTICAL recommendations."  Comparing the normalised patient AND
    the resulting recommendation is what makes that testable.
    """
    snap = patient.snapshot(now_min)
    # The pseudonymous ref differs by construction between paths; the CLINICAL
    # content must not.
    snap = dict(snap)
    snap.pop("patient_ref", None)
    return snap


# ---------------------------------------------------------------------------
# L2 - CSV
# ---------------------------------------------------------------------------

class CsvAdapter:
    tier = "L2"
    name = "csv_file_drop"

    FIELDS = ["patient_ref", "age_days", "age_source", "sex", "arrival_mode",
              "chief_complaint", "heart_rate", "respiratory_rate", "spo2",
              "systolic_bp", "diastolic_bp", "temperature_c", "consciousness_acvpu",
              "pain_score"]

    def rows(self, text: str) -> Iterable[Dict[str, str]]:
        return csv.DictReader(io.StringIO(text))

    def to_patients(self, text: str, now_min: float) -> List[Patient]:
        from app.core.models import (
            AgeSource, ArrivalMode, Observation, Provenance, Quality,
        )
        out: List[Patient] = []
        for row in self.rows(text):
            try:
                p = Patient(
                    patient_ref=row.get("patient_ref") or f"CSV-{len(out) + 1:04d}",
                    arrival_timestamp_min=now_min,
                    age_days=int(row["age_days"]) if row.get("age_days") else None,
                    age_source=AgeSource(row.get("age_source") or "unknown"),
                    sex=row.get("sex") or "unknown",
                    arrival_mode=ArrivalMode(row.get("arrival_mode") or "walk_in"),
                    stated_chief_complaint=row.get("chief_complaint") or "",
                )
                for f in ("heart_rate", "respiratory_rate", "spo2", "systolic_bp",
                          "diastolic_bp", "pain_score"):
                    if row.get(f):
                        p.set_observation(Observation(f, int(float(row[f])),
                                                      Provenance.DEV, now_min,
                                                      Quality.CLEAN, 0.9))
                if row.get("temperature_c"):
                    p.set_observation(Observation("temperature_c",
                                                  float(row["temperature_c"]),
                                                  Provenance.DEV, now_min,
                                                  Quality.CLEAN, 0.9))
                if row.get("consciousness_acvpu"):
                    p.set_observation(Observation("consciousness_acvpu",
                                                  row["consciousness_acvpu"],
                                                  Provenance.OBS, now_min,
                                                  Quality.CLEAN, 0.95))
                out.append(p)
            except (ValueError, KeyError) as exc:
                raise AdapterError(f"malformed CSV row {row!r}: {exc}") from exc
        return out
