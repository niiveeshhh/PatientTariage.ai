"""
The four uncertainty classes and their MANDATORY behaviours - Blueprint 10.3.

"'Confidence: 87%' satisfies the LETTER of [Round 2's requirement] and defeats its
purpose - a percentage invites the nurse to read it as accuracy, and a high number
next to a low risk is precisely how a system manufactures false reassurance."

Class precedence: BLIND > CONFLICTED > THIN > CLEAR (Blueprint 21.1).
Each class carries a DEFINED, TESTABLE system behaviour - mandatory, not advisory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.core.knowledge import Knowledge
from app.core.models import (
    Contradiction, MatchState, Patient, UncertaintyClass, UncertaintyComponents,
    worse_uncertainty,
)
from app.clinical.layer1_envelope import EnvelopeSelection


@dataclass
class ClassBehaviour:
    """The mandatory behaviour attached to a class.  Nothing here is advisory."""
    uncertainty_class: UncertaintyClass
    ttl_floor_minutes: Optional[float]
    widen_set_toward_acute: int          # extra levels added on the acute side
    opens_data_completion_task: bool
    raises_human_verification: bool
    model_may_lower_anything: bool
    queue_class_hint: Optional[str]      # "B" for BLIND
    auto_escalates_if_unacknowledged: bool
    marker: str
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uncertainty_class": self.uncertainty_class.value,
            "ttl_floor_minutes": self.ttl_floor_minutes,
            "widen_set_toward_acute": self.widen_set_toward_acute,
            "opens_data_completion_task": self.opens_data_completion_task,
            "raises_human_verification": self.raises_human_verification,
            "model_may_lower_anything": self.model_may_lower_anything,
            "queue_class_hint": self.queue_class_hint,
            "auto_escalates_if_unacknowledged": self.auto_escalates_if_unacknowledged,
            "marker": self.marker,
            "reasons": list(self.reasons),
        }


def resolve_class(patient: Patient, selection: EnvelopeSelection,
                  comp: UncertaintyComponents, contradictions: List[Contradiction],
                  kb: Knowledge, forced_by_rule: Optional[str] = None
                  ) -> ClassBehaviour:
    """Resolve the five components into one of four named states.

    The ordering below IS the precedence.  A patient can only ever be moved to a
    MORE uncertain class by any single condition - nothing in this function can
    lower a class that another condition has already raised.
    """
    reasons: List[str] = []
    cls = UncertaintyClass.CLEAR

    core = kb.core["uncertainty"]
    thin_threshold = float(core["completeness_thin_threshold"])

    # --- THIN ----------------------------------------------------------------
    if comp.missing_critical1:
        cls = worse_uncertainty(cls, UncertaintyClass.THIN)
        reasons.append(
            "missing decision-critical field: " + ", ".join(sorted(comp.missing_critical1))
        )
    if comp.completeness_score < thin_threshold:
        cls = worse_uncertainty(cls, UncertaintyClass.THIN)
        reasons.append(
            f"weighted completeness {comp.completeness_score:.2f} below {thin_threshold:.2f}"
        )
    if comp.stale_fields:
        cls = worse_uncertainty(cls, UncertaintyClass.THIN)
        oldest = max(comp.stale_fields.items(), key=lambda kv: kv[1])
        reasons.append(f"{oldest[0]} is {oldest[1]:.0f} min old, past its half-life")
    if patient.complaint_unmapped:
        cls = worse_uncertainty(cls, UncertaintyClass.THIN)
        reasons.append("complaint could not be mapped to any pathway")

    # Provenance rule P1 is a HARD GATE on reaching CLEAR (Blueprint 8.5, 10.1).
    if comp.reassurance_is_self_reported_only:
        cls = worse_uncertainty(cls, UncertaintyClass.THIN)
        reasons.append("reassurance is self-reported only - cannot reach CLEAR (rule P1)")

    if selection.forced_uncertainty_class == "THIN":
        cls = worse_uncertainty(cls, UncertaintyClass.THIN)
        reasons.append(selection.note or "envelope requires THIN")

    # --- CONFLICTED ----------------------------------------------------------
    if contradictions:
        cls = worse_uncertainty(cls, UncertaintyClass.CONFLICTED)
        for c in contradictions:
            reasons.append(f"contradiction: {c.card_text}")

    # --- BLIND ---------------------------------------------------------------
    blind_reasons: List[str] = []
    if patient.communication_barrier and not patient.self_report_channel_available:
        blind_reasons.append("no self-report channel and inadequate observation")
    if not patient.age_known:
        blind_reasons.append("age unknown")
    if (patient.identity.match_state == MatchState.UNMATCHED
            and patient.record_id is not None
            and not patient.observations):
        blind_reasons.append("unresolved identity with no direct data")
    if selection.forced_uncertainty_class == "BLIND":
        blind_reasons.append(selection.note or "hard model-applicability exclusion")
    if forced_by_rule == "BLIND":
        blind_reasons.append("hard rule forces BLIND")

    if blind_reasons:
        cls = worse_uncertainty(cls, UncertaintyClass.BLIND)
        reasons.extend(blind_reasons)

    return behaviour_for(cls, kb, reasons)


def behaviour_for(cls: UncertaintyClass, kb: Knowledge,
                  reasons: Optional[List[str]] = None) -> ClassBehaviour:
    """Blueprint 10.3 - the mandatory behaviour table, implemented literally."""
    reasons = reasons or []

    if cls == UncertaintyClass.CLEAR:
        return ClassBehaviour(
            uncertainty_class=cls,
            ttl_floor_minutes=None,        # protocol floor for the acted level applies
            widen_set_toward_acute=0,
            opens_data_completion_task=False,
            raises_human_verification=False,
            model_may_lower_anything=True,
            queue_class_hint=None,
            auto_escalates_if_unacknowledged=False,
            marker="green",
            reasons=reasons,
        )

    if cls == UncertaintyClass.THIN:
        return ClassBehaviour(
            uncertainty_class=cls,
            # "Clock TIGHTENED below the risk-derived value."
            ttl_floor_minutes=None,
            widen_set_toward_acute=1,
            opens_data_completion_task=True,
            raises_human_verification=False,
            model_may_lower_anything=True,
            queue_class_hint=None,
            auto_escalates_if_unacknowledged=False,
            marker="amber",
            reasons=reasons,
        )

    if cls == UncertaintyClass.CONFLICTED:
        return ClassBehaviour(
            uncertainty_class=cls,
            ttl_floor_minutes=kb.special_floor("conflicted_uncertainty"),
            # "Set includes every level consistent with EITHER side of the
            # contradiction."  One level from the acute edge of the base set spans
            # both interpretations; widening further would escalate on the NUMBER
            # of detectors rather than on the disagreement itself. [ASM]
            widen_set_toward_acute=1,
            opens_data_completion_task=False,
            raises_human_verification=True,
            # "The model's output MAY NOT BE USED TO LOWER ANYTHING while this
            # class holds."
            model_may_lower_anything=False,
            queue_class_hint=None,
            auto_escalates_if_unacknowledged=False,
            marker="red_outline",
            reasons=reasons,
        )

    # BLIND
    return ClassBehaviour(
        uncertainty_class=cls,
        # "Hard 10-minute clock floor REGARDLESS of how stable the vitals look."
        ttl_floor_minutes=kb.special_floor("blind_uncertainty"),
        widen_set_toward_acute=2,
        opens_data_completion_task=True,
        raises_human_verification=True,
        model_may_lower_anything=False,
        queue_class_hint="B",
        auto_escalates_if_unacknowledged=True,
        marker="black",
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# Card copy.  Blueprint 10.3 "What the nurse sees" column.
# ---------------------------------------------------------------------------

CLASS_CARD_COPY = {
    UncertaintyClass.CLEAR: "No gap.",
    UncertaintyClass.THIN: "Missing information.",
    UncertaintyClass.CONFLICTED: "Inputs disagree.",
    UncertaintyClass.BLIND: "Cannot assess.",
}


def gap_chip(comp: UncertaintyComponents) -> Optional[str]:
    """The persistent chip: "Missing: SpO2, BP".  Actionable, where
    'completeness 0.72' is not (Blueprint 14.2)."""
    if not comp.missing_critical1:
        return None
    pretty = {
        "spo2": "SpO2", "systolic_bp": "BP", "heart_rate": "HR",
        "respiratory_rate": "RR", "consciousness_acvpu": "ACVPU",
        "temperature_c": "temp", "gcs": "GCS",
        "work_of_breathing": "work of breathing",
        "capillary_refill_seconds": "cap refill",
    }
    names = [pretty.get(f, f) for f in comp.missing_critical1]
    return "Missing: " + ", ".join(names)
