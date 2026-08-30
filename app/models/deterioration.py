"""
The deterioration estimator - Blueprint 9.4 and 20.

"A monotonically-constrained gradient-boosted deterioration estimator WHOSE ONLY
AUTHORITY IS TO SHORTEN CLOCKS.  If the model is wrong, the cost is a wasted glance.
If the model is absent, the product still runs."

Blueprint 9.3 authority table, reproduced here as an executable boundary:
    MAY:     estimate deterioration, contribute to uncertainty, shorten clocks
    MAY NOT: veto a deterministic red flag, lengthen a TTL, independently
             de-escalate, diagnose, prescribe, discharge, autonomously change
             clinical state

The feature set is deliberately small so the top driver is always nameable, and
every physiological feature carries a monotone constraint so its direction of effect
is guaranteed rather than estimated.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.models import Patient
from app.clinical.layer1_envelope import EnvelopeSelection
from app.clinical.layer3_risk import RiskRead
from app.models.monotonic_gbm import MonotonicGBM

# ---------------------------------------------------------------------------
# The feature set.  +1 == higher value can only RAISE deterioration probability.
# ---------------------------------------------------------------------------

FEATURES: List[Tuple[str, int]] = [
    ("risk_score",              +1),   # envelope-appropriate derangement gradient
    ("hr_over_threshold_ratio", +1),   # normalised to the ACTIVE envelope's band
    ("rr_over_threshold_ratio", +1),
    ("spo2_deficit",            +1),   # max(0, 96 - SpO2)
    ("sbp_deficit",             +1),   # max(0, 110 - systolic)
    ("temp_abs_deviation",      +1),
    ("acvpu_severity",          +1),   # A=0 C=2 V=3 P=4 U=5
    ("hr_slope_per_10min",      +1),   # TREND, not absolute
    ("rr_slope_per_10min",      +1),
    ("spo2_negative_slope",     +1),   # positive when SpO2 is FALLING
    ("minutes_waiting",         +1),
    ("missing_critical_count",  +1),   # ignorance raises, never lowers
    ("staleness_fraction",      +1),
    ("occupancy_ratio",         +1),   # crowding is itself an exposure [S3]
    ("communication_barrier",   +1),
]

FEATURE_NAMES = [f for f, _ in FEATURES]
CONSTRAINTS = [c for _, c in FEATURES]

MODEL_PATH_DEFAULT = os.path.join("data", "calibration", "deterioration_gbm.json")


# ---------------------------------------------------------------------------

def _acvpu_severity(v: Any) -> float:
    return {"A": 0.0, "C": 2.0, "V": 3.0, "P": 4.0, "U": 5.0}.get(v, 1.0)


def _slope_per_10min(patient: Patient, field_name: str, now_min: float,
                     lookback: float = 60.0) -> float:
    series = [o for o in patient.history_for(field_name)
              if o.value is not None and not o.quarantined
              and now_min - o.timestamp_min <= lookback]
    if len(series) < 2:
        return 0.0
    first, last = series[0], series[-1]
    span = last.timestamp_min - first.timestamp_min
    if span <= 0:
        return 0.0
    try:
        return (float(last.value) - float(first.value)) / span * 10.0
    except (TypeError, ValueError):
        return 0.0


def build_features(patient: Patient, selection: EnvelopeSelection, risk: RiskRead,
                   now_min: float, occupancy_ratio: float,
                   missing_critical_count: int, staleness_fraction: float
                   ) -> List[float]:
    """Blueprint C6 equity guard: this vector contains NO protected attribute.
    Age enters only through the envelope's own thresholds, which is clinical
    necessity, not profiling."""
    band = selection.age_band
    hr_ref = float(band["hr_high"]) if band else 100.0
    rr_ref = float(band["rr_high"]) if band else 20.0

    hr = patient.value("heart_rate")
    rr = patient.value("respiratory_rate")
    spo2 = patient.value("spo2")
    sbp = patient.value("systolic_bp")
    temp = patient.value("temperature_c")

    return [
        max(0.0, min(1.0, risk.score)),
        max(0.0, (float(hr) / hr_ref) - 1.0) if hr is not None else 0.0,
        max(0.0, (float(rr) / rr_ref) - 1.0) if rr is not None else 0.0,
        max(0.0, 96.0 - float(spo2)) if spo2 is not None else 0.0,
        max(0.0, 110.0 - float(sbp)) if sbp is not None else 0.0,
        abs(float(temp) - 37.0) if temp is not None else 0.0,
        _acvpu_severity(patient.value("consciousness_acvpu")),
        max(0.0, _slope_per_10min(patient, "heart_rate", now_min)),
        max(0.0, _slope_per_10min(patient, "respiratory_rate", now_min)),
        max(0.0, -_slope_per_10min(patient, "spo2", now_min)),
        patient.minutes_since_arrival(now_min),
        float(missing_critical_count),
        max(0.0, min(1.0, staleness_fraction)),
        max(0.0, occupancy_ratio),
        1.0 if patient.communication_barrier else 0.0,
    ]


@dataclass
class DeteriorationEstimator:
    """The bounded learned layer.

    `available=False` is degradation rung L1 (NO-MODEL): the estimator returns None,
    the product keeps running on rules and published scores, and every clock gets
    SHORTER because the system now knows less (Blueprint 13.2).
    """
    model: Optional[MonotonicGBM] = None
    available: bool = True
    version: str = "0.9.0"

    def estimate(self, patient: Patient, selection: EnvelopeSelection, risk: RiskRead,
                 now_min: float, occupancy_ratio: float,
                 missing_critical_count: int = 0,
                 staleness_fraction: float = 0.0) -> Optional[float]:
        """Probability that this patient deteriorates while waiting.

        Returns None when the model is unavailable - and None is a FULLY SUPPORTED
        state everywhere downstream, not an error.
        """
        if not self.available or self.model is None:
            return None
        x = build_features(patient, selection, risk, now_min, occupancy_ratio,
                           missing_critical_count, staleness_fraction)
        return self.model.predict_proba(x)

    def top_driver(self, patient: Patient, selection: EnvelopeSelection,
                   risk: RiskRead, now_min: float, occupancy_ratio: float,
                   missing_critical_count: int = 0,
                   staleness_fraction: float = 0.0) -> Optional[Tuple[str, float]]:
        """The single nameable top contributor, for the basis view."""
        if not self.available or self.model is None:
            return None
        x = build_features(patient, selection, risk, now_min, occupancy_ratio,
                           missing_critical_count, staleness_fraction)
        contribs = self.model.feature_contributions(x)
        if not contribs:
            return None
        name, value = max(contribs.items(), key=lambda kv: abs(kv[1]))
        return name, value

    # ------------------------------------------------------------------
    def disable(self) -> None:
        """WOW moment 4 - 'we kill the model live'.  Blueprint 24 beat 6."""
        self.available = False

    def enable(self) -> None:
        self.available = True

    @staticmethod
    def load(path: str = MODEL_PATH_DEFAULT) -> "DeteriorationEstimator":
        if not os.path.exists(path):
            # New site with no calibration data -> run rules-only, which is safe and
            # useful on day one (Blueprint complexity 27 fallback).
            return DeteriorationEstimator(model=None, available=False)
        return DeteriorationEstimator(model=MonotonicGBM.load(path), available=True)


def new_model(seed: int = 20260825) -> MonotonicGBM:
    return MonotonicGBM(
        feature_names=list(FEATURE_NAMES),
        monotone_constraints=list(CONSTRAINTS),
        n_estimators=40,
        learning_rate=0.15,
        max_depth=3,
        min_samples_leaf=12,
        subsample=1.0,
        seed=seed,
    )
