"""
Scenario library loader - Blueprint 17.3.

Turns the declarative library in data/scenarios/scenarios.json into Patient objects,
and evaluates system output against each scenario's EXPECTED-BEHAVIOUR ENVELOPE.

Blueprint 21.3: "A scenario passes only if EVERY DIMENSION is satisfied.  Failures
are reported INDIVIDUALLY and named in the results - we will report WHICH SCENARIOS
FAILED rather than reporting a pass rate, because a pass rate hides which case broke."
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.core.knowledge import REPO_ROOT
from app.core.models import (
    AgeSource, ArrivalMode, CommunicationBarrier, IdentityLink, MatchState,
    MissingReason, Observation, Patient, PregnancyStatus, Provenance, Quality,
    absent,
)

SCENARIOS_PATH = os.path.join(REPO_ROOT, "data", "scenarios", "scenarios.json")

# Fields that are clinician-observed rather than device-measured.
_OBS_FIELDS = {
    "consciousness_acvpu", "work_of_breathing", "ambulatory", "skin_perfusion",
    "capillary_refill_seconds", "behavioural_state", "visible_haemorrhage",
    "major_mechanism", "diaphoresis", "guarding", "observed_distress",
    "clinician_gestalt_concern", "gcs",
}
_PT_FIELDS = {"pain_score", "reports_feels_fine", "patient_denies_confusion",
              "patient_denies_medication", "stated_onset_time_min"}
_ATT_FIELDS = {"carer_concern", "reported_new_confusion", "relative_reports_change"}


def load_library(path: str = SCENARIOS_PATH) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _provenance_for(field_name: str, override: Optional[str]) -> Provenance:
    if override:
        return Provenance(override)
    if field_name in _OBS_FIELDS:
        return Provenance.OBS
    if field_name in _PT_FIELDS:
        return Provenance.PT
    if field_name in _ATT_FIELDS:
        return Provenance.ATT
    return Provenance.DEV


def build_patient(scenario: Dict[str, Any], now_min: float = 0.0,
                  ref_suffix: str = "") -> Patient:
    """Construct the Patient exactly as the scenario declares it.

    A scenario value may be a bare value, or a dict carrying {"absent": reason},
    {"value": v, "age_min": n} for a deliberately stale reading, or
    {"value": v, "provenance": "Att"}.
    """
    demo = scenario["demographics"]
    hist = scenario.get("history", {})
    arrival = now_min + float(scenario.get("arrival_offset_min", 0.0))

    identity = IdentityLink(
        match_state=MatchState(hist.get("identity", "UNMATCHED")),
        identity_confidence=float(hist.get("identity_confidence", 0.0)),
        matched_fields=list(hist.get("matched_fields", [])),
        candidate_record_ids=list(hist.get("candidate_record_ids", [])),
    )

    patient = Patient(
        patient_ref=scenario["scenario_id"] + ref_suffix,
        arrival_timestamp_min=arrival,
        chair=hist.get("chair", ""),
        age_days=demo.get("age_days"),
        age_source=AgeSource(demo.get("age_source", "unknown")),
        age_estimated=(demo.get("age_source") == "estimated"),
        sex=demo.get("sex", "unknown"),
        arrival_mode=ArrivalMode(demo.get("arrival_mode", "walk_in")),
        record_id=(hist.get("candidate_record_ids") or [None])[0],
        identity=identity,
        stated_chief_complaint=scenario.get("stated_chief_complaint", ""),
        communication_barrier=bool(hist.get("communication_barrier", False)),
        communication_barrier_kind=CommunicationBarrier(
            hist.get("communication_barrier_kind", "none")),
        self_report_channel_available=bool(
            hist.get("self_report_channel_available", True)),
        pregnancy_status=PregnancyStatus(
            demo.get("pregnancy_status", "not_applicable")),
        prior_ed_visits_90d=hist.get("prior_ed_visits_90d"),
        known_conditions=list(hist.get("known_conditions", [])),
        baseline_systolic_bp=hist.get("baseline_systolic_bp"),
        baseline_bp_age_days=hist.get("baseline_bp_age_days"),
        baseline_oriented=hist.get("baseline_oriented"),
        rate_control_medication=hist.get("rate_control_medication"),
        frailty_indicator=hist.get("frailty_indicator"),
        immunisations_incomplete=hist.get("immunisations_incomplete"),
        spinal_cord_injury=bool(hist.get("spinal_cord_injury", False)),
        scenario_id=scenario["scenario_id"],
        _latent=scenario.get("latent_trajectory"),
    )

    for field_name, spec in scenario.get("observations", {}).items():
        patient.set_observation(_make_observation(field_name, spec, arrival))

    return patient


def _make_observation(field_name: str, spec: Any, arrival: float) -> Observation:
    if isinstance(spec, dict):
        if "absent" in spec:
            # Blueprint 8.7: missing carries a REASON. Never imputed.
            return absent(field_name, MissingReason(spec["absent"]), arrival)
        value = spec.get("value")
        age_min = float(spec.get("age_min", 0.0))
        prov = _provenance_for(field_name, spec.get("provenance"))
        quality = Quality(spec.get("quality", "clean"))
        ts = arrival - age_min
    else:
        value = spec
        prov = _provenance_for(field_name, None)
        quality = Quality.CLEAN
        ts = arrival

    if field_name == "stated_onset_time_min" and isinstance(value, (int, float)):
        value = arrival + float(value)      # negative offsets mean "before arrival"

    return Observation(
        field_name=field_name, value=value, provenance=prov,
        timestamp_min=ts, quality=quality,
        source_confidence=0.95 if prov in (Provenance.DEV, Provenance.OBS) else 0.6,
    )


def build_paired_patient(scenario: Dict[str, Any], now_min: float = 0.0
                         ) -> Optional[Patient]:
    """S-22 is ONE scenario containing TWO arrivals - the collision is the scenario."""
    paired = scenario.get("paired_arrival")
    if not paired:
        return None
    sub = dict(scenario)
    sub["demographics"] = paired["demographics"]
    sub["stated_chief_complaint"] = paired["stated_chief_complaint"]
    sub["observations"] = paired["observations"]
    sub["history"] = paired["history"]
    sub["arrival_offset_min"] = paired.get("arrival_offset_min", 0.0)
    return build_patient(sub, now_min, ref_suffix=paired.get("suffix", "b"))


# ---------------------------------------------------------------------------
# Expected-behaviour envelope evaluation
# ---------------------------------------------------------------------------

@dataclass
class ScenarioResult:
    scenario_id: str
    adversarial: bool
    passed: bool
    failures: List[str] = field(default_factory=list)
    observed: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "adversarial": self.adversarial,
            "passed": self.passed,
            "failures": list(self.failures),
            "observed": self.observed,
        }


def check_envelope(scenario: Dict[str, Any], rec, trace,
                   queue_class: Optional[str] = None,
                   triggers: Optional[List[Any]] = None,
                   patient: Optional[Patient] = None) -> ScenarioResult:
    """Assert the recommendation against every dimension of the envelope.

    Dimensions checked: acceptable acted-level range, required uncertainty class,
    required fired rules, required TTL bound, required queue class, required reason
    content (Blueprint 21.3).
    """
    exp = scenario.get("expected_behaviour_envelope", {})
    res = ScenarioResult(scenario_id=scenario["scenario_id"],
                         adversarial=scenario.get("adversarial", False),
                         passed=True)
    fired = [r.rule_id for r in rec.fired_rules]
    detectors = [c.detector_id for c in rec.contradictions]
    res.observed = {
        "acted_level": rec.acted_level,
        "point_estimate_level": rec.point_estimate_level,
        "prediction_set": rec.prediction_set,
        "uncertainty_class": rec.uncertainty_class.value,
        "envelope_id": rec.envelope_id,
        "fired_rules": fired,
        "contradiction_detectors": detectors,
        "ttl_minutes": rec.ttl_minutes,
        "queue_class": queue_class,
        "escalation_premium": rec.escalation_premium,
        "transfer_consideration": rec.transfer_consideration,
        "pathway_clocks": [p["pathway_id"] for p in rec.pathway_clocks],
    }

    def fail(msg: str) -> None:
        res.passed = False
        res.failures.append(msg)

    if "acted_level_range" in exp:
        lo, hi = exp["acted_level_range"]
        if not (lo <= rec.acted_level <= hi):
            fail(f"acted_level {rec.acted_level} outside envelope [{lo},{hi}]")

    if "acted_level_max" in exp and rec.acted_level > exp["acted_level_max"]:
        fail(f"acted_level {rec.acted_level} less acute than required max "
             f"{exp['acted_level_max']}")

    if "point_estimate_min" in exp and rec.point_estimate_level < exp["point_estimate_min"]:
        fail(f"point_estimate_level {rec.point_estimate_level} more acute than "
             f"expected minimum {exp['point_estimate_min']} - the case is supposed "
             f"to LOOK benign to a point estimate")

    if "uncertainty_class_in" in exp:
        if rec.uncertainty_class.value not in exp["uncertainty_class_in"]:
            fail(f"uncertainty_class {rec.uncertainty_class.value} not in "
                 f"{exp['uncertainty_class_in']}")

    for rid in exp.get("required_fired_rules", []):
        if rid not in fired:
            fail(f"required rule {rid} did not fire (fired: {fired})")

    if "required_fired_rules_any" in exp:
        if not (set(exp["required_fired_rules_any"]) & set(fired)):
            fail(f"none of {exp['required_fired_rules_any']} fired (fired: {fired})")

    for rid in exp.get("forbidden_fired_rules", []):
        if rid in fired:
            fail(f"forbidden rule {rid} fired")

    for d in exp.get("required_contradiction_detectors", []):
        if d not in detectors:
            fail(f"required contradiction detector {d} did not fire "
                 f"(fired: {detectors})")

    for d in exp.get("forbidden_contradiction_detectors", []):
        if d in detectors:
            fail(f"FALSE POSITIVE: contradiction detector {d} fired on an explained "
                 f"abnormality")

    if "required_envelope" in exp and rec.envelope_id != exp["required_envelope"]:
        fail(f"envelope {rec.envelope_id} != required {exp['required_envelope']}")

    if "ttl_max_minutes" in exp and rec.ttl_minutes is not None:
        if rec.ttl_minutes > exp["ttl_max_minutes"] + 1e-6:
            fail(f"TTL {rec.ttl_minutes:.1f} min exceeds envelope max "
                 f"{exp['ttl_max_minutes']} min")

    if "required_queue_class" in exp and queue_class is not None:
        if queue_class != exp["required_queue_class"]:
            fail(f"queue class {queue_class} != required {exp['required_queue_class']}")

    if "queue_class_in" in exp and queue_class is not None:
        if queue_class not in exp["queue_class_in"]:
            fail(f"queue class {queue_class} not in {exp['queue_class_in']}")

    if "prediction_set_equals" in exp:
        if sorted(rec.prediction_set) != sorted(exp["prediction_set_equals"]):
            fail(f"prediction set {rec.prediction_set} != "
                 f"{exp['prediction_set_equals']}")

    if "prediction_set_min_width" in exp:
        if len(rec.prediction_set) < exp["prediction_set_min_width"]:
            fail(f"prediction set width {len(rec.prediction_set)} below required "
                 f"minimum {exp['prediction_set_min_width']}")

    if "prediction_set_max_width" in exp:
        if len(rec.prediction_set) > exp["prediction_set_max_width"]:
            fail(f"prediction set width {len(rec.prediction_set)} above allowed "
                 f"maximum {exp['prediction_set_max_width']}")

    if "escalation_premium_min" in exp:
        if rec.escalation_premium < exp["escalation_premium_min"] - 1e-9:
            fail(f"escalation premium {rec.escalation_premium} below required "
                 f"{exp['escalation_premium_min']}")

    for pid in exp.get("required_pathway_clocks", []):
        if pid not in res.observed["pathway_clocks"]:
            fail(f"required time-critical pathway clock {pid} not opened")

    if "required_transfer_consideration_contains" in exp:
        needle = exp["required_transfer_consideration_contains"]
        text = rec.transfer_consideration or ""
        if needle.lower() not in text.lower():
            fail(f"transfer consideration missing '{needle}' (got: {text!r})")

    if triggers is not None:
        classes = {t.trigger_class for t in triggers}
        for tc in exp.get("required_trigger_classes", []):
            if tc not in classes:
                fail(f"required trigger class {tc} did not fire (fired: {sorted(classes)})")
        res.observed["trigger_classes"] = sorted(classes)

    if patient is not None:
        for kind in exp.get("required_open_task_kinds", []):
            if not any(t.kind == kind for t in patient.open_task_list()):
                fail(f"required open task of kind '{kind}' was not created")

        if exp.get("assert_bp_absence_is_not_applicable"):
            obs = patient.observations.get("systolic_bp")
            if obs is None or obs.missing_reason != MissingReason.NOT_APPLICABLE:
                fail("BP absence should be marked NOT_APPLICABLE in a small child, "
                     "with capillary refill substituting")

        if exp.get("assert_missing_reason_is_refused"):
            refused = [f for f, o in patient.observations.items()
                       if o.missing_reason == MissingReason.REFUSED]
            if not refused:
                fail("no field carries missing_reason=refused; refusal must be a "
                     "distinct reason, not generic absence")

        if exp.get("assert_impossible_value_quarantined_not_deleted"):
            obs = patient.observations.get("systolic_bp")
            if obs is None:
                fail("impossible BP was DELETED - it must be quarantined, not removed")
            elif not obs.quarantined:
                fail("impossible BP was not quarantined")
            elif obs.value is None:
                fail("impossible BP value was erased; quarantine must RETAIN the "
                     "reading so a reviewer can see what the device reported")

        if exp.get("assert_provisional_match_never_lowers_acted_acuity") or \
           exp.get("assert_provisional_record_cannot_reassure"):
            if patient.identity.may_reassure:
                fail("a PROVISIONAL identity match must not be permitted to reassure "
                     "(provenance rule P3)")

        if exp.get("assert_no_automatic_merge"):
            if len(patient.identity.candidate_record_ids) > 1 and \
                    patient.identity.match_state == MatchState.MATCHED:
                fail("two candidate records were auto-merged into a MATCHED state")

    if exp.get("assert_news2_near_zero_but_level_acute"):
        news2 = rec.risk_components.get("news2_total")
        if news2 is not None and news2 > 3:
            fail(f"NEWS2 {news2} is not near zero; this scenario exists to show a "
                 f"complaint-driven acute case that a physiological score misses")
        if rec.acted_level > 2:
            fail(f"acted level {rec.acted_level} not acute despite an active "
                 f"time-critical pathway")

    if exp.get("assert_normal_temperature_never_reassures_in_geriatric"):
        removed = rec.risk_components.get("reassurance_removed", [])
        if not any("temperature" in r for r in removed):
            fail("geriatric envelope did not record that a normal temperature does "
                 "not reassure [S6]")

    if exp.get("assert_benign_appearance_cannot_suppress_age_rule"):
        if "RF-P04" not in fired:
            fail("neonatal fever rule RF-P04 was suppressed by a benign appearance")

    if exp.get("assert_zero_history_yields_valid_recommendation"):
        if rec.acted_level is None or not rec.dominant_reason:
            fail("zero-history patient did not receive a valid, explained "
                 "recommendation")

    if exp.get("assert_escalated_by_class_not_score") and queue_class is not None:
        if queue_class != "B":
            fail(f"BLIND patient should be escalated by QUEUE CLASS B, got "
                 f"{queue_class}")

    return res
