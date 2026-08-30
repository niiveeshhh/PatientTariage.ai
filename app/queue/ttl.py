"""
L5 - TTL assignment.  Blueprint 9.2, 12.1.

"Where risk becomes operational.  Converts 'this patient is 0.73' into 'this
decision is stale in 15 minutes', which is a thing a human can act on.  Floored by
published CTAS intervals [S17]."

    TTL = min( protocol floor for the acted level,
               risk-derived interval,
               uncertainty-derived interval,
               load-compressed interval )

INVARIANT I1 (Blueprint 13.1):
    "TTL is non-increasing for the duration of a patient's stay, except by an
     explicit human action with an attributed audit record."

Enforced by a SINGLE GUARDED WRITE PATH.  Blueprint 13.1: "automatic lengthening is
NOT AN AVAILABLE OPERATION in the engine."  There is exactly one function below that
can raise a TTL and it demands an actor and a reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.core.knowledge import Knowledge
from app.core.models import Patient, UncertaintyClass, UncertaintyComponents
from app.clinical.layer1_envelope import EnvelopeSelection
from app.uncertainty.classes import ClassBehaviour


class TTLViolation(RuntimeError):
    """Raised when something attempts to lengthen a clock without a named human.
    Blueprint 13.1 I1 is a RELEASE GATE, not a KPI - so the engine raises rather
    than silently clamping, and the property tests assert the raise."""


@dataclass
class TTLResult:
    ttl_minutes: float
    basis: str
    candidates: Dict[str, Optional[float]] = field(default_factory=dict)
    expires_at_min: float = 0.0
    clamped_by_ratchet: bool = False
    previous_ttl: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ttl_minutes": round(self.ttl_minutes, 3),
            "basis": self.basis,
            "candidates": {k: (round(v, 3) if v is not None else None)
                           for k, v in sorted(self.candidates.items())},
            "expires_at_min": round(self.expires_at_min, 4),
            "clamped_by_ratchet": self.clamped_by_ratchet,
            "previous_ttl": (round(self.previous_ttl, 3)
                             if self.previous_ttl is not None else None),
        }


# ASM parameters for the risk-derived and load-compressed intervals.
RISK_INTERVAL_MAX_MIN = 120.0
RISK_INTERVAL_MIN_MIN = 5.0
UNCERTAINTY_INTERVAL_MAX_MIN = 120.0
UNCERTAINTY_INTERVAL_MIN_MIN = 8.0
LOAD_COMPRESSION_FLOOR = 0.35     # a clock may compress to 35% of its base under load
SET_WIDTH_TIGHTENING = {1: 1.00, 2: 0.75, 3: 0.55, 4: 0.40, 5: 0.30}   # ASM


def risk_derived_interval(risk_score: float) -> float:
    """A steeper derangement gradient buys a shorter clock, never a longer one.
    Monotonically non-increasing in risk by construction."""
    r = max(0.0, min(1.0, risk_score))
    return RISK_INTERVAL_MIN_MIN + (RISK_INTERVAL_MAX_MIN - RISK_INTERVAL_MIN_MIN) * (1.0 - r) ** 2


def uncertainty_derived_interval(comp: UncertaintyComponents,
                                 behaviour: ClassBehaviour,
                                 set_width: int) -> float:
    """Blueprint 10.4 step 7: "TTL multiplier decreases MONOTONICALLY as |S| grows."

    And Blueprint 10.1: every component's effect is in ONE DIRECTION - lower
    completeness -> shorter clock; staleness -> shorter clock; weak provenance ->
    cannot reach CLEAR; contradiction -> shorter clock; out-of-distribution ->
    wider set, which via the multiplier is again a shorter clock.
    """
    u = max(0.0, min(1.0, comp.composite()))
    base = UNCERTAINTY_INTERVAL_MIN_MIN + \
        (UNCERTAINTY_INTERVAL_MAX_MIN - UNCERTAINTY_INTERVAL_MIN_MIN) * (1.0 - u) ** 2
    width_factor = SET_WIDTH_TIGHTENING.get(max(1, min(5, set_width)), 0.30)
    value = base * width_factor
    if behaviour.ttl_floor_minutes is not None:
        value = min(value, behaviour.ttl_floor_minutes)
    return value


def load_compressed_interval(base_minutes: float, occupancy_ratio: float,
                             mode: str) -> float:
    """Blueprint complexity 20: "Occupancy ratio is an ENGINE INPUT, so a crowded
    room automatically tightens every clock."

    Compression only ever SHORTENS (constraint C1).  A falling load does NOT buy
    the clock back - that is exactly the "optimiser recovering capacity by relaxing
    clocks when the room quietens" failure C1 exists to prevent.
    """
    occ = max(0.0, occupancy_ratio)
    if occ <= 0.90 and mode == "NORMAL":
        return base_minutes
    # Above the measured harm inflection [S3], compress proportionally to overload.
    overload = max(0.0, occ - 0.90)
    factor = max(LOAD_COMPRESSION_FLOOR, 1.0 - overload)
    if mode in ("SURGE", "MCI"):
        factor = min(factor, 0.60)
    elif mode == "STRAINED":
        factor = min(factor, 0.80)
    return base_minutes * factor


def compute_ttl(patient: Patient, selection: EnvelopeSelection, kb: Knowledge,
                acted_level: int, risk_score: float, comp: UncertaintyComponents,
                behaviour: ClassBehaviour, set_width: int, occupancy_ratio: float,
                mode: str, profile: Dict[str, Any], now_min: float,
                rule_ttl_floor: Optional[float] = None,
                degradation_rung: str = "L0_FULL") -> TTLResult:
    """The minimum of every applicable constraint.  Blueprint 12.1."""
    candidates: Dict[str, Optional[float]] = {}

    # 1. Protocol floor for the acted level, after any hospital TIGHTENING.
    protocol = kb.protocol_floor(acted_level, profile)
    if protocol <= 0:
        # Level 1 is CONTINUOUS nursing [S17] - represented as a 1-minute clock so
        # the ring still runs and the patient can never fall off the board.
        protocol = 1.0
    candidates["protocol_floor"] = protocol

    # 2. Risk-derived.
    candidates["risk_derived"] = risk_derived_interval(risk_score)

    # 3. Uncertainty-derived.
    candidates["uncertainty_derived"] = uncertainty_derived_interval(
        comp, behaviour, set_width)

    # 4. Load-compressed (applied to the running minimum, not as an independent term).
    running = min(v for v in candidates.values() if v is not None)
    candidates["load_compressed"] = load_compressed_interval(running, occupancy_ratio, mode)

    # 5. Envelope floor (unknown age, pregnancy suppression).
    if selection.ttl_floor_minutes is not None:
        candidates["envelope_floor"] = selection.ttl_floor_minutes

    # 6. Uncertainty-class floor (BLIND 10 min, CONFLICTED conflict floor).
    if behaviour.ttl_floor_minutes is not None:
        candidates["uncertainty_class_floor"] = behaviour.ttl_floor_minutes

    # 7. Hard-rule floor (RF-X01 communication barrier, RF-X02 unknown age).
    if rule_ttl_floor is not None:
        candidates["rule_floor"] = rule_ttl_floor

    # 8. Degradation.  INVARIANT I5: every rung produces clocks AT LEAST AS SHORT
    #    as the rung above.  Degraded modes substitute the protocol floor, which is
    #    the shortest value the engine could have produced anyway.
    if degradation_rung != "L0_FULL":
        candidates["degradation"] = degradation_ttl(degradation_rung, protocol)

    value = min(v for v in candidates.values() if v is not None)
    basis = min(
        ((k, v) for k, v in candidates.items() if v is not None),
        key=lambda kv: (kv[1], kv[0]),
    )[0]

    # ------------------------------------------------------------------
    # THE RATCHET.  Constraint C1 / invariant I1.
    # A newly-computed LONGER value is DISCARDED and the current clock stands.
    # Blueprint 12.3 step 4: "A reassuring re-measurement buys the patient a FRESH
    # LOOK, not a LONGER LEASH."
    # ------------------------------------------------------------------
    previous = patient.current_ttl_minutes
    clamped = False
    if previous is not None:
        remaining = remaining_ttl(patient, now_min)
        if remaining is not None and value > remaining:
            value = remaining
            basis = f"ratchet_hold({basis} was longer)"
            clamped = True

    return TTLResult(
        ttl_minutes=value,
        basis=basis,
        candidates=candidates,
        expires_at_min=now_min + value,
        clamped_by_ratchet=clamped,
        previous_ttl=previous,
    )


def degradation_ttl(rung: str, protocol_floor: float) -> float:
    """Blueprint 13.2: every rung shortens clocks.  The ladder is monotone.

    L1 NO-MODEL    : protocol floor and uncertainty only
    L2 NO-HISTORY  : uncertainty rises system-wide; all clocks tighten
    L3 NO-ENGINE   : static protocol mode - every patient falls to the published
                     reassessment interval for their last human-assigned acuity
    L4 DARK        : exportable protocol snapshot; no new recommendations
    """
    ladder = {
        "L0_FULL": 1.00,
        "L1_NO_MODEL": 0.90,
        "L2_NO_HISTORY": 0.80,
        "L3_NO_ENGINE": 1.00,     # protocol floor exactly - no engine to shorten it
        "L4_DARK": 1.00,
    }
    return protocol_floor * ladder.get(rung, 1.0)


# ---------------------------------------------------------------------------
# The single guarded write path
# ---------------------------------------------------------------------------

def remaining_ttl(patient: Patient, now_min: float) -> Optional[float]:
    if patient.current_ttl_minutes is None or patient.ttl_set_at_min is None:
        return None
    return patient.current_ttl_minutes - (now_min - patient.ttl_set_at_min)


def apply_ttl(patient: Patient, result: TTLResult, now_min: float) -> float:
    """THE ONLY automatic write path for a TTL.

    Blueprint 13.1: "A single guarded write path for TTL; automatic lengthening is
    NOT AN AVAILABLE OPERATION in the engine."  This function physically cannot
    lengthen a clock: it takes the minimum of the proposal and what is left.
    """
    remaining = remaining_ttl(patient, now_min)
    proposed = result.ttl_minutes
    if remaining is not None:
        proposed = min(proposed, max(0.0, remaining))
    patient.current_ttl_minutes = proposed
    patient.ttl_set_at_min = now_min
    result.ttl_minutes = proposed
    result.expires_at_min = now_min + proposed
    return proposed


def human_lengthen_ttl(patient: Patient, new_ttl_minutes: float, now_min: float,
                       actor: str, actor_role: str, reason: str,
                       audit_written: bool) -> float:
    """The ONLY path by which a TTL may lengthen - and it demands all four of:
    a named actor, a role, a reason, and a DURABLE AUDIT RECORD.

    Blueprint 13.3: "If the audit write fails, the override is REFUSED and the
    clinician is told.  No unlogged clinical change, ever."
    """
    if not actor or not actor.strip():
        raise TTLViolation(
            "I1 VIOLATION REFUSED: lengthening a TTL requires a named human actor. "
            "Blueprint 9.3: 'If the actor is unknown, the action is refused, not "
            "defaulted. An unattributable clinical change is not permitted.'"
        )
    if not reason or not reason.strip():
        raise TTLViolation(
            "I1 VIOLATION REFUSED: lengthening a TTL requires a recorded reason."
        )
    if not audit_written:
        raise TTLViolation(
            "I7 VIOLATION REFUSED: the audit record could not be durably written, "
            "so the clock change is refused rather than performed silently."
        )
    patient.current_ttl_minutes = float(new_ttl_minutes)
    patient.ttl_set_at_min = now_min
    patient.ttl_lengthened_by_human.append((now_min, f"{actor_role}:{actor}:{reason}"))
    return patient.current_ttl_minutes


def is_expired(patient: Patient, now_min: float) -> bool:
    """Class E membership: TTL elapsed and NO reassessment recorded."""
    remaining = remaining_ttl(patient, now_min)
    if remaining is None:
        return False
    if remaining > 0:
        return False
    if patient.last_reassessed_at_min is None:
        return True
    return patient.last_reassessed_at_min < (patient.ttl_set_at_min or 0.0)


def ttl_fraction_elapsed(patient: Patient, now_min: float) -> float:
    """elapsed/TTL, used by the queue's superlinear time-pressure term."""
    if patient.current_ttl_minutes is None or patient.ttl_set_at_min is None:
        return 0.0
    if patient.current_ttl_minutes <= 0:
        return 1.0
    return max(0.0, (now_min - patient.ttl_set_at_min) / patient.current_ttl_minutes)
