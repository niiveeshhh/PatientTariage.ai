"""
Latent deterioration trajectories - Blueprint 17.2, 18.1 tier G3/G4.

"The simulator OWNS the latent trajectory.  The engine must NOT have access to
hidden ground truth during decision-making."

This separation is what makes ground-truth tiers G3 and G4 possible:

    G3  outcome-referenced acuity - true acuity is defined by THE INTERVENTION THE
        PATIENT TURNS OUT TO NEED, the operational definition used in the largest
        ESI mistriage study [S2], never by a human or model opinion.

    G4  exact counterfactual queue harm - because the simulator owns every latent
        trajectory, the harm produced by ANY POLICY on the same arrival stream can
        be computed EXACTLY.  This is the strongest empirical tier available to a
        synthetic study and the reason the simulator exists.

Stated limitation (Blueprint 18.2): "the trajectories are OURS, so this measures
whether the engine recovers the generator's logic - NOT whether the generator
resembles medicine."
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.core.models import Observation, Patient, Provenance, Quality


# Blueprint 22.1: "Critical-case miss rate - fraction of patients requiring an
# IMMEDIATE LIFE-SAVING INTERVENTION who were not placed in the top queue class
# within their protocol interval."  The intervention set defines true acuity.
INTERVENTION_TO_TRUE_ACUITY: Dict[str, int] = {
    "airway_management": 1,
    "resuscitation": 1,
    "fluid_resuscitation": 1,
    "immediate_transfusion": 1,
    "thrombolysis": 1,
    "resus_bay": 1,
    "high_flow_oxygen": 2,
    "iv_antibiotics_within_1h": 2,
    "urgent_imaging": 2,
    "cardiac_monitoring": 2,
    "iv_analgesia": 3,
    "iv_fluids": 3,
    "observation_period": 3,
    "wound_care": 4,
    "simple_analgesia": 4,
    "advice_only": 5,
}


@dataclass
class LatentTrajectory:
    """The hidden truth.  NOTHING in app/clinical, app/uncertainty, app/queue or
    app/models may read this object - only app/simulation and app/metrics."""
    deteriorates: bool = False
    onset_offset_min: float = 0.0          # minutes after arrival when drift begins
    event_offset_min: Optional[float] = None   # when the clinical event occurs
    event: Optional[str] = None
    severity: float = 0.0                  # 0..1, how fast and how far
    required_interventions: List[str] = field(default_factory=list)
    true_acuity: int = 5
    silent: bool = False                   # deteriorates without new observations

    # per-field drift rates, units per minute
    drift: Dict[str, float] = field(default_factory=dict)

    def true_acuity_from_interventions(self) -> int:
        if not self.required_interventions:
            return self.true_acuity
        return min(INTERVENTION_TO_TRUE_ACUITY.get(i, 5)
                   for i in self.required_interventions)

    def is_deteriorated_at(self, minutes_since_arrival: float) -> bool:
        """Whether the patient IS in a deteriorated state at this moment.  Used by
        the undetected-deterioration-minutes metric (the primary endpoint)."""
        if not self.deteriorates:
            return False
        return minutes_since_arrival >= self.onset_offset_min

    def to_dict(self) -> Dict[str, Any]:
        return {
            "deteriorates": self.deteriorates,
            "onset_offset_min": round(self.onset_offset_min, 2),
            "event_offset_min": (round(self.event_offset_min, 2)
                                 if self.event_offset_min is not None else None),
            "event": self.event,
            "severity": round(self.severity, 3),
            "required_interventions": sorted(self.required_interventions),
            "true_acuity": self.true_acuity_from_interventions(),
            "silent": self.silent,
            "drift": {k: round(v, 4) for k, v in sorted(self.drift.items())},
        }


# ---------------------------------------------------------------------------

DETERIORATION_PATTERNS: List[Dict[str, Any]] = [
    {
        "name": "sepsis_progression",
        "event": "septic_shock",
        "interventions": ["iv_antibiotics_within_1h", "fluid_resuscitation"],
        "drift": {"heart_rate": 1.4, "respiratory_rate": 0.35,
                  "systolic_bp": -0.9, "temperature_c": 0.02, "spo2": -0.12},
    },
    {
        "name": "respiratory_failure",
        "event": "respiratory_failure",
        "interventions": ["high_flow_oxygen", "airway_management"],
        "drift": {"respiratory_rate": 0.55, "spo2": -0.30, "heart_rate": 0.9},
    },
    {
        "name": "paediatric_compensated_shock",
        "event": "decompensated_shock",
        "interventions": ["fluid_resuscitation", "resus_bay"],
        # The tell: HR climbs steadily while BP is MAINTAINED, then BP falls
        # abruptly.  Blueprint scenario S-28: "children compensate and then crash."
        "drift": {"heart_rate": 2.0, "systolic_bp": 0.0, "capillary_refill_seconds": 0.03},
        "late_collapse": {"systolic_bp": -3.5},
        "paediatric_only": True,
    },
    {
        "name": "occult_haemorrhage",
        "event": "haemorrhagic_shock",
        "interventions": ["immediate_transfusion", "resuscitation"],
        "drift": {"heart_rate": 1.6, "systolic_bp": -1.3, "spo2": -0.05},
    },
    {
        "name": "neurological_decline",
        "event": "reduced_consciousness",
        "interventions": ["airway_management", "urgent_imaging"],
        "drift": {"heart_rate": 0.5, "respiratory_rate": 0.2, "systolic_bp": 0.8},
        "acvpu_decline_at_fraction": 0.7,
    },
    {
        "name": "cardiac_decompensation",
        "event": "cardiogenic_shock",
        "interventions": ["cardiac_monitoring", "resuscitation"],
        "drift": {"heart_rate": 1.1, "systolic_bp": -1.1, "spo2": -0.18,
                  "respiratory_rate": 0.3},
    },
]

STABLE_INTERVENTIONS_BY_LEVEL: Dict[int, List[str]] = {
    1: ["resuscitation"],
    2: ["cardiac_monitoring"],
    3: ["iv_analgesia", "observation_period"],
    4: ["wound_care"],
    5: ["advice_only"],
}


def make_trajectory(rng: random.Random, deteriorates: bool, paediatric: bool,
                    apparent_level: int, wait_horizon_min: float = 120.0
                    ) -> LatentTrajectory:
    """Assign a hidden trajectory.  Seeded, so an identical stream replays exactly.

    Blueprint 23: "Every policy is run over the IDENTICAL SEEDED ARRIVAL STREAM, so
    differences are attributable to the POLICY rather than the sample."
    """
    if not deteriorates:
        return LatentTrajectory(
            deteriorates=False,
            required_interventions=list(
                STABLE_INTERVENTIONS_BY_LEVEL.get(apparent_level, ["advice_only"])),
            true_acuity=apparent_level,
        )

    candidates = [p for p in DETERIORATION_PATTERNS
                  if paediatric or not p.get("paediatric_only")]
    pattern = rng.choice(candidates)

    onset = rng.uniform(5.0, max(10.0, wait_horizon_min * 0.45))
    severity = rng.uniform(0.45, 1.0)
    event_at = onset + rng.uniform(15.0, 45.0) * (1.6 - severity)

    drift = {k: v * severity for k, v in pattern["drift"].items()}

    traj = LatentTrajectory(
        deteriorates=True,
        onset_offset_min=onset,
        event_offset_min=event_at,
        event=pattern["event"],
        severity=severity,
        required_interventions=list(pattern["interventions"]),
        drift=drift,
        # Blueprint trigger T5: some deteriorations produce NO new observations at
        # all.  Those are the ones only the silence trigger can catch.
        silent=(rng.random() < 0.30),
    )
    traj.true_acuity = traj.true_acuity_from_interventions()
    traj._pattern = pattern          # type: ignore[attr-defined]
    return traj


# ---------------------------------------------------------------------------

def project_vitals(patient: Patient, traj: LatentTrajectory,
                   now_min: float) -> Dict[str, Any]:
    """What the patient's vitals ACTUALLY are right now, under the hidden
    trajectory.  The engine never calls this - only the simulator, when it decides
    to record a new observation, and the metrics harness, when it scores harm.
    """
    elapsed = patient.minutes_since_arrival(now_min)
    if not traj.deteriorates or elapsed < traj.onset_offset_min:
        return {}

    drift_minutes = elapsed - traj.onset_offset_min
    out: Dict[str, Any] = {}
    pattern = getattr(traj, "_pattern", {})

    for field_name, rate in traj.drift.items():
        base = patient.value(field_name)
        if base is None:
            continue
        value = float(base) + rate * drift_minutes
        out[field_name] = _clamp_physiological(field_name, value)

    late = pattern.get("late_collapse")
    if late and traj.event_offset_min is not None:
        # The paediatric tell: BP holds, then falls abruptly near the event.
        collapse_start = traj.onset_offset_min + \
            0.75 * (traj.event_offset_min - traj.onset_offset_min)
        if elapsed >= collapse_start:
            for field_name, rate in late.items():
                base = patient.value(field_name)
                if base is None:
                    continue
                value = float(base) + rate * (elapsed - collapse_start)
                out[field_name] = _clamp_physiological(field_name, value)

    frac = pattern.get("acvpu_decline_at_fraction")
    if frac and traj.event_offset_min is not None:
        threshold = traj.onset_offset_min + \
            frac * (traj.event_offset_min - traj.onset_offset_min)
        if elapsed >= threshold:
            out["consciousness_acvpu"] = "V" if elapsed < traj.event_offset_min else "P"

    return out


def _clamp_physiological(field_name: str, value: float) -> Any:
    bounds = {
        "heart_rate": (25, 260, 0),
        "respiratory_rate": (4, 70, 0),
        "systolic_bp": (45, 260, 0),
        "diastolic_bp": (25, 160, 0),
        "spo2": (55, 100, 0),
        "temperature_c": (32.0, 42.5, 1),
        "capillary_refill_seconds": (0.5, 10.0, 1),
    }
    if field_name not in bounds:
        return value
    lo, hi, digits = bounds[field_name]
    v = max(lo, min(hi, value))
    return round(v, digits) if digits else int(round(v))


def record_observation(patient: Patient, traj: LatentTrajectory, now_min: float,
                       fields: Optional[List[str]] = None,
                       provenance: Provenance = Provenance.DEV) -> List[Observation]:
    """The simulator takes a fresh set of vitals.  This is the ONLY channel by which
    the hidden trajectory becomes visible to the engine - which is exactly the
    real-world constraint: the system only knows what somebody measured."""
    projected = project_vitals(patient, traj, now_min)
    if not projected:
        # Not deteriorating (yet) - re-record the current values with a new stamp,
        # which is what a real repeat observation looks like.
        projected = {f: patient.value(f) for f in
                     ("heart_rate", "respiratory_rate", "spo2", "systolic_bp")
                     if patient.value(f) is not None}

    targets = fields or list(projected)
    made: List[Observation] = []
    for f in targets:
        if f not in projected or projected[f] is None:
            continue
        obs = Observation(
            field_name=f, value=projected[f], provenance=provenance,
            timestamp_min=now_min, quality=Quality.CLEAN, source_confidence=0.95,
        )
        patient.set_observation(obs)
        made.append(obs)
    return made
