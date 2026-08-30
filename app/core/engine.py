"""
triage_core - the eight-layer engine.  Blueprint 9.2, 20.

"The crown jewel.  All eight layers of clinical and queue logic, with no framework,
no database and no network.  Deterministic and therefore testable and reproducible.
This module is the product; everything else is packaging."

The layers are NOT merged into one opaque function.  Each is a separate call with a
separate return object, so each is testable independently:

    L0  run_layer0        integrity + identity gate
    L1  select_envelope   age selects the RULE SET, not a coefficient
    L2  run_layer2        deterministic hard rules; ML cannot veto
    L3  run_layer3        envelope-appropriate physiological read
    L4  uncertainty + conformal -> acted level = most acute member of the set
    L5  compute_ttl       min(protocol floor, risk, uncertainty, load)
    L6  build_queue       (called by the department, not per patient)
    L7  evaluate_triggers (called by the department, not per patient)
    L8  human decision    override + audit, in app/api and app/audit

Zero I/O.  Zero network.  Deterministic given (patient, profile, clock, versions).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.core import versions as V
from app.core.knowledge import Knowledge, load_knowledge
from app.core.models import (
    ACUITY_LEVELS, Contradiction, FiredRule, Patient, Recommendation,
    UncertaintyClass, clamp_level,
)
from app.clinical.layer0_integrity import IntegrityResult, run_layer0
from app.clinical.layer1_envelope import (
    EnvelopeSelection, critical_1_fields_for, select_envelope,
)
from app.clinical.layer2_redflags import RuleResult, dominant_rule, run_layer2
from app.clinical.layer3_risk import RiskRead, run_layer3
from app.clinical.pathways import (
    ComplaintMapping, map_complaint, open_pathway_clocks, routing_suggestion,
)
from app.models.deterioration import DeteriorationEstimator
from app.queue.ttl import TTLResult, apply_ttl, compute_ttl
from app.safety.degradation import DegradationState
from app.uncertainty.classes import ClassBehaviour, gap_chip, resolve_class
from app.uncertainty.components import compute_components
from app.uncertainty.contradictions import detect_all
from app.uncertainty.conformal import (
    CalibrationSet, ConformalResult, level_plausibility, predict_set,
)


# ASM.  The ceiling on how far an uncertainty condition may widen the prediction
# set beyond the calibrated quantile.  Blueprint 22.6 interpretive rule: "a high
# Escalation Premium is only a good result if the alert burden stayed inside
# capacity."  Without a cap, every uncertain patient saturates at level 1 and the
# worklist stops discriminating.
MAX_SET_WIDENING = 2


@dataclass
class EngineContext:
    """Everything the engine needs that is not the patient.  Passing this
    explicitly is what keeps the core free of globals and therefore reproducible."""
    kb: Knowledge
    profile: Dict[str, Any]
    now_min: float
    mode: str = "NORMAL"
    occupancy_ratio: float = 0.0
    degradation: DegradationState = field(default_factory=DegradationState)
    estimator: Optional[DeteriorationEstimator] = None
    calibrations: Dict[str, CalibrationSet] = field(default_factory=dict)
    alpha: Optional[float] = None
    staff_override: Optional[int] = None

    @property
    def effective_alpha(self) -> float:
        """Invariant I10: alpha may be LOWERED by a hospital, never raised above the
        universal ceiling.  Validated at profile load; clamped again here so a
        runtime override cannot loosen it either."""
        core = self.kb.core["conformal"]
        a = self.alpha if self.alpha is not None else \
            float(self.profile.get("conformal_alpha", core["alpha_default"]))
        return max(float(core["alpha_floor"]), min(float(core["alpha_ceiling"]), a))


@dataclass
class LayerTrace:
    """Per-layer outputs, retained so the basis view can show the engine's work and
    so each layer can be asserted independently in tests."""
    l0: Optional[IntegrityResult] = None
    l1: Optional[EnvelopeSelection] = None
    l2: Optional[RuleResult] = None
    l3: Optional[RiskRead] = None
    l4_class: Optional[ClassBehaviour] = None
    l4_conformal: Optional[ConformalResult] = None
    l5: Optional[TTLResult] = None
    mapping: Optional[ComplaintMapping] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "L0_integrity": self.l0.to_dict() if self.l0 else None,
            "L1_envelope": self.l1.to_dict() if self.l1 else None,
            "L2_rules": self.l2.to_dict() if self.l2 else None,
            "L3_risk": self.l3.to_dict() if self.l3 else None,
            "L4_class": self.l4_class.to_dict() if self.l4_class else None,
            "L4_conformal": self.l4_conformal.to_dict() if self.l4_conformal else None,
            "L5_ttl": self.l5.to_dict() if self.l5 else None,
            "complaint_mapping": self.mapping.to_dict() if self.mapping else None,
        }


# ---------------------------------------------------------------------------

def evaluate(patient: Patient, ctx: EngineContext,
             change_reason: Optional[str] = None) -> Tuple[Recommendation, LayerTrace]:
    """Run L0-L5 for one patient and produce a versioned recommendation.

    Blueprint 12.3 step 3: "Full recomputation L2..L6 produces a NEW VERSIONED
    recommendation.  The previous recommendation is RETAINED, NEVER OVERWRITTEN."
    """
    kb, now = ctx.kb, ctx.now_min
    trace = LayerTrace()

    # --- complaint mapping (deterministic; no LLM) -------------------------
    mapping = map_complaint(patient.stated_chief_complaint)
    patient.complaint_concepts = list(mapping.concepts)
    patient.complaint_ambiguous = mapping.ambiguous
    patient.complaint_unmapped = mapping.unmapped
    trace.mapping = mapping

    # --- L1 provisional (needed by L0 for age-dependent plausibility bounds) -
    provisional = select_envelope(patient, kb)

    # --- L0 integrity + identity gate --------------------------------------
    trace.l0 = run_layer0(
        patient, kb, now,
        envelope_hint=provisional.primary_id,
        history_available=ctx.degradation.history_healthy,
    )

    # --- L1 envelope selection (re-run: identity may have changed the picture) -
    selection = select_envelope(patient, kb)
    trace.l1 = selection

    # --- L3 physiological read (needed before L2, which tests news2 facts) ---
    risk = run_layer3(patient, selection, kb)
    trace.l3 = risk

    # --- L2 deterministic hard rules ---------------------------------------
    rules = run_layer2(patient, selection, kb, now, risk.as_news2_facts(), mapping)
    trace.l2 = rules

    # --- time-critical pathway clocks (outside the optimiser's authority) ---
    for clock in open_pathway_clocks(patient, mapping, kb, now, ctx.profile):
        patient.pathway_clocks.append(clock)

    # --- L4 uncertainty ----------------------------------------------------
    contradictions = detect_all(patient, selection, risk, mapping, now)
    comp = compute_components(
        patient, selection, kb, now, risk, mapping, contradictions,
        model_available=ctx.degradation.model_healthy,
        calibration_populated=bool(ctx.calibrations),
    )
    behaviour = resolve_class(patient, selection, comp, contradictions, kb,
                              forced_by_rule=rules.forced_uncertainty_class)
    trace.l4_class = behaviour

    # --- bounded learned layer ---------------------------------------------
    deterioration: Optional[float] = None
    if ctx.estimator is not None and ctx.degradation.model_healthy:
        deterioration = ctx.estimator.estimate(
            patient, selection, risk, now, ctx.occupancy_ratio,
            missing_critical_count=len(comp.missing_critical1),
            staleness_fraction=comp.u2_freshness,
        )

    # --- L4 conformal escalation -------------------------------------------
    plaus = level_plausibility(
        risk.score,
        deterioration=deterioration,
        uncertainty_composite=comp.composite(),
    )
    calibration = ctx.calibrations.get(selection.primary_id) or \
        ctx.calibrations.get("adult")
    # Blueprint 9.5 table B: a "removal of reassurance" means the benign levels
    # are no longer well supported.  Mechanically that is a WIDER SET toward the
    # acute side - never a bigger number on the gradient.  This is the line that
    # makes the geriatric case act on level 2 while the point estimate still reads
    # the raw numbers as level 4.
    # Widenings are combined by MAX and capped, not summed.  Each condition
    # states a MINIMUM set width it needs in order to be honest; the set must
    # satisfy the most demanding one.  Summing them would escalate on the NUMBER of
    # concerns rather than on their severity, and "a system that escalates
    # everything is trivially safe and useless" (Blueprint 22.6). [ASM]
    reassurance_widen = 1 if risk.set_widening > 0 else 0
    widen = min(MAX_SET_WIDENING,
                max(behaviour.widen_set_toward_acute, reassurance_widen))
    # A COMPLAINT PATHWAY floor counts as a rule floor even when no vital-based
    # rule fired.  Blueprint scenario S-21 exists precisely for this: a stroke
    # patient's NEWS2 is close to zero, and the complaint pathway must fire
    # INDEPENDENTLY of physiology or the physiological score misses the case.
    deterministic_floor = None
    if rules.fired or rules.pathway_floor_level is not None:
        deterministic_floor = rules.floor_level

    conformal = predict_set(
        plaus, calibration, ctx.effective_alpha,
        widen_toward_acute=widen,
        rule_floor=deterministic_floor,
        widen_acute_bound=deterministic_floor,
    )
    trace.l4_conformal = conformal

    acted = conformal.acted_level

    # INVARIANT I9 / I4: the rule floor is applied AFTER set selection.  No model
    # output, confidence value or uncertainty class can suppress it.
    if deterministic_floor is not None and deterministic_floor < acted:
        acted = deterministic_floor

    # Blueprint 10.3 CONFLICTED: "The model's output MAY NOT BE USED TO LOWER
    # ANYTHING while this class holds."
    model_rule_disagreement = False
    if deterioration is not None and rules.fired:
        if conformal.point_estimate_level > rules.floor_level + 1:
            model_rule_disagreement = True

    acted = clamp_level(acted)

    # --- L5 TTL -------------------------------------------------------------
    ttl = compute_ttl(
        patient, selection, kb, acted, risk.score, comp, behaviour,
        conformal.set_width, ctx.occupancy_ratio, ctx.mode, ctx.profile, now,
        rule_ttl_floor=rules.ttl_floor_minutes,
        degradation_rung=ctx.degradation.rung,
    )
    trace.l5 = ttl

    # --- routing (capability-filtered, recommend-only) ----------------------
    routing = routing_suggestion(acted, mapping, ctx.profile, selection.primary_id,
                                obstetric=selection.obstetric_pathway)

    # --- explanation --------------------------------------------------------
    dominant, secondary = build_reasons(
        patient, selection, rules, risk, comp, behaviour, conformal,
        contradictions, mapping, acted,
    )

    snapshot = patient.snapshot(now)
    snapshot_hash = hashlib.sha256(
        __import__("json").dumps(snapshot, sort_keys=True, separators=(",", ":"),
                                 default=str).encode("utf-8")
    ).hexdigest()

    rec = Recommendation(
        version=len(patient.recommendation_versions) + 1,
        patient_ref=patient.patient_ref,
        created_at_min=now,
        acted_level=acted,
        point_estimate_level=conformal.point_estimate_level,
        prediction_set=conformal.prediction_set,
        rule_floor_level=rules.floor_level if rules.fired else None,
        uncertainty_class=behaviour.uncertainty_class,
        uncertainty=comp,
        contradictions=contradictions,
        dominant_reason=dominant,
        secondary_reasons=secondary,
        action_verb=_action_verb(rules, acted, behaviour),
        routing_suggestion=routing["suggestion"],
        routing_blocked_reason=routing["blocked_reason"],
        transfer_consideration=routing["transfer_consideration"],
        envelope_id=selection.primary_id,
        envelope_version=selection.version,
        envelope_note=selection.display_name,
        fired_rules=rules.fired,
        risk_score=risk.score,
        risk_components=risk.to_dict(),
        risk_suppressed=risk.suppressed,
        risk_suppression_reason=risk.suppression_reason,
        deterioration_estimate=deterioration,
        model_used=(ctx.estimator is not None and ctx.degradation.model_healthy),
        model_rule_disagreement=model_rule_disagreement,
        ttl_minutes=ttl.ttl_minutes,
        ttl_basis=ttl.basis,
        ttl_candidates=ttl.candidates,
        ttl_expires_at_min=ttl.expires_at_min,
        pathway_clocks=[_pathway_dict(pc, now) for pc in patient.pathway_clocks],
        escalation_premium=(conformal.escalation_premium
                            if conformal.set_width > 1 else 0.0),
        change_reason=change_reason,
        engine_version=V.ENGINE_VERSION,
        rule_version=V.RULE_SET_VERSION,
        model_version=(V.MODEL_VERSION if ctx.degradation.model_healthy else "offline"),
        calibration_id=(calibration.calibration_id if calibration else "none"),
        alpha=ctx.effective_alpha,
        degradation_rung=ctx.degradation.rung,
        operating_mode=ctx.mode,
        snapshot_hash=snapshot_hash,
        input_snapshot=snapshot,
    )

    # --- open the data-completion task the THIN class demands ---------------
    if behaviour.opens_data_completion_task and comp.missing_critical1:
        from app.core.models import OpenTask
        for f in comp.missing_critical1:
            patient.add_task(OpenTask(
                task_id=f"{patient.patient_ref}:measure:{f}",
                kind="measure", field_name=f, opened_at_min=now,
                deadline_min=now + min(ttl.ttl_minutes, 15.0),
                reason=f"{f} not measured - decision-critical for this envelope",
            ))

    # Blueprint 12.3 step 3: nothing is overwritten.
    patient.recommendation_versions.append(rec)
    return rec, trace


# ---------------------------------------------------------------------------
# Explanation.  Blueprint complexity 7: contrastive, closed vocabulary, <=12 words.
# ---------------------------------------------------------------------------

def build_reasons(patient: Patient, selection: EnvelopeSelection, rules: RuleResult,
                  risk: RiskRead, comp, behaviour: ClassBehaviour,
                  conformal: ConformalResult, contradictions: List[Contradiction],
                  mapping: ComplaintMapping, acted: int) -> Tuple[str, List[str]]:
    """"Explanations are CONTRASTIVE: 'level 2 not 3 because SpO2 89%', which is how
    humans actually accept or reject a claim."

    Every string returned here comes from the closed vocabulary in the rule file,
    the envelope files, or the fixed templates below.  Nothing is generated.
    """
    secondary: List[str] = []

    top = dominant_rule(rules)
    next_level = min(5, acted + 1)

    if top is not None:
        dominant = f"Level {acted} not {next_level} - {top.reason_string.lower()}"
    elif risk.suppressed and selection.primary_id == "pregnancy":
        dominant = "Usual early-warning score not valid in pregnancy"
    elif selection.primary_id == "unknown_age":
        dominant = "Age unknown - conservative envelope"
    elif contradictions:
        dominant = contradictions[0].card_text
    elif comp.missing_critical1:
        f = comp.missing_critical1[0]
        obs = patient.observations.get(f)
        reason = (obs.missing_reason.value.replace("_", " ")
                  if obs is not None and obs.missing_reason else "never measured")
        dominant = f"{_pretty(f)} {reason}"
    elif conformal.set_width > 1:
        dominant = f"Acting on worst of {{{', '.join(str(l) for l in conformal.prediction_set)}}}"
    else:
        dominant = f"Level {acted} - physiology within envelope"

    # --- secondary lines, one tap away on the card -------------------------
    if conformal.set_width > 1:
        secondary.append(
            f"acting on worst of {{{', '.join(str(l) for l in conformal.prediction_set)}}}"
        )
    if selection.dual_scored and selection.dual_reason:
        secondary.append(selection.dual_reason)
    for rr in risk.reassurance_removed:
        secondary.append(rr)
    for c in contradictions:
        secondary.append(f"{behaviour.uncertainty_class.value} - {c.card_text}")
    chip = gap_chip(comp)
    if chip:
        secondary.append(chip)
    if comp.reassurance_is_self_reported_only:
        secondary.append("reassurance is self-reported only")
    if risk.suppressed and risk.suppression_reason:
        secondary.append(risk.suppression_reason)
    for r in rules.fired[1:4]:
        secondary.append(r.reason_string)
    if rules.pathway_reason:
        secondary.append(rules.pathway_reason)
    return dominant, secondary


def _action_verb(rules: RuleResult, acted: int, behaviour: ClassBehaviour) -> str:
    """"The card ends with something TO DO, not something to know."  One verb."""
    top = dominant_rule(rules)
    if top is not None:
        return top.action_verb
    if behaviour.uncertainty_class == UncertaintyClass.BLIND:
        return "Direct observation now"
    if behaviour.uncertainty_class == UncertaintyClass.CONFLICTED:
        return "Verify the conflicting values"
    if behaviour.uncertainty_class == UncertaintyClass.THIN:
        return "Take the missing observation"
    return {1: "Move to resuscitation now", 2: "Move to monitored area",
            3: "Assess in majors", 4: "Assess when free",
            5: "Routine assessment"}[acted]


def _pretty(field_name: str) -> str:
    return {
        "spo2": "SpO2", "systolic_bp": "BP", "heart_rate": "HR",
        "respiratory_rate": "RR", "consciousness_acvpu": "ACVPU",
        "temperature_c": "Temperature", "gcs": "GCS",
        "work_of_breathing": "Work of breathing",
        "capillary_refill_seconds": "Capillary refill",
    }.get(field_name, field_name)


def _pathway_dict(pc, now_min: float) -> Dict[str, Any]:
    return {
        "pathway_id": pc.pathway_id,
        "name": pc.name,
        "window_minutes": pc.window_minutes,
        "remaining_minutes": (round(pc.remaining_minutes(now_min), 1)
                              if pc.remaining_minutes(now_min) is not None else None),
        "elapsed_minutes": round(pc.elapsed_minutes(now_min), 1),
        "origin_is_known": pc.origin_is_known,
        "required_capabilities": list(pc.required_capabilities),
        "capability_available": pc.capability_available,
        "transfer_consideration": pc.transfer_consideration,
        "source": pc.source,
        "note": ("The clock belongs to the disease, not the queue - displayed "
                 "independently of the TTL and outside the optimiser's authority."),
    }


# ---------------------------------------------------------------------------
# Freshness summary for the audit record - Blueprint 15.1
# ---------------------------------------------------------------------------

def freshness_summary(patient: Patient, kb: Knowledge, now_min: float
                      ) -> Dict[str, Any]:
    """"'The vitals were 40 minutes old' is often the whole explanation for an
    adverse event." """
    out: Dict[str, Any] = {}
    for name in sorted(patient.observations):
        obs = patient.observations[name]
        if obs.value is None:
            continue
        hl = kb.half_life(name)
        out[name] = {
            "age_min": round(obs.age_minutes(now_min), 2),
            "half_life_min": hl,
            "weight": round(obs.freshness_weight(now_min, hl), 4),
            "condition": obs.condition(now_min, hl).value,
        }
    return out


def commit_ttl(patient: Patient, rec: Recommendation, ctx: EngineContext) -> float:
    """Apply the computed TTL through the single guarded write path (invariant I1).

    Separated from evaluate() so that a caller can evaluate WITHOUT mutating the
    patient's clock - which is exactly what the I3 ablation property test needs.
    """
    from app.queue.ttl import TTLResult as _T
    result = _T(ttl_minutes=rec.ttl_minutes or 0.0, basis=rec.ttl_basis,
                candidates=rec.ttl_candidates,
                expires_at_min=rec.ttl_expires_at_min or 0.0)
    applied = apply_ttl(patient, result, ctx.now_min)
    rec.ttl_minutes = applied
    rec.ttl_expires_at_min = ctx.now_min + applied
    rec.ttl_basis = result.basis
    return applied


def make_context(profile_id: str = "H-L", now_min: float = 0.0,
                 kb: Optional[Knowledge] = None, **kwargs) -> EngineContext:
    kb = kb or load_knowledge()
    try:
        profile = kb.profile(profile_id)
    except Exception:
        profile = kb.conservative_default_profile()
    return EngineContext(kb=kb, profile=profile, now_min=now_min, **kwargs)
