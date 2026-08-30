
"""
The five uncertainty components U1-U5 - Blueprint 10.1.

"Why five and not one: these components FAIL INDEPENDENTLY and DEMAND DIFFERENT
RESPONSES.  A patient with a complete, fresh, device-measured, coherent picture who
is simply unusual (high U5) needs a WIDER PREDICTION SET.  A patient with a
perfectly ordinary presentation whose entire picture is self-reported (high U3)
needs a SHORTER CLOCK and a re-look.  Collapsing them into a single percentage
discards exactly the information a nurse would use to decide what to do next."

Every component is in [0, 1] where HIGHER MEANS MORE UNCERTAIN, and every one has an
effect in ONE DIRECTION ONLY: shorter clocks, wider sets, higher class.  None of
them can reassure.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from app.core.knowledge import Knowledge
from app.core.models import (
    DataCondition, MatchState, MissingReason, Patient, Provenance,
    UncertaintyComponents, PROVENANCE_RANK, WEAK_REASSURANCE_CLASSES,
)
from app.clinical.layer1_envelope import EnvelopeSelection, critical_1_fields_for
from app.clinical.layer3_risk import RiskRead
from app.clinical.pathways import ComplaintMapping

# Field criticality weights, band-specific.  Blueprint complexity 5: "Per-field
# criticality weights, band-specific (SpO2 >> allergy)."  ASM values.
CRITICALITY_WEIGHTS: Dict[str, float] = {
    "consciousness_acvpu": 1.0,
    "spo2": 1.0,
    "respiratory_rate": 0.95,
    "heart_rate": 0.9,
    "systolic_bp": 0.85,
    "work_of_breathing": 0.9,
    "capillary_refill_seconds": 0.8,
    "temperature_c": 0.6,
    "gcs": 0.9,
    "blood_glucose_mgdl": 0.7,
    "pain_score": 0.3,
    "skin_perfusion": 0.5,
    "ambulatory": 0.35,
    "behavioural_state": 0.4,
    "visible_haemorrhage": 0.6,
    "medications": 0.1,
    "allergies": 0.1,
}
DEFAULT_WEIGHT = 0.3


def _weight(field_name: str) -> float:
    return CRITICALITY_WEIGHTS.get(field_name, DEFAULT_WEIGHT)


# ---------------------------------------------------------------------------
# U1 - Completeness
# ---------------------------------------------------------------------------

def compute_u1(patient: Patient, selection: EnvelopeSelection, kb: Knowledge,
               now_min: float, comp: UncertaintyComponents) -> float:
    """"How much of the decision-critical picture exists AT ALL, for THIS PATIENT'S
    ENVELOPE - not for the schema in general."

    Two-tier: an aggregate score, PLUS a hard rule that any absent critical-1 field
    forces THIN regardless of the aggregate.  Blueprint 7.4: "The second clause is
    the important one: a single absent critical field cannot be outvoted by a high
    overall completeness score.  Aggregate measures hide exactly the gap that
    matters."
    """
    critical = critical_1_fields_for(selection, patient, kb)
    scored = list(critical) + [
        f for f in ("temperature_c", "pain_score", "gcs", "blood_glucose_mgdl")
        if f not in critical
    ]

    total_w = 0.0
    have_w = 0.0

    for field_name in scored:
        obs = patient.observations.get(field_name)
        w = _weight(field_name)

        if obs is not None and obs.missing_reason == MissingReason.NOT_APPLICABLE:
            # Blueprint 8.7: "'Not applicable' is the ONLY reason that carries no
            # penalty, and it is only valid where the envelope says so."
            continue

        total_w += w

        if obs is None or obs.value is None or obs.quarantined:
            reason = (obs.missing_reason.value if obs is not None and obs.missing_reason
                      else MissingReason.NOT_YET_TAKEN.value)
            if obs is not None and obs.quarantined:
                reason = "quarantined_unreliable"
            comp.missing_fields[field_name] = reason
            if field_name in critical:
                comp.missing_critical1.append(field_name)
            continue

        hl = kb.half_life(field_name)
        cond = obs.condition(now_min, hl)
        if cond == DataCondition.ABSENT:
            # Past 2x half-life it BECOMES absent (Blueprint 8.7).
            comp.missing_fields[field_name] = "stale_beyond_2x_half_life"
            if field_name in critical:
                comp.missing_critical1.append(field_name)
            continue

        have_w += w

    completeness = 1.0 if total_w <= 0 else have_w / total_w
    comp.completeness_score = completeness
    return max(0.0, min(1.0, 1.0 - completeness))


# ---------------------------------------------------------------------------
# U2 - Freshness
# ---------------------------------------------------------------------------

def compute_u2(patient: Patient, selection: EnvelopeSelection, kb: Knowledge,
               now_min: float, comp: UncertaintyComponents) -> float:
    """"Whether the values being reasoned over describe the patient NOW or the
    patient half an hour ago."

    Per-field age against its clinical half-life (Blueprint 8.6), aggregated with
    the same criticality weights.  Blueprint invariant: staleness may ONLY SHORTEN
    a clock.
    """
    critical = critical_1_fields_for(selection, patient, kb)
    total_w = 0.0
    stale_w = 0.0

    for field_name in critical + ["temperature_c", "pain_score"]:
        obs = patient.observations.get(field_name)
        if obs is None or obs.value is None:
            continue
        hl = kb.half_life(field_name)
        if hl is None or hl <= 0:
            continue
        w = _weight(field_name)
        total_w += w
        age = obs.age_minutes(now_min)
        if age > hl:
            comp.stale_fields[field_name] = age
        # Decay toward UNKNOWN: 1 - freshness_weight is the fraction of this value's
        # authority that has expired.
        stale_w += w * (1.0 - obs.freshness_weight(now_min, hl))

    if total_w <= 0:
        return 0.0
    return max(0.0, min(1.0, stale_w / total_w))


# ---------------------------------------------------------------------------
# U3 - Provenance quality
# ---------------------------------------------------------------------------

def compute_u3(patient: Patient, selection: EnvelopeSelection, kb: Knowledge,
               now_min: float, comp: UncertaintyComponents,
               risk: RiskRead) -> float:
    """"What class of evidence the picture rests on ... and the identity confidence
    behind any record-derived component."

    Weighted position in the provenance lattice (Blueprint 8.5), with RULE P1
    applied as a HARD GATE on reaching CLEAR.  Provenance is NEVER a risk discount.
    """
    max_rank = float(max(PROVENANCE_RANK.values()))
    total_w = 0.0
    rank_w = 0.0
    reassuring_sources: Set[Provenance] = set()

    critical = critical_1_fields_for(selection, patient, kb)
    for field_name in critical + ["temperature_c", "pain_score"]:
        obs = patient.observations.get(field_name)
        if obs is None or obs.value is None or obs.quarantined:
            continue
        w = _weight(field_name)
        total_w += w
        rank = PROVENANCE_RANK.get(obs.provenance, max_rank)
        rank_w += w * (rank / max_rank)
        if obs.provenance in WEAK_REASSURANCE_CLASSES:
            comp.weak_provenance_fields.append(field_name)
        reassuring_sources.add(obs.provenance)

    # Quality flags feed U3 as well (Blueprint section 5 item 14).
    artefact_penalty = 0.0
    n_quality = 0
    for field_name in critical:
        obs = patient.observations.get(field_name)
        if obs is None or obs.value is None:
            continue
        n_quality += 1
        if obs.quality.value == "artefact":
            artefact_penalty += 1.0
        elif obs.quality.value == "suspect":
            artefact_penalty += 0.5
    quality_component = (artefact_penalty / n_quality) if n_quality else 0.0

    # Identity confidence.  A PROVISIONAL match is a source of uncertainty in its
    # own right (Blueprint complexity 6).
    identity_component = 0.0
    if patient.identity.match_state == MatchState.PROVISIONAL:
        identity_component = 0.6
    elif patient.identity.match_state == MatchState.UNMATCHED and patient.record_id:
        identity_component = 0.3

    base = (rank_w / total_w) if total_w > 0 else 0.5

    # RULE P1 (Blueprint 8.5): a recommendation whose REASSURING evidence rests only
    # on {Pt, Att, Unk, Rec*} can NEVER reach CLEAR.  We detect that state here and
    # the class resolver enforces the gate.
    if reassuring_sources and reassuring_sources.issubset(WEAK_REASSURANCE_CLASSES):
        # Only meaningful when the picture is actually reassuring - a deranged
        # patient is not being reassured by anything.
        if not risk.suppressed and risk.score <= 0.3:
            comp.reassurance_is_self_reported_only = True

    if patient.communication_barrier and not patient.self_report_channel_available:
        # The self-report channel is scored as ABSENT, not as negative findings
        # (Blueprint section 5 item 1).
        base = max(base, 0.7)

    return max(0.0, min(1.0, max(base, quality_component, identity_component)))


# ---------------------------------------------------------------------------
# U4 - Coherence
# ---------------------------------------------------------------------------

def compute_u4(contradictions: List[Any]) -> float:
    """"Whether the inputs contradict each other.  This is the component most
    systems lack and the one that carries the most information."

    Any firing detector sets CONFLICTED (handled by the class resolver); the
    magnitude scales with how many independent detectors agree that something is
    wrong.
    """
    if not contradictions:
        return 0.0
    distinct = len({c.detector_id for c in contradictions})
    return min(1.0, 0.5 + 0.15 * distinct)


# ---------------------------------------------------------------------------
# U5 - Model applicability
# ---------------------------------------------------------------------------

def compute_u5(patient: Patient, selection: EnvelopeSelection, risk: RiskRead,
               mapping: ComplaintMapping, comp: UncertaintyComponents,
               model_available: bool = True,
               calibration_populated: bool = True) -> float:
    """"Whether this patient is INSIDE the population the model was calibrated on -
    including HARD APPLICABILITY RULES (age band, pregnancy, NEWS2 exclusions) and a
    DISTRIBUTIONAL CHECK."

    Out of distribution -> the model's contribution is down-weighted toward the
    deterministic floor and the SET WIDENS.
    """
    score = 0.0

    for exclusion in selection.applicability_exclusions:
        comp.applicability_exclusions.append(exclusion)
        score = max(score, 0.85 if exclusion in ("age_unknown",) else 0.6)

    if selection.aggregate_score_suppressed:
        comp.applicability_exclusions.append("aggregate_score_suppressed")
        score = max(score, 0.7)

    if not selection.news2_permitted and selection.primary_id in ("pregnancy", "unknown_age"):
        comp.applicability_exclusions.append("news2_not_valid_for_this_patient")
        score = max(score, 0.7)

    if mapping.unmapped:
        # Blueprint complexity 1 fallback: unmapped complaint -> THIN.
        comp.applicability_exclusions.append("complaint_unmapped")
        score = max(score, 0.55)

    if mapping.ambiguous:
        score = max(score, 0.4)

    if not model_available:
        # Degradation rung L1.  The set collapses to the conservative default and
        # applicability uncertainty is explicit rather than hidden.
        comp.applicability_exclusions.append("model_offline")
        score = max(score, 0.5)

    if not calibration_populated:
        comp.applicability_exclusions.append("insufficient_calibration_data")
        score = max(score, 0.6)

    if patient.spinal_cord_injury:
        comp.applicability_exclusions.append("spinal_cord_injury_caveat")
        score = max(score, 0.45)

    if risk.components_missing:
        # A partially-computed aggregate is a partially-applicable model.
        score = max(score, min(0.5, 0.12 * len(risk.components_missing)))

    if score > 0.5:
        comp.out_of_distribution = True
    return max(0.0, min(1.0, score))


# ---------------------------------------------------------------------------

def compute_components(patient: Patient, selection: EnvelopeSelection,
                       kb: Knowledge, now_min: float, risk: RiskRead,
                       mapping: ComplaintMapping, contradictions: List[Any],
                       model_available: bool = True,
                       calibration_populated: bool = True) -> UncertaintyComponents:
    comp = UncertaintyComponents()
    comp.u1_completeness = compute_u1(patient, selection, kb, now_min, comp)
    comp.u2_freshness = compute_u2(patient, selection, kb, now_min, comp)
    comp.u3_provenance = compute_u3(patient, selection, kb, now_min, comp, risk)
    comp.u4_coherence = compute_u4(contradictions)
    comp.u5_model_applicability = compute_u5(
        patient, selection, risk, mapping, comp,
        model_available=model_available,
        calibration_populated=calibration_populated,
    )
    comp.missing_critical1 = sorted(set(comp.missing_critical1))
    comp.weak_provenance_fields = sorted(set(comp.weak_provenance_fields))
    comp.applicability_exclusions = sorted(set(comp.applicability_exclusions))
    return comp
