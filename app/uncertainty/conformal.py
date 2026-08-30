"""
Conformal escalation - Blueprint 10.4.

This is the mechanism by which Round 2's hardest requirement - "deliberately tuned
to bias toward escalation under uncertainty ... demonstrate this design choice
EXPLICITLY in their prototype" - becomes a PROVABLE PROPERTY rather than a claim.

    CALIBRATION, once per envelope, offline
      1  Hold out a calibration set, disjoint from training.
      2  For each calibration patient, compute the model's non-conformity score
         against the outcome-referenced true acuity (ground-truth tier G3).
      3  q = the ceil((n+1)(1-alpha))/n empirical quantile of those scores.

    INFERENCE, per patient
      4  S = every acuity level whose non-conformity score does not exceed q.
      5  ACTED LEVEL = min(S)  -- "min" means the MOST ACUTE member.
      6  |S| is the reported uncertainty magnitude.
      7  TTL multiplier decreases monotonically as |S| grows.

    INVARIANTS
      I8  acted_level == min(S)                                always, no exceptions
      I9  acted_level at least as acute as the rule-layer floor  rules win ties
      I10 alpha may be lowered by a hospital, never raised above the ceiling

GUARANTEE, stated with its assumption: under EXCHANGEABILITY between calibration and
deployment populations, the true acuity lies in S with probability at least 1-alpha.
Exchangeability is an ASSUMPTION, not a fact - it degrades under distribution shift,
which is exactly why set-width drift is monitored as a shift alarm and why the
deterministic rule floor exists beneath the whole mechanism.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.models import ACUITY_LEVELS, clamp_level, most_acute


@dataclass
class CalibrationSet:
    """A per-envelope calibration, disjoint from training data."""
    envelope_id: str
    scores: List[float] = field(default_factory=list)
    calibration_id: str = "cal-synthetic-v1"
    n: int = 0

    def quantile(self, alpha: float) -> Optional[float]:
        """q = the ceil((n+1)(1-alpha))/n empirical quantile.

        Returns None when there is not enough calibration data, which triggers the
        conservative fallback rather than a silently wrong guarantee.
        """
        n = len(self.scores)
        if n == 0:
            return None
        k = math.ceil((n + 1) * (1.0 - alpha))
        if k > n:
            # The requested coverage is not attainable with this much data.
            return None
        ordered = sorted(self.scores)
        return ordered[k - 1]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "envelope_id": self.envelope_id,
            "calibration_id": self.calibration_id,
            "n": len(self.scores),
        }


@dataclass
class ConformalResult:
    prediction_set: List[int]
    acted_level: int
    point_estimate_level: int
    set_width: int
    escalation_premium: float
    q: Optional[float]
    alpha: float
    calibration_n: int
    fallback_used: bool
    fallback_reason: Optional[str] = None
    nonconformity: Dict[int, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prediction_set": sorted(self.prediction_set),
            "acted_level": self.acted_level,
            "point_estimate_level": self.point_estimate_level,
            "set_width": self.set_width,
            "escalation_premium": round(self.escalation_premium, 4),
            "q": None if self.q is None else round(self.q, 6),
            "alpha": self.alpha,
            "calibration_n": self.calibration_n,
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
            "nonconformity": {k: round(v, 5) for k, v in sorted(self.nonconformity.items())},
        }


# ---------------------------------------------------------------------------
# Non-conformity
# ---------------------------------------------------------------------------

def nonconformity_scores(level_scores: Dict[int, float]) -> Dict[int, float]:
    """Non-conformity for a level = 1 - its normalised plausibility.

    Lower non-conformity == the level conforms better to the evidence.  Using
    1 - softmax-normalised plausibility keeps the score in [0, 1] and makes the
    calibration quantile directly interpretable.
    """
    total = sum(max(0.0, v) for v in level_scores.values())
    if total <= 0:
        return {lv: 1.0 for lv in level_scores}
    return {lv: 1.0 - (max(0.0, v) / total) for lv, v in level_scores.items()}


def calibrate(records: Sequence[Tuple[Dict[int, float], int]], envelope_id: str,
              calibration_id: str = "cal-synthetic-v1") -> CalibrationSet:
    """Split-conformal calibration.

    records: (level_plausibility_scores, true_acuity_level) pairs from a HELD-OUT
    calibration split, disjoint from training.  The true level comes from
    ground-truth tier G3 (outcome-referenced acuity), never from a model opinion.
    """
    cal = CalibrationSet(envelope_id=envelope_id, calibration_id=calibration_id)
    for level_scores, true_level in records:
        nc = nonconformity_scores(level_scores)
        cal.scores.append(nc.get(clamp_level(true_level), 1.0))
    cal.n = len(cal.scores)
    return cal


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------

def predict_set(level_scores: Dict[int, float], calibration: Optional[CalibrationSet],
                alpha: float, widen_toward_acute: int = 0,
                rule_floor: Optional[int] = None,
                widen_acute_bound: Optional[int] = None) -> ConformalResult:
    """Produce the prediction set and the ACTED LEVEL.

    Step 5 of the specification is the entire safety argument: ACTED LEVEL = the
    MOST ACUTE member of the set.  Escalation is the MECHANISM, not a post-hoc
    adjustment - it cannot be accidentally tuned away.
    """
    nc = nonconformity_scores(level_scores)
    point = min(level_scores.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    point = clamp_level(point)

    q = calibration.quantile(alpha) if calibration else None
    fallback_used = False
    fallback_reason: Optional[str] = None

    if q is None:
        # Blueprint complexity 9 fallback: "If calibration data is insufficient,
        # alpha defaults to its most conservative permitted value and the set
        # defaults to {point estimate, one level more acute}."
        fallback_used = True
        fallback_reason = (
            "insufficient calibration data - conservative fallback: "
            "{point estimate, one level more acute}"
        )
        pset = {point, clamp_level(point - 1)}
    else:
        pset = {lv for lv, score in nc.items() if score <= q}
        if not pset:
            # A set must never be empty; the safest non-empty set contains the
            # point estimate AND one level more acute.
            pset = {point, clamp_level(point - 1)}
            fallback_used = True
            fallback_reason = "empty prediction set - conservative fallback applied"

    # Uncertainty classes widen the set TOWARD THE ACUTE SIDE (Blueprint 10.3).
    # Widening never removes a level and never moves the set away from acuity.
    #
    # widen_acute_bound is the DETERMINISTIC RULE FLOOR.  Where the rule layer has
    # spoken, it has already resolved the acute side authoritatively, and widening
    # PAST it would be escalating on uncertainty alone beyond where the guideline
    # says the threshold is.  Uncertainty widening exists for where the rules are
    # SILENT.  Blueprint 22.6: "a system that escalates everything is trivially safe
    # and useless."  [ASM]
    if widen_toward_acute > 0:
        acute_edge = min(pset)
        for i in range(1, widen_toward_acute + 1):
            candidate = clamp_level(acute_edge - i)
            if widen_acute_bound is not None and candidate < widen_acute_bound:
                break
            pset.add(candidate)

    prediction_set = sorted(pset)
    acted = most_acute(prediction_set)          # INVARIANT I8

    if rule_floor is not None:
        # INVARIANT I9: acted level is at least as acute as the rule-layer floor.
        # The rule floor is applied AFTER set selection - rules win ties and
        # disagreements (Blueprint 10.4).
        if rule_floor < acted:
            acted = rule_floor
            if rule_floor not in prediction_set:
                prediction_set = sorted(set(prediction_set) | {rule_floor})

    return ConformalResult(
        prediction_set=prediction_set,
        acted_level=acted,
        point_estimate_level=point,
        set_width=len(prediction_set),
        # Blueprint 22.6: ESCALATION PREMIUM = mean(point_estimate - acted) over
        # patients with |S| > 1.  Positive by construction; measured in acuity levels.
        escalation_premium=float(point - acted),
        q=q,
        alpha=alpha,
        calibration_n=calibration.n if calibration else 0,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
        nonconformity=nc,
    )


def empirical_coverage(results: Sequence[Tuple[List[int], int]]) -> float:
    """Blueprint 22.3: empirical fraction of patients whose TRUE level lay inside
    the prediction set, versus the nominal 1-alpha.

    Reported per envelope and per profile.  "Materially below nominal in any
    stratum ... usually means EXCHANGEABILITY HAS FAILED there, not that the model
    is bad."
    """
    if not results:
        return 0.0
    hits = sum(1 for pset, true_level in results if true_level in pset)
    return hits / float(len(results))


def mean_set_width(sets: Sequence[List[int]]) -> float:
    if not sets:
        return 0.0
    return sum(len(s) for s in sets) / float(len(sets))


def set_width_drift(baseline_widths: Sequence[List[int]],
                    candidate_widths: Sequence[List[int]]) -> float:
    """Blueprint 6.11 / complexity 10: conformal SET-WIDTH DRIFT as a cheap,
    principled distribution-shift alarm.

    "If mean set width rises at a new site, the calibration no longer holds and the
    system says so."  Returns the absolute change in mean width.
    """
    return mean_set_width(candidate_widths) - mean_set_width(baseline_widths)


DRIFT_ALARM_THRESHOLD = 0.35     # ASM: mean-width increase that trips the alarm


def drift_alarm(delta: float) -> bool:
    return delta >= DRIFT_ALARM_THRESHOLD


# ---------------------------------------------------------------------------
# Level plausibility from the risk read.  Deterministic, monotone in risk.
# ---------------------------------------------------------------------------

# ASM: level centroids on the 0..1 derangement gradient.  Level 1 sits at maximal
# derangement, level 5 at minimal.  These are the mapping between a continuous read
# and the department's discrete vocabulary - they are OUR choice, and the conformal
# layer exists precisely so that this choice is not trusted on its own.
LEVEL_CENTROIDS: Dict[int, float] = {1: 0.92, 2: 0.70, 3: 0.45, 4: 0.22, 5: 0.05}
LEVEL_BANDWIDTH = 0.17           # ASM


def level_plausibility(risk_score: float, deterioration: Optional[float] = None,
                       uncertainty_composite: float = 0.0) -> Dict[int, float]:
    """Turn the L3 gradient into a plausibility over acuity levels.

    Deterioration and uncertainty SHIFT MASS TOWARD THE ACUTE SIDE ONLY - neither
    can move mass toward the benign side.  That one-directional coupling is what
    makes "uncertainty never reassures" a property of the arithmetic.
    """
    shift = 0.0
    if deterioration is not None:
        shift += 0.25 * max(0.0, min(1.0, deterioration))
    shift += 0.15 * max(0.0, min(1.0, uncertainty_composite))
    effective = min(1.0, max(0.0, risk_score) + shift)

    out: Dict[int, float] = {}
    for level in ACUITY_LEVELS:
        d = effective - LEVEL_CENTROIDS[level]
        out[level] = math.exp(-(d * d) / (2 * LEVEL_BANDWIDTH * LEVEL_BANDWIDTH))
    return out
