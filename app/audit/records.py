"""
The audit record - Blueprint 15.1.

"Four audiences read this log for different reasons.  A field that serves none of
them is surveillance; a missing field that one of them needs is a gap discovered at
the worst moment."

    CR clinical review · SI safety investigation · MD model debugging ·
    RC regulatory compliance · BA bias analysis

The full 21-field set below carries an explicit AUDIENCE MAP, so a reviewer can see
which fields serve which need.  Blueprint 19: "21-field record with an audience map,
written durably BEFORE DISPLAY, hash-chained, with a live chain-verify the judges
can try to break."
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Audience(str, Enum):
    CR = "clinical_review"
    SI = "safety_investigation"
    MD = "model_debugging"
    RC = "regulatory_compliance"
    BA = "bias_analysis"


# Blueprint 15.1, reproduced as data so the governance tab can render it.
AUDIENCE_MAP: Dict[str, List[str]] = {
    "record_id":                 ["SI", "RC"],
    "prev_hash":                 ["SI", "RC"],
    "hash":                      ["SI", "RC"],
    "patient_ref":               ["CR", "SI", "RC"],
    "timestamp":                 ["CR", "SI", "MD", "RC", "BA"],
    "input_snapshot":            ["CR", "SI", "MD", "BA"],
    "data_freshness_summary":    ["SI", "MD"],
    "envelope_id_version":       ["CR", "SI", "RC", "BA"],
    "versions":                  ["SI", "MD", "RC"],
    "fired_rules":               ["CR", "SI", "RC"],
    "risk_score_components":     ["MD", "BA"],
    "uncertainty_class_u1_u5":   ["SI", "MD"],
    "prediction_set_acted_level": ["CR", "SI", "MD", "RC"],
    "missing_fields_reasons":    ["CR", "SI", "BA"],
    "recommendation_explanation": ["CR", "SI", "RC"],
    "ttl_assigned_basis":        ["CR", "SI"],
    "clinician_decision":        ["CR", "SI", "RC", "BA"],
    "override":                  ["CR", "SI", "RC", "BA"],
    "resulting_queue_state":     ["SI", "MD"],
    "reassessment_events":       ["CR", "SI", "MD"],
    "mode_occupancy_staffing":   ["SI", "MD", "BA"],
    "system_errors_degradation": ["SI", "MD", "RC"],
}

FIELD_RATIONALE: Dict[str, str] = {
    "input_snapshot": (
        "The single most important field. Without it, no past decision can be "
        "reproduced or defended."
    ),
    "data_freshness_summary": (
        "'The vitals were 40 minutes old' is often the whole explanation for an "
        "adverse event."
    ),
    "envelope_id_version": (
        "The age-stratification decision is auditable. A wrong envelope is a "
        "specific, findable defect."
    ),
    "uncertainty_class_u1_u5": (
        "Answers 'did the system know it did not know?' - the central question "
        "after a miss."
    ),
    "prediction_set_acted_level": (
        "Demonstrates escalation bias case by case. The evidence behind the "
        "Escalation Premium metric."
    ),
    "missing_fields_reasons": (
        "Distinguishes 'we did not know' from 'we knew and got it wrong' - "
        "different failures with different fixes."
    ),
    "clinician_decision": (
        "The clinical decision, held separately from the recommendation. The gap "
        "between them is the accountability boundary."
    ),
    "resulting_queue_state": (
        "Reconstructs the room, not just the patient - necessary for any "
        "counterfactual analysis."
    ),
    "mode_occupancy_staffing": (
        "The same decision means different things at 40% and 140% occupancy."
    ),
}


class ActorRole(str, Enum):
    TRIAGE_NURSE = "triage_nurse"
    CHARGE_NURSE = "charge_nurse"
    PHYSICIAN = "physician"
    AUDITOR = "auditor"
    DPO = "data_protection_officer"
    ENGINE = "engine"          # the engine's own VERSIONED identity, never "system"


class OverrideDirection(str, Enum):
    ESCALATE = "escalate"
    DE_ESCALATE = "de_escalate"
    LATERAL = "lateral"


# Blueprint 14.4 - the complete NINE-CATEGORY reason taxonomy.
# "Nine categories - enough to be informative, few enough to pick in two seconds."
OVERRIDE_REASONS: Dict[str, Dict[str, Any]] = {
    "R1": {"label": "Saw the patient - looks better than the data",
           "free_text_required": False},
    "R2": {"label": "Saw the patient - looks worse than the data",
           "free_text_required": False},
    "R3": {"label": "Data is wrong or stale", "free_text_required": False},
    "R4": {"label": "This is the patient's known baseline", "free_text_required": False},
    "R5": {"label": "Clinical context the system does not have "
                    "(palliative, chronic, recent review)", "free_text_required": False},
    "R6": {"label": "Operational or resource reason", "free_text_required": False},
    "R7": {"label": "Local protocol or policy differs", "free_text_required": False},
    "R8": {"label": "Disagree with the system's reasoning", "free_text_required": False},
    "R9": {"label": "Other", "free_text_required": True},
}


@dataclass
class ClinicianDecision:
    actor: str
    actor_role: ActorRole
    assigned_level: int
    timestamp_min: float
    latency_from_display_min: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "actor": self.actor,
            "actor_role": self.actor_role.value,
            "assigned_level": self.assigned_level,
            "timestamp_min": round(self.timestamp_min, 4),
            "latency_from_display_min": round(self.latency_from_display_min, 4),
        }


@dataclass
class OverrideRecord:
    """Blueprint 14.4 "What is logged": actor identity and role, timestamp,
    direction, from-level and to-level, reason category, free text, the exact input
    snapshot, THE AI'S STATED REASONS AT THAT MOMENT, rule/envelope/model versions,
    elapsed time from display to override, and the resulting queue state."""
    actor: str
    actor_role: ActorRole
    direction: OverrideDirection
    from_level: int
    to_level: int
    reason_category: Optional[str]
    free_text: Optional[str]
    timestamp_min: float
    elapsed_from_display_min: float
    ai_stated_reasons: List[str] = field(default_factory=list)
    red_flag_active_at_override: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "actor": self.actor,
            "actor_role": self.actor_role.value,
            "direction": self.direction.value,
            "from_level": self.from_level,
            "to_level": self.to_level,
            "reason_category": self.reason_category,
            "reason_label": (OVERRIDE_REASONS.get(self.reason_category, {}).get("label")
                             if self.reason_category else None),
            "free_text": self.free_text,
            "timestamp_min": round(self.timestamp_min, 4),
            "elapsed_from_display_min": round(self.elapsed_from_display_min, 4),
            "ai_stated_reasons": list(self.ai_stated_reasons),
            "red_flag_active_at_override": self.red_flag_active_at_override,
        }


class OverrideRefused(RuntimeError):
    """Blueprint 13.3: 'If the audit write fails, the override is REFUSED and the
    clinician is told.  No unlogged clinical change, ever.'  Also raised when a
    de-escalation arrives without its mandatory reason."""


def validate_override(direction: OverrideDirection, reason_category: Optional[str],
                      free_text: Optional[str], red_flag_active: bool) -> None:
    """ASYMMETRIC FRICTION - Blueprint 14.4.

    "Escalate: ONE TAP, reason optional.  De-escalate: reason category REQUIRED,
    then confirm - on the same screen, never a blocking modal.  The friction is
    asymmetric because THE RISK IS ASYMMETRIC."

    "Free text is additionally mandatory in TWO CASES ONLY: reason category
    'Other', and ANY DE-ESCALATION WHILE A HARD RED FLAG IS FIRING."
    """
    if direction == OverrideDirection.ESCALATE:
        return                              # one tap, nothing else required

    if not reason_category:
        raise OverrideRefused(
            "De-escalation requires a reason category. Blueprint 14.4: the friction "
            "is asymmetric because the risk is asymmetric."
        )
    if reason_category not in OVERRIDE_REASONS:
        raise OverrideRefused(
            f"Unknown reason category '{reason_category}'. Valid categories: "
            f"{sorted(OVERRIDE_REASONS)}"
        )
    needs_text = OVERRIDE_REASONS[reason_category]["free_text_required"] or red_flag_active
    if needs_text and not (free_text or "").strip():
        why = ("reason category 'Other'" if reason_category == "R9"
               else "a hard red flag is firing")
        raise OverrideRefused(
            f"Free text is mandatory because {why}. Blueprint 14.4."
        )


def build_audit_payload(recommendation: Dict[str, Any], patient_ref: str,
                        actor: str, actor_role: ActorRole, timestamp_min: float,
                        queue_state: Optional[Dict[str, Any]] = None,
                        clinician_decision: Optional[ClinicianDecision] = None,
                        override: Optional[OverrideRecord] = None,
                        reassessment_events: Optional[List[Dict[str, Any]]] = None,
                        mode: str = "NORMAL", occupancy_ratio: float = 0.0,
                        staffing: Optional[Dict[str, Any]] = None,
                        system_errors: Optional[List[str]] = None,
                        degradation_rung: str = "L0_FULL",
                        event_type: str = "recommendation",
                        clock_snapshot: Optional[Dict[str, Any]] = None
                        ) -> Dict[str, Any]:
    """Assemble the full field set.  Key order is deterministic so the hash of an
    unchanged decision is stable across runs - which is what makes the
    reproducibility guarantee checkable."""
    rec = recommendation
    return {
        "event_type": event_type,
        "patient_ref": patient_ref,
        "timestamp_min": round(timestamp_min, 4),
        "actor": actor,
        "actor_role": actor_role.value,
        "input_snapshot": rec.get("_input_snapshot", {}),
        "data_freshness_summary": rec.get("_freshness_summary", {}),
        "envelope_id": rec.get("envelope_id"),
        "envelope_version": rec.get("envelope_version"),
        "versions": {
            "engine_version": rec.get("engine_version"),
            "rule_version": rec.get("rule_version"),
            "model_version": rec.get("model_version"),
            "calibration_id": rec.get("calibration_id"),
            "alpha": rec.get("alpha"),
        },
        "fired_rules": rec.get("fired_rules", []),
        "risk_score": rec.get("risk_score"),
        "risk_components": rec.get("risk_components", {}),
        "uncertainty_class": rec.get("uncertainty_class"),
        "uncertainty_components": rec.get("uncertainty", {}),
        "prediction_set": rec.get("prediction_set", []),
        "acted_level": rec.get("acted_level"),
        "point_estimate_level": rec.get("point_estimate_level"),
        "missing_fields": rec.get("uncertainty", {}).get("missing_fields", {}),
        "recommendation": {
            "acted_level": rec.get("acted_level"),
            "routing_suggestion": rec.get("routing_suggestion"),
            "action_verb": rec.get("action_verb"),
            "dominant_reason": rec.get("dominant_reason"),
            "secondary_reasons": rec.get("secondary_reasons", []),
        },
        "ttl_assigned": rec.get("ttl_minutes"),
        "ttl_basis": rec.get("ttl_basis"),
        "ttl_candidates": rec.get("ttl_candidates", {}),
        "clinician_decision": clinician_decision.to_dict() if clinician_decision else None,
        "override": override.to_dict() if override else None,
        "resulting_queue_state": queue_state or {},
        "reassessment_events": reassessment_events or [],
        "mode": mode,
        "occupancy_ratio": round(occupancy_ratio, 4),
        "staffing": staffing or {},
        "system_errors": system_errors or [],
        "degradation_rung": degradation_rung,
        "clock": clock_snapshot or {},
        "synthetic_data": True,
    }
