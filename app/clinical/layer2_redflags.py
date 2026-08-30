"""
L2 - Hard safety rules.  Blueprint 9.2.

"Deterministic, transparent, clinically owned. It exists because rare catastrophic
events defeat statistical models, and because a nurse must be able to see the rule
that fired.  NO LEARNED COMPONENT MAY VETO THIS LAYER."

Blueprint 13.1 invariant I4: any firing hard red flag places the patient in queue
class R regardless of every model output, confidence value and uncertainty class.

The rules themselves live in rules/red_flags/hard_rules.json as a clinically-owned,
versioned, citable artefact.  This module is the deterministic INTERPRETER for that
artefact - it holds no thresholds of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.knowledge import Knowledge
from app.core.models import FiredRule, Patient, clamp_level
from app.clinical.layer1_envelope import EnvelopeSelection
from app.clinical.pathways import HIGH_RISK_PATHWAYS, ComplaintMapping, pathway_floor


@dataclass
class RuleResult:
    fired: List[FiredRule] = field(default_factory=list)
    floor_level: int = 5                       # 5 == least acute; rules only lower it
    forced_uncertainty_class: Optional[str] = None
    ttl_floor_minutes: Optional[float] = None
    escalate_only_rules: List[str] = field(default_factory=list)
    pathway_floor_level: Optional[int] = None
    pathway_reason: Optional[str] = None

    @property
    def any_hard_flag(self) -> bool:
        """Class R membership.  Escalate-only advisory rules (carer concern,
        missing-vital nudge) do NOT by themselves constitute a hard red flag - they
        floor the level but do not claim a resuscitation-grade finding."""
        return any(
            r.rule_id not in self.escalate_only_rules and r.floor_level <= 2
            for r in self.fired
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fired_rule_ids": [r.rule_id for r in self.fired],
            "floor_level": self.floor_level,
            "any_hard_flag": self.any_hard_flag,
            "forced_uncertainty_class": self.forced_uncertainty_class,
            "ttl_floor_minutes": self.ttl_floor_minutes,
            "pathway_floor_level": self.pathway_floor_level,
        }


# ---------------------------------------------------------------------------
# Derived facts.  Rules reference these by name; they are computed once so a rule
# file can stay declarative.
# ---------------------------------------------------------------------------

def build_fact_table(patient: Patient, selection: EnvelopeSelection, kb: Knowledge,
                     now_min: float, news2: Optional[Dict[str, Any]],
                     mapping: ComplaintMapping) -> Dict[str, Any]:
    """Every value a rule may test, resolved once, with quarantined values excluded.

    A quarantined value reads as None here - which is the point of quarantine: the
    rule layer neither trusts it nor pretends it was never taken.  The absence is
    then picked up by U1 completeness and RF-X03.
    """
    facts: Dict[str, Any] = {}

    for name, obs in patient.observations.items():
        facts[name] = None if obs.quarantined else obs.value

    facts["age_days"] = patient.age_days
    facts["age_known"] = patient.age_known
    facts["communication_barrier"] = patient.communication_barrier
    facts["self_report_channel_available"] = patient.self_report_channel_available
    facts["minutes_since_arrival"] = patient.minutes_since_arrival(now_min)
    facts["immunisations_incomplete"] = bool(patient.immunisations_incomplete)
    facts["spinal_cord_injury"] = patient.spinal_cord_injury

    # "no obvious source" for the paediatric fever rule: no localising concept.
    localising = {"urinary", "rash", "diarrhoea", "vomiting", "injury"}
    facts["no_obvious_source"] = not (set(mapping.concepts) & localising)

    # NEWS2-derived facts, only where the envelope permits NEWS2 at all.
    facts["news2_total"] = news2.get("total") if news2 else None
    facts["news2_max_single"] = news2.get("max_single") if news2 else None

    # Geriatric relative hypotension (Blueprint 9.6 "elderly, atypical").
    facts["relative_bp_drop_fraction"] = _relative_bp_drop(patient)

    # Geriatric new confusion against a documented oriented baseline.
    facts["new_confusion_vs_baseline"] = _new_confusion(patient)

    # Missing critical-1 count for RF-X03.
    from app.clinical.layer1_envelope import critical_1_fields_for
    crit = critical_1_fields_for(selection, patient, kb)
    missing = 0
    for f in crit:
        obs = patient.observations.get(f)
        if obs is None or obs.value is None or obs.quarantined:
            missing += 1
        else:
            hl = kb.half_life(f)
            if hl and obs.age_minutes(now_min) >= 2 * hl:
                missing += 1
    facts["missing_critical1_count"] = missing

    # on_oxygen guard for the hypoxaemia rules.
    facts["on_oxygen"] = bool(patient.value("on_oxygen"))
    return facts


def _relative_bp_drop(patient: Patient) -> Optional[float]:
    """Blueprint complexity 3 / scenario S-11: 'A BP of 104/60 is unremarkable in
    isolation and alarming against a usual 150/90.'

    Provenance rule P3 is enforced here: a baseline read from a PROVISIONAL record
    may RAISE risk (so the drop is still computed) but may never reassure, so a
    HIGH baseline that would make current BP look fine is not used to reassure -
    the function only ever returns a positive drop fraction.
    """
    baseline = patient.baseline_systolic_bp
    current = patient.value("systolic_bp")
    if baseline is None or current is None or baseline <= 0:
        return None
    drop = (baseline - current) / float(baseline)
    return max(0.0, drop)


def _new_confusion(patient: Patient) -> bool:
    acvpu = patient.value("consciousness_acvpu")
    if acvpu == "C":
        # Documented oriented baseline makes it NEW; absence of a baseline does not
        # make it old.  Unknown resolves toward more attention, never less.
        return patient.baseline_oriented is not False
    reported = patient.value("reported_new_confusion")
    if reported:
        return True
    return False


# ---------------------------------------------------------------------------
# Condition evaluation
# ---------------------------------------------------------------------------

_OPS: Dict[str, Callable[[Any, Any], bool]] = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
}


def _resolve_value(spec: Any, selection: EnvelopeSelection,
                   band: Optional[Dict[str, Any]]) -> Any:
    """'@band.hr_high' resolves against the ACTIVE AGE BAND, which is how a
    declarative rule file expresses an age-banded threshold without duplicating
    the table."""
    if isinstance(spec, str) and spec.startswith("@band."):
        if band is None:
            return None
        return band.get(spec.split(".", 1)[1])
    return spec


def _eval_condition(cond: Dict[str, Any], facts: Dict[str, Any],
                    selection: EnvelopeSelection,
                    band: Optional[Dict[str, Any]]) -> Tuple[bool, Dict[str, Any]]:
    """Returns (fired, triggering_values).  Missing facts NEVER fire a rule - a
    rule must not fire on ignorance; ignorance is handled by U1 and RF-X03."""
    triggering: Dict[str, Any] = {}

    if "all" in cond:
        for sub in cond["all"]:
            ok, tv = _eval_condition(sub, facts, selection, band)
            triggering.update(tv)
            if not ok:
                return False, {}
        return True, triggering

    if "any" in cond:
        for sub in cond["any"]:
            ok, tv = _eval_condition(sub, facts, selection, band)
            if ok:
                triggering.update(tv)
                return True, triggering
        return False, {}

    field_name = cond.get("field")
    op = cond.get("op")
    expected = _resolve_value(cond.get("value"), selection, band)
    actual = facts.get(field_name)

    if actual is None or expected is None:
        return False, {}

    guard = cond.get("guard")
    if guard:
        gok, _ = _eval_condition(guard, facts, selection, band)
        if not gok:
            return False, {}

    fn = _OPS.get(op)
    if fn is None:
        return False, {}
    try:
        fired = bool(fn(actual, expected))
    except TypeError:
        return False, {}

    if fired:
        triggering[field_name] = actual
        if isinstance(expected, (int, float)):
            triggering[f"{field_name}__threshold"] = expected
    return fired, triggering


# ---------------------------------------------------------------------------
# The layer
# ---------------------------------------------------------------------------

def run_layer2(patient: Patient, selection: EnvelopeSelection, kb: Knowledge,
               now_min: float, news2: Optional[Dict[str, Any]],
               mapping: ComplaintMapping) -> RuleResult:
    """Fire every applicable hard rule.  Deterministic, order-independent, and
    reproducible from the stored snapshot alone.

    For a DUAL-SCORED patient (adolescent 16-18, or estimated age near a band
    boundary), rules from BOTH envelopes are evaluated and the UNION of escalations
    is applied - the more acute result is acted on (Blueprint 9.5 table B).
    """
    result = RuleResult()
    facts = build_fact_table(patient, selection, kb, now_min, news2, mapping)

    bands = selection.age_bands_considered or ([selection.age_band] if selection.age_band else [None])
    applicable_envelopes = set(selection.envelopes) | {selection.primary_id}

    for rule in kb.red_flags["rules"]:
        if not (set(rule["envelopes"]) & applicable_envelopes):
            continue

        best_fire: Optional[Tuple[int, Dict[str, Any], Dict[str, Any]]] = None

        for band in bands:
            fired, triggering = _eval_condition(rule["condition"], facts, selection, band)
            if not fired:
                continue
            floor = int(rule["floor_level"])

            esc1 = rule.get("escalate_to_level_1_if")
            if esc1:
                ok, tv = _eval_condition(esc1, facts, selection, band)
                if ok:
                    floor = 1
                    triggering.update(tv)
            esc2 = rule.get("escalate_to_level_2_if")
            if esc2:
                ok, tv = _eval_condition(esc2, facts, selection, band)
                if ok:
                    floor = min(floor, 2)
                    triggering.update(tv)

            if band is not None:
                triggering["age_band"] = band.get("band_id")
            # Union of escalations: keep the MOST ACUTE outcome across bands.
            if best_fire is None or floor < best_fire[0]:
                best_fire = (floor, triggering, rule)

        if best_fire is None:
            continue

        floor, triggering, rule = best_fire
        fr = FiredRule(
            rule_id=rule["rule_id"],
            name=rule["name"],
            floor_level=floor,
            reason_string=rule["reason_string"],
            action_verb=rule["action_verb"],
            source=rule["source"],
            citation_text=rule.get("citation_text", ""),
            triggering_values=triggering,
        )
        result.fired.append(fr)
        result.floor_level = min(result.floor_level, floor)

        if rule.get("escalate_only"):
            result.escalate_only_rules.append(rule["rule_id"])
        if rule.get("forces_uncertainty_class"):
            result.forced_uncertainty_class = rule["forces_uncertainty_class"]
        if rule.get("ttl_floor_minutes") is not None:
            v = float(rule["ttl_floor_minutes"])
            result.ttl_floor_minutes = (
                v if result.ttl_floor_minutes is None
                else min(result.ttl_floor_minutes, v)
            )

    # ------------------------------------------------------------------
    # Candidate risk pathways fire INDEPENDENTLY of vitals.
    # Blueprint scenario S-21: NEWS2 for a stroke patient is close to zero.
    # ------------------------------------------------------------------
    pfloor = pathway_floor(mapping.pathways)
    if pfloor is not None:
        result.pathway_floor_level = pfloor
        result.floor_level = min(result.floor_level, pfloor)
        actives = sorted(p for p in mapping.pathways if p in HIGH_RISK_PATHWAYS)
        result.pathway_reason = f"complaint pathway active: {', '.join(actives)}"

    result.fired.sort(key=lambda r: (r.floor_level, r.rule_id))
    result.floor_level = clamp_level(result.floor_level)
    return result


def dominant_rule(result: RuleResult) -> Optional[FiredRule]:
    """The single most acute fired rule, used for the 5-second card's contrastive
    reason.  Blueprint 14.2: 'One dominant driver, phrased as the reason this level
    rather than the next one down.'"""
    if not result.fired:
        return None
    return result.fired[0]
