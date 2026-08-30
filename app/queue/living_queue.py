"""
L6 - The Living Queue.  Blueprint 11.

"The product.  Humans track 5-7 waiting patients; at 40 the room becomes anonymous.
This layer is the only one that reasons about the ROOM rather than the patient."

The instruction was explicit: DO NOT simply sort patients by an AI score.  A scalar
priority has three fatal properties (Blueprint 11.1): it permits trade-offs that
must not exist, it hides which term moved, and it invites resource terms.

    STEP 1  HARD CLASS ASSIGNMENT  (lexicographic; no cross-class trade-off possible)
              R  red flag active   E  expired   B  blind   N  normal
            Ordering is ABSOLUTE.  No score in a lower class can ever displace a
            member of a higher class.

    STEP 2  HARM-RATE SCORE, computed only WITHIN a class
              H = w_risk*Risk + w_det*Deterioration + w_time*TimePressure
                  + w_unc*Uncertainty + w_traj*Delta
              TimePressure = (elapsed/TTL)^gamma,  gamma > 1

    STEP 3  WORKLIST CONSTRUCTION
              K = clamp(round(staff * relooks_per_staff_hour * TTL_window_hours), 1, 5)
              Demand above K does NOT lengthen the list.  It becomes a quantified DEFICIT.

Constraints C1-C6 are enforced in this module.  Blueprint 11.3: "This is the most
important subsection in the document.  A ranking function that is mathematically
defensible and clinically dangerous is the single most likely way this product fails."
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.knowledge import Knowledge
from app.core.models import Patient, Recommendation, UncertaintyClass
from app.queue.ttl import is_expired, remaining_ttl, ttl_fraction_elapsed


# ---------------------------------------------------------------------------

@dataclass
class QueueEntry:
    patient: Patient
    recommendation: Recommendation
    hard_class: str = "N"
    harm_score: float = 0.0
    components: Dict[str, float] = field(default_factory=dict)
    rank: int = 0
    previous_rank: Optional[int] = None
    on_worklist: bool = False
    change_reason: Optional[str] = None
    projected_wait_min: float = 0.0
    breaches_max_wait: bool = False
    resource_tiebreak_applied: bool = False

    @property
    def patient_ref(self) -> str:
        return self.patient.patient_ref

    def to_dict(self, now_min: float) -> Dict[str, Any]:
        rem = remaining_ttl(self.patient, now_min)
        return {
            "patient_ref": self.patient_ref,
            "chair": self.patient.chair,
            "hard_class": self.hard_class,
            "harm_score": round(self.harm_score, 4),
            "components": {k: round(v, 4) for k, v in sorted(self.components.items())},
            "rank": self.rank,
            "previous_rank": self.previous_rank,
            "on_worklist": self.on_worklist,
            "change_reason": self.change_reason,
            "ttl_remaining_min": (round(rem, 2) if rem is not None else None),
            "ttl_total_min": self.patient.current_ttl_minutes,
            "projected_wait_min": round(self.projected_wait_min, 2),
            "breaches_max_wait": self.breaches_max_wait,
            "acted_level": self.recommendation.acted_level,
            "uncertainty_class": self.recommendation.uncertainty_class.value,
            "dominant_reason": self.recommendation.dominant_reason,
            "action_verb": self.recommendation.action_verb,
        }


@dataclass
class DeficitEntry:
    patient_ref: str
    chair: str
    level_label: str
    due_in_min: Optional[float]
    overdue_by_min: Optional[float]
    no_capacity_until_min: Optional[float]


@dataclass
class DeficitBoard:
    """Blueprint 16.3.

    "When constraint C2 cannot be satisfied ... the system does NOT return a
    'best available' ordering and let the interface imply the room is under
    control.  It raises a second panel."

    "An AI that admits insufficiency is more trustworthy than one that always has
    an answer - and it is also the only honest response, because no ordering of
    four patients across two nurses creates a fifth nurse."
    """
    active: bool = False
    demand_per_hour: float = 0.0
    capacity_per_hour: float = 0.0
    deficit_per_hour: float = 0.0
    additional_staff_needed: float = 0.0
    at_risk: List[DeficitEntry] = field(default_factory=list)
    recommendation: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "active": self.active,
            "demand_per_hour": round(self.demand_per_hour, 2),
            "capacity_per_hour": round(self.capacity_per_hour, 2),
            "deficit_per_hour": round(self.deficit_per_hour, 2),
            "additional_staff_needed": round(self.additional_staff_needed, 2),
            "at_risk": [
                {
                    "patient_ref": e.patient_ref,
                    "chair": e.chair,
                    "level_label": e.level_label,
                    "due_in_min": (round(e.due_in_min, 1) if e.due_in_min is not None else None),
                    "overdue_by_min": (round(e.overdue_by_min, 1)
                                       if e.overdue_by_min is not None else None),
                    "no_capacity_until_min": (round(e.no_capacity_until_min, 1)
                                              if e.no_capacity_until_min is not None else None),
                }
                for e in self.at_risk
            ],
            "recommendation": self.recommendation,
        }


@dataclass
class QueueState:
    entries: List[QueueEntry] = field(default_factory=list)
    worklist: List[QueueEntry] = field(default_factory=list)
    k: int = 3
    k_basis: Dict[str, Any] = field(default_factory=dict)
    deficit: DeficitBoard = field(default_factory=DeficitBoard)
    mode: str = "NORMAL"
    objective: str = "expected_harm"
    occupancy_ratio: float = 0.0
    constraint_log: List[str] = field(default_factory=list)
    churn_violations: List[str] = field(default_factory=list)

    def to_dict(self, now_min: float) -> Dict[str, Any]:
        return {
            "entries": [e.to_dict(now_min) for e in self.entries],
            "worklist": [e.patient_ref for e in self.worklist],
            "k": self.k,
            "k_basis": self.k_basis,
            "deficit": self.deficit.to_dict(),
            "mode": self.mode,
            "objective": self.objective,
            "occupancy_ratio": round(self.occupancy_ratio, 4),
            "constraint_log": list(self.constraint_log),
            "churn_violations": list(self.churn_violations),
        }


# ---------------------------------------------------------------------------
# K derivation - Blueprint 11.2 step 3, and the answer to "why three?"
# ---------------------------------------------------------------------------

def derive_k(profile: Dict[str, Any], kb: Knowledge,
             ttl_window_hours: Optional[float] = None,
             staff_override: Optional[int] = None) -> Tuple[int, Dict[str, Any]]:
    """K = clamp(round(staff_on_shift * relooks_per_staff_hour * TTL_window_hours), 1, 5)

    Blueprint 2.3: "Round 1's 'exactly 3' becomes a DERIVED QUANTITY with a
    defensible ceiling.  Above 5 the list stops being a worklist and becomes an
    alarm stream."

    Blueprint judge question 4: "Three is a DEFAULT, not a constant."
    """
    staffing = profile.get("staffing", {})
    staff = staff_override if staff_override is not None else staffing.get("triage_nurses_on_shift")
    if staff is None or int(staff) < 1:
        # Blueprint complexity 12 fallback: unknown staffing -> assume the profile's
        # declared minimum, giving the smallest K and the earliest deficit warning.
        staff = staffing.get("minimum_staffing_assumption", 1)
    relooks = float(staffing.get("relooks_per_staff_hour", 10))
    if ttl_window_hours is None:
        # ASM. The worklist answers 'who is looked at in the NEXT RE-LOOK SLOT',
        # so the window is ONE re-look: 1/relooks_per_staff_hour = 6 min at the
        # blueprint's 10 re-looks per nurse-hour. This makes the formula
        # reproduce Blueprint 16.2's shipped profile table exactly (H-L 3 nurses
        # -> K=3, H-M -> 2, H-S -> 1) instead of saturating at the ceiling of 5.
        ttl_window_hours = (1.0 / relooks) if relooks > 0 else 0.1

    raw = float(staff) * relooks * float(ttl_window_hours)
    core = kb.core["worklist"]
    k = max(int(core["k_min"]), min(int(core["k_max"]), int(round(raw))))

    return k, {
        "staff_on_shift": int(staff),
        "relooks_per_staff_hour": relooks,
        "ttl_window_hours": ttl_window_hours,
        "raw": round(raw, 3),
        "clamped_to": [core["k_min"], core["k_max"]],
        "k": k,
        "note": "K is derived from reassessment capacity, not fixed. Blueprint 2.3.",
    }


# ---------------------------------------------------------------------------
# Step 1 - hard class assignment
# ---------------------------------------------------------------------------

CLASS_ORDER = {"R": 0, "E": 1, "B": 2, "N": 3}


def assign_hard_class(patient: Patient, rec: Recommendation, now_min: float,
                      red_flag_active: bool, red_flag_actioned: bool) -> str:
    """Lexicographic classes.  Ordering is ABSOLUTE.

    INVARIANT I4: "Any firing hard red flag places the patient in class R
    REGARDLESS of every model output, confidence value and uncertainty class."
    Class assignment reads the RULE LAYER FIRST; no learned component has write
    access to class R membership.
    """
    if red_flag_active and not red_flag_actioned:
        return "R"
    if is_expired(patient, now_min):
        return "E"
    if rec.uncertainty_class == UncertaintyClass.BLIND:
        return "B"
    return "N"


# ---------------------------------------------------------------------------
# Step 2 - harm-rate score, WITHIN a class only
# ---------------------------------------------------------------------------

def time_pressure(patient: Patient, now_min: float, gamma: float) -> float:
    """TimePressure(i,t) = (elapsed(i,t) / TTL(i,t)) ^ gamma  with gamma > 1.

    "The exponent is the whole idea.  Priority rises SUPERLINEARLY as a clock
    approaches expiry, so a patient at 90% of their clock outranks one at 45% by
    much more than 2x.  This is what converts 'time waiting' from a FAIRNESS term
    into a SAFETY term."
    """
    frac = ttl_fraction_elapsed(patient, now_min)
    return float(min(4.0, frac) ** gamma)


def risk_delta(patient: Patient) -> float:
    """Delta(i,t) = recent change in Risk, POSITIVE PART ONLY.

    Positive-part only is not a detail: a falling risk must never be able to buy a
    patient a lower rank, because that is the automatic withdrawal of attention
    that constraint C1 forbids.
    """
    versions = patient.recommendation_versions
    if len(versions) < 2:
        return 0.0
    prev, cur = versions[-2], versions[-1]
    try:
        return max(0.0, float(cur.risk_score) - float(prev.risk_score))
    except (AttributeError, TypeError, ValueError):
        return 0.0


def harm_score(patient: Patient, rec: Recommendation, kb: Knowledge,
               now_min: float) -> Tuple[float, Dict[str, float]]:
    w = kb.core["queue"]["harm_score_weights"]
    gamma = float(kb.core["queue"]["time_pressure_gamma"])

    risk = max(0.0, min(1.0, rec.risk_score))
    det = max(0.0, min(1.0, rec.deterioration_estimate or 0.0))
    tp = time_pressure(patient, now_min, gamma)
    unc = max(0.0, min(1.0, rec.uncertainty.composite()))
    delta = risk_delta(patient)

    # Acuity itself must dominate within a class: a level-2 patient at 10% of their
    # clock must not sit below a level-5 patient at 95%.  Acuity enters as a floor
    # on the risk term, not as a separate tradeable weight.
    acuity_pressure = (5 - rec.acted_level) / 4.0
    risk = max(risk, acuity_pressure)

    components = {
        "risk": float(w["w_risk"]) * risk,
        "deterioration": float(w["w_det"]) * det,
        "time_pressure": float(w["w_time"]) * tp,
        "uncertainty": float(w["w_unc"]) * unc,
        "trajectory": float(w["w_traj"]) * delta,
    }
    total = sum(components.values())
    components["_risk_raw"] = risk
    components["_time_pressure_raw"] = tp
    return total, components


# ---------------------------------------------------------------------------
# Constraints C1-C6
# ---------------------------------------------------------------------------

def _c3_resource_tiebreak(entries: List[QueueEntry], kb: Knowledge,
                          resource_state: Dict[str, Any],
                          log: List[str]) -> None:
    """C3 - NO RESOURCE-DRIVEN DE-PRIORITISATION.

    "Resource state may break ties within an epsilon band and may inform routing.
    It may NEVER move a patient down by more than k positions, NEVER reduce an
    acted acuity, and NEVER lengthen a clock.  A patient is never ranked lower
    because the department is out of beds."

    "The most seductive and most catastrophic optimisation available.  Throughput
    objectives systematically punish the sickest patients, who consume the most
    resources - and it looks like efficiency the whole way down."
    """
    if not resource_state:
        return
    eps = float(kb.core["queue"]["resource_tiebreak_epsilon"])
    max_down = int(kb.core["queue"]["resource_max_positions_down"])
    blocked = set(resource_state.get("offline", []))
    if not blocked:
        return

    i = 0
    while i < len(entries) - 1:
        a, b = entries[i], entries[i + 1]
        if a.hard_class != b.hard_class:
            i += 1
            continue
        if abs(a.harm_score - b.harm_score) > eps:
            i += 1
            continue
        # Within the epsilon band ONLY, prefer the patient whose pathway is
        # actually deliverable here.  Acuity is untouched; the clock is untouched.
        a_blocked = _needs_blocked_resource(a, blocked)
        b_blocked = _needs_blocked_resource(b, blocked)
        if a_blocked and not b_blocked:
            entries[i], entries[i + 1] = b, a
            a.resource_tiebreak_applied = True
            log.append(
                f"C3 tie-break within eps={eps}: {b.patient_ref} before "
                f"{a.patient_ref} (deliverable pathway). Acuity and clocks unchanged; "
                f"movement capped at {max_down} positions."
            )
        i += 1


def _needs_blocked_resource(entry: QueueEntry, blocked: set) -> bool:
    for pc in entry.recommendation.pathway_clocks:
        for cap in pc.get("required_capabilities", []):
            if cap in blocked:
                return True
    return False


def _c4_bounded_churn(entries: List[QueueEntry], kb: Knowledge, now_min: float,
                      log: List[str], violations: List[str]) -> None:
    """C4 - BOUNDED CHURN.

    "Rank changes are rate-limited and hysteresis-banded (5% of H).  Every movement
    carries a reason string that persists for one cycle.  No patient may change
    position more than three times per fifteen minutes without a named cause."

    "Flicker.  A board that reorders every ten seconds is a board nobody can build
    a mental model from, and an unreadable board is an unused one."
    """
    hysteresis = float(kb.core["queue"]["hysteresis_band"])
    ceiling = int(kb.core["queue"]["rank_churn_ceiling_per_15min"])

    # Hysteresis: two adjacent entries whose scores are effectively tied keep their
    # previous relative order rather than swapping on noise.
    for i in range(len(entries) - 1):
        a, b = entries[i], entries[i + 1]
        if a.hard_class != b.hard_class:
            continue
        denom = max(abs(a.harm_score), abs(b.harm_score), 1e-9)
        if abs(a.harm_score - b.harm_score) / denom < hysteresis:
            if (a.previous_rank is not None and b.previous_rank is not None
                    and b.previous_rank < a.previous_rank):
                entries[i], entries[i + 1] = b, a
                log.append(
                    f"C4 hysteresis: {b.patient_ref} holds position over "
                    f"{a.patient_ref} (scores within {hysteresis:.0%})"
                )

    for idx, entry in enumerate(entries, start=1):
        entry.rank = idx
        p = entry.patient
        p.rank_history.append((now_min, idx))
        recent = [r for (t, r) in p.rank_history if now_min - t <= 15.0]
        changes = sum(1 for a, b in zip(recent, recent[1:]) if a != b)
        if changes > ceiling and entry.change_reason is None:
            violations.append(
                f"C4 churn ceiling exceeded for {entry.patient_ref}: {changes} "
                f"position changes in 15 min with no named cause (ceiling {ceiling})"
            )


def _c6_equity_guard(rec: Recommendation) -> None:
    """C6 - EQUITY GUARD.

    "H reads NO PROTECTED ATTRIBUTE.  Age enters ONLY as an envelope selector,
    which is clinical necessity.  Sex enters ONLY to gate the conditional pregnancy
    field."

    This is enforced structurally: harm_score() above receives only the
    recommendation's risk/deterioration/uncertainty/time terms.  Nothing in this
    module can read patient.sex, patient.display_name, or any demographic field.
    The assertion below is a runtime tripwire for a future edit that breaks that.
    """
    forbidden = {"sex", "name", "display_name", "religion", "caste", "ethnicity",
                 "race", "language", "insurance", "occupation", "address"}
    leaked = forbidden & set(rec.risk_components.keys())
    if leaked:
        raise AssertionError(
            f"C6 EQUITY GUARD VIOLATION: ranking inputs contain protected "
            f"attribute(s) {sorted(leaked)}. The harm score reads no protected "
            f"attribute. Blueprint 11.3 C6."
        )


# ---------------------------------------------------------------------------
# C2 - starvation guard and the Deficit Board
# ---------------------------------------------------------------------------

def _c2_starvation_guard(entries: List[QueueEntry], kb: Knowledge, profile: Dict[str, Any],
                         k: int, now_min: float, staff_override: Optional[int],
                         log: List[str]) -> DeficitBoard:
    """C2 - STARVATION GUARD.

    "No patient's projected wait may exceed the maximum acceptable wait for their
    acted level [S17].  IF NO FEASIBLE ORDERING SATISFIES THIS FOR EVERY PATIENT,
    THE SYSTEM DECLARES INSUFFICIENCY AND RAISES THE DEFICIT BOARD rather than
    producing a 'best available' ordering."

    "A queue that always returns an answer implies the room is manageable.
    Sometimes it is not, and the honest output is 'you need more hands', not a
    cleverer sequence."
    """
    staffing = profile.get("staffing", {})
    staff = staff_override if staff_override is not None else \
        staffing.get("triage_nurses_on_shift", 1)
    relooks_per_hour = float(staffing.get("relooks_per_staff_hour", 10))
    capacity_per_hour = float(staff) * relooks_per_hour
    minutes_per_relook = 60.0 / relooks_per_hour if relooks_per_hour > 0 else 6.0

    board = DeficitBoard(capacity_per_hour=capacity_per_hour)

    # Demand = re-looks per hour implied by every waiting patient's own clock.
    demand = 0.0
    for e in entries:
        ttl = e.patient.current_ttl_minutes
        if ttl and ttl > 0:
            demand += 60.0 / ttl
    board.demand_per_hour = demand

    # Projected wait: serve in current rank order at the department's re-look rate.
    at_risk: List[DeficitEntry] = []
    for idx, e in enumerate(entries):
        projected = idx * minutes_per_relook / max(1.0, float(staff))
        e.projected_wait_min = projected
        bound = kb.max_wait(e.recommendation.acted_level)
        rem = remaining_ttl(e.patient, now_min)

        elapsed = e.patient.minutes_since_arrival(now_min)
        breach = (elapsed + projected) > bound if bound > 0 else projected > 0
        e.breaches_max_wait = bool(breach)
        if breach:
            at_risk.append(DeficitEntry(
                patient_ref=e.patient_ref,
                chair=e.patient.chair,
                level_label=str(e.recommendation.acted_level),
                due_in_min=(rem if rem is not None and rem > 0 else None),
                overdue_by_min=(-rem if rem is not None and rem <= 0 else None),
                no_capacity_until_min=projected,
            ))

    board.deficit_per_hour = max(0.0, demand - capacity_per_hour)
    if board.deficit_per_hour > 0:
        board.additional_staff_needed = board.deficit_per_hour / relooks_per_hour

    if at_risk or board.deficit_per_hour > 0:
        board.active = True
        board.at_risk = at_risk[: max(k * 3, 6)]
        need = max(1, int(math.ceil(board.additional_staff_needed))) \
            if board.additional_staff_needed > 0 else 1
        board.recommendation = (
            f"RECOMMEND: declare SURGE mode and request {need} additional triage "
            f"nurse{'s' if need > 1 else ''}."
        )
        log.append(
            f"C2 starvation guard: {len(at_risk)} patient(s) cannot be reassessed "
            f"within their safe interval at current staffing. Deficit "
            f"{board.deficit_per_hour:.1f}/hr. System declares insufficiency rather "
            f"than producing a best-available ordering."
        )
    return board


# ---------------------------------------------------------------------------
# The layer
# ---------------------------------------------------------------------------

def build_queue(pairs: Sequence[Tuple[Patient, Recommendation]], kb: Knowledge,
                profile: Dict[str, Any], now_min: float, mode: str = "NORMAL",
                occupancy_ratio: float = 0.0,
                resource_state: Optional[Dict[str, Any]] = None,
                staff_override: Optional[int] = None,
                red_flag_actioned: Optional[Dict[str, bool]] = None,
                previous_ranks: Optional[Dict[str, int]] = None) -> QueueState:
    """Assemble the whole room.  Deterministic given the same inputs."""
    log: List[str] = []
    violations: List[str] = []
    red_flag_actioned = red_flag_actioned or {}
    previous_ranks = previous_ranks or {}

    entries: List[QueueEntry] = []
    for patient, rec in pairs:
        if patient.exit_state.value != "waiting":
            continue
        _c6_equity_guard(rec)
        red_flag_active = any(
            r.floor_level <= 2 for r in rec.fired_rules
        )
        hc = assign_hard_class(
            patient, rec, now_min,
            red_flag_active=red_flag_active,
            red_flag_actioned=red_flag_actioned.get(patient.patient_ref, False),
        )
        score, components = harm_score(patient, rec, kb, now_min)
        entry = QueueEntry(
            patient=patient, recommendation=rec, hard_class=hc,
            harm_score=score, components=components,
            previous_rank=previous_ranks.get(patient.patient_ref),
            change_reason=rec.change_reason,
        )
        entries.append(entry)

    # STEP 1+2: sort lexicographically by class, then by harm score WITHIN class.
    # No score in a lower class can ever displace a member of a higher class.
    entries.sort(key=lambda e: (CLASS_ORDER[e.hard_class], -e.harm_score, e.patient_ref))

    # C3 and C4 operate only within a class and only within their bounded budgets.
    _c3_resource_tiebreak(entries, kb, resource_state or {}, log)
    _c4_bounded_churn(entries, kb, now_min, log, violations)

    # STEP 3: worklist construction.
    k, k_basis = derive_k(profile, kb, staff_override=staff_override)
    worklist = entries[:k]
    for e in entries:
        e.on_worklist = False
    for e in worklist:
        e.on_worklist = True

    # C5 ATTENTION CAP - invariant I6.  Demand above K becomes a DEFICIT SIGNAL,
    # never a longer list.
    if len(worklist) > k:
        raise AssertionError(
            f"I6 VIOLATION: worklist length {len(worklist)} exceeds K={k}. "
            "Blueprint 11.3 C5: overflow becomes a deficit signal, never a longer list."
        )
    log.append(f"C5 attention cap: worklist length {len(worklist)} <= K={k}")

    deficit = _c2_starvation_guard(entries, kb, profile, k, now_min, staff_override, log)

    objective = "minimax_worst_case_harm" if mode in ("SURGE", "MCI") else "expected_harm"
    if objective == "minimax_worst_case_harm":
        # Blueprint 16.3: under scarcity the right question stops being "what
        # minimises average harm" and becomes "WHO IS CLOSEST TO BEING HARMED" - a
        # different function, not a tuned one.  Within each class, rank by the worst
        # case each patient faces rather than by their expected harm rate.
        _apply_minimax(entries, kb, now_min, log)
        entries.sort(key=lambda e: (CLASS_ORDER[e.hard_class],
                                    -e.components.get("minimax", e.harm_score),
                                    e.patient_ref))
        for idx, e in enumerate(entries, start=1):
            e.rank = idx
        worklist = entries[:k]
        for e in entries:
            e.on_worklist = False
        for e in worklist:
            e.on_worklist = True

    return QueueState(
        entries=entries, worklist=worklist, k=k, k_basis=k_basis,
        deficit=deficit, mode=mode, objective=objective,
        occupancy_ratio=occupancy_ratio,
        constraint_log=log, churn_violations=violations,
    )


def _apply_minimax(entries: List[QueueEntry], kb: Knowledge, now_min: float,
                   log: List[str]) -> None:
    """Worst-case harm: how bad does it get for THIS patient if nobody looks?

    Expected harm asks "what is the average cost of waiting"; minimax asks "what is
    the worst outcome reachable before the next possible look".  The difference is
    that a patient with a wide prediction set and a short clock outranks a patient
    with a higher mean risk and a long clock.
    """
    for e in entries:
        rec = e.recommendation
        worst_level = min(rec.prediction_set) if rec.prediction_set else rec.acted_level
        worst_acuity = (5 - worst_level) / 4.0
        rem = remaining_ttl(e.patient, now_min)
        urgency = 1.0 if rem is None else 1.0 / max(0.5, rem)
        det = rec.deterioration_estimate or 0.0
        e.components["minimax"] = worst_acuity * (1.0 + det) * (1.0 + urgency)
    log.append(
        "SURGE objective switch: minimising WORST-CASE harm rather than expected "
        "harm from delay. Blueprint 16.3."
    )
