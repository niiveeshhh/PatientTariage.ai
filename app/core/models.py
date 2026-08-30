"""
Patient data model - Master Blueprint section 8.

The governing design rule, inherited from Round 1 and enforced architecturally here:
the system must produce a defensible output from the registration row alone, because
on the worst night that is all it will get.

Blueprint 6.1 design decision: "Model data as (value, provenance, timestamp, quality,
source-confidence) QUINTUPLES rather than as fields."  Every value in this module is
therefore an Observation, never a bare number.

Pure Python. No I/O, no network, no framework. Deterministic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Provenance lattice - Blueprint 8.5
# ---------------------------------------------------------------------------

class Provenance(str, Enum):
    """Blueprint 8.5. Strictly ordered, most to least trusted.

    Dev   device-measured, quality flag clean
    Obs   clinician-observed or clinician-measured
    Rec   record-derived, identity MATCHED
    Att   attendant/relative-stated  (common in Indian practice; distinct from Pt)
    Pt    patient-stated
    RecP  record-derived, identity PROVISIONAL  (may raise risk only; may never reassure)
    Der   model-derived  (never an input to another model decision)
    Unk   provenance unknown  (treated as Pt, the least trusted plausible class)
    """
    DEV = "Dev"
    OBS = "Obs"
    REC = "Rec"
    ATT = "Att"
    PT = "Pt"
    REC_PROVISIONAL = "RecProvisional"
    DER = "Der"
    UNK = "Unk"


# Rank 0 == most trusted. Used for weighting and for uncertainty component U3.
PROVENANCE_RANK: Dict[Provenance, int] = {
    Provenance.DEV: 0,
    Provenance.OBS: 1,
    Provenance.REC: 2,
    Provenance.ATT: 3,
    Provenance.PT: 4,
    Provenance.REC_PROVISIONAL: 5,
    Provenance.DER: 6,
    Provenance.UNK: 7,
}

# Blueprint 8.5 rule P1: a recommendation whose reassuring evidence rests only on
# {Pt, Att, Unk} can never reach uncertainty class CLEAR.
WEAK_REASSURANCE_CLASSES = frozenset(
    {Provenance.PT, Provenance.ATT, Provenance.UNK, Provenance.REC_PROVISIONAL}
)


def effective_provenance(p: Optional[Provenance]) -> Provenance:
    """Blueprint 8.5: unknown provenance is treated as Pt, the least trusted
    plausible class, never the most."""
    if p is None or p == Provenance.UNK:
        return Provenance.PT
    return p


# ---------------------------------------------------------------------------
# Measurement quality and missingness - Blueprint 8.7
# ---------------------------------------------------------------------------

class Quality(str, Enum):
    """Blueprint 8.3 measurement_quality. Assume SUSPECT when unknown - the
    conservative default."""
    CLEAN = "clean"
    SUSPECT = "suspect"
    ARTEFACT = "artefact"
    IMPOSSIBLE = "impossible"


class MissingReason(str, Enum):
    """Blueprint 8.7 / complexity 5: missing != null. Missing carries a reason.

    NOT_APPLICABLE is the ONLY reason that carries no completeness penalty, and it
    is only valid where the envelope says so (e.g. BP in a small child, where
    capillary refill substitutes).
    """
    NOT_YET_TAKEN = "not_yet_taken"
    REFUSED = "refused"
    NOT_OBTAINABLE = "not_obtainable"
    DEVICE_FAILED = "device_failed"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class DataCondition(str, Enum):
    """Blueprint 8.7 - the four defined behaviours. All four escalate."""
    PRESENT = "present"
    ABSENT = "absent"
    STALE = "stale"
    UNRELIABLE = "unreliable"


# ---------------------------------------------------------------------------
# Identity - Blueprint 8.1 / complexity 6
# ---------------------------------------------------------------------------

class MatchState(str, Enum):
    """Three identity states. Blueprint complexity 6: the dangerous case is not
    NO record - it is a CONFIDENT WRONG record, which imports someone else's
    baseline and normalises an abnormal reading."""
    MATCHED = "MATCHED"
    PROVISIONAL = "PROVISIONAL"
    UNMATCHED = "UNMATCHED"


class AgeSource(str, Enum):
    DOCUMENT = "document"
    STATED = "stated"
    ATTENDANT = "attendant"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class ArrivalMode(str, Enum):
    AMBULANCE = "ambulance"
    WALK_IN = "walk_in"
    REFERRED = "referred"
    POLICE = "police"
    INTER_FACILITY = "inter_facility_transfer"


class CommunicationBarrier(str, Enum):
    NONE = "none"
    LANGUAGE = "language"
    COGNITION = "cognition"
    CONSCIOUSNESS = "consciousness"
    PRE_VERBAL = "pre_verbal"
    PHYSICAL = "physical"


class PregnancyStatus(str, Enum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class ExitState(str, Enum):
    """Blueprint section 5 item 18: a patient who leaves is not a resolved patient.
    The queue must distinguish 'gone' from 'seen'."""
    WAITING = "waiting"
    SEEN = "seen"
    LEFT_WITHOUT_BEING_SEEN = "left_without_being_seen"
    TRANSFERRED = "transferred"
    ABSCONDED = "absconded"


# ---------------------------------------------------------------------------
# The quintuple
# ---------------------------------------------------------------------------

@dataclass
class Observation:
    """The (value, provenance, timestamp, quality, source-confidence) quintuple of
    Blueprint 6.1.  A value without these four companions is not admissible input.
    """
    field_name: str
    value: Any
    provenance: Provenance = Provenance.UNK
    timestamp_min: float = 0.0          # simulated minutes since epoch (monotonic)
    quality: Quality = Quality.SUSPECT
    source_confidence: float = 0.5      # 0..1
    missing_reason: Optional[MissingReason] = None
    quarantined: bool = False
    quarantine_note: Optional[str] = None
    device_id: Optional[str] = None

    # ------------------------------------------------------------------
    @property
    def is_present(self) -> bool:
        return self.value is not None and not self.quarantined

    def age_minutes(self, now_min: float) -> float:
        return max(0.0, now_min - self.timestamp_min)

    def freshness_weight(self, now_min: float, half_life_min: Optional[float]) -> float:
        """Blueprint 8.6: beyond its half-life a value's weight decays toward
        UNKNOWN - which raises uncertainty and shortens the clock - and never
        toward normal.  Exponential decay with the given half-life.

        half_life_min of None means the value never expires (e.g. onset time).
        """
        if half_life_min is None:
            return 1.0
        if half_life_min <= 0:
            return 1.0
        age = self.age_minutes(now_min)
        return float(0.5 ** (age / half_life_min))

    def condition(self, now_min: float, half_life_min: Optional[float]) -> DataCondition:
        """Blueprint 8.7 - which of the four defined behaviours applies."""
        if self.quality == Quality.IMPOSSIBLE or self.quarantined:
            return DataCondition.UNRELIABLE
        if self.value is None:
            return DataCondition.ABSENT
        if half_life_min is not None and half_life_min > 0:
            age = self.age_minutes(now_min)
            if age >= 2 * half_life_min:
                # Blueprint 8.7: past 2x half-life it BECOMES Absent and
                # generates a re-measure task.
                return DataCondition.ABSENT
            if age > half_life_min:
                return DataCondition.STALE
        if self.quality in (Quality.ARTEFACT, Quality.SUSPECT):
            return DataCondition.UNRELIABLE
        return DataCondition.PRESENT

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["provenance"] = self.provenance.value
        d["quality"] = self.quality.value
        d["missing_reason"] = self.missing_reason.value if self.missing_reason else None
        return d


def absent(field_name: str, reason: MissingReason,
           now_min: float = 0.0) -> Observation:
    """Construct an explicitly-absent field.  Blueprint 8.7: missing is never
    imputed to a population mean or a normal value."""
    return Observation(
        field_name=field_name,
        value=None,
        provenance=Provenance.UNK,
        timestamp_min=now_min,
        quality=Quality.SUSPECT,
        source_confidence=0.0,
        missing_reason=reason,
    )


# ---------------------------------------------------------------------------
# Identity link
# ---------------------------------------------------------------------------

@dataclass
class IdentityLink:
    """Blueprint 8.1 / complexity 6.  Every EHR link carries an identity
    confidence score and the fields it matched on."""
    match_state: MatchState = MatchState.UNMATCHED
    identity_confidence: float = 0.0
    matched_fields: List[str] = field(default_factory=list)
    candidate_record_ids: List[str] = field(default_factory=list)
    resolved_at_min: Optional[float] = None
    confirmed_by_actor: Optional[str] = None
    rejected: bool = False

    @property
    def may_reassure(self) -> bool:
        """Blueprint 8.5 rule P3: RecProvisional may increase risk or shorten a
        clock.  It may never decrease risk, lengthen a clock, or lower an acted
        acuity level."""
        return self.match_state == MatchState.MATCHED and not self.rejected

    def record_provenance(self) -> Provenance:
        if self.match_state == MatchState.MATCHED and not self.rejected:
            return Provenance.REC
        if self.match_state == MatchState.PROVISIONAL:
            return Provenance.REC_PROVISIONAL
        return Provenance.UNK


# ---------------------------------------------------------------------------
# Open tasks - Blueprint complexity 5 "data-completion task with its own deadline"
# ---------------------------------------------------------------------------

@dataclass
class OpenTask:
    task_id: str
    kind: str                    # "measure" | "re_measure" | "verify" | "confirm_identity"
    field_name: Optional[str]
    opened_at_min: float
    deadline_min: float
    reason: str
    closed_at_min: Optional[float] = None
    closed_by: Optional[str] = None

    @property
    def is_open(self) -> bool:
        return self.closed_at_min is None

    def is_overdue(self, now_min: float) -> bool:
        return self.is_open and now_min > self.deadline_min


# ---------------------------------------------------------------------------
# Time-critical pathway clock - Blueprint section 5 item 6
# ---------------------------------------------------------------------------

@dataclass
class PathwayClock:
    """Blueprint 5 item 6: 'the clock belongs to the disease, not the queue.'
    Displayed independently of the TTL and OUTSIDE the optimiser's authority.
    The queue may not trade a thrombolysis window against anything."""
    pathway_id: str
    name: str
    opened_at_min: float
    window_minutes: float
    clock_origin_min: float          # onset time if known, else arrival
    origin_is_known: bool
    required_capabilities: List[str] = field(default_factory=list)
    capability_available: bool = True
    transfer_consideration: bool = False
    source: str = ""

    def remaining_minutes(self, now_min: float) -> Optional[float]:
        if not self.origin_is_known:
            return None
        return self.window_minutes - (now_min - self.clock_origin_min)

    def elapsed_minutes(self, now_min: float) -> float:
        return now_min - self.clock_origin_min


# ---------------------------------------------------------------------------
# Patient
# ---------------------------------------------------------------------------

@dataclass
class Patient:
    """Blueprint 8.  Fields are grouped by the WINDOW in which the information can
    realistically exist: registration (t~0), first 60 seconds, first 5 minutes.
    Later-assessment data (labs, ECG, imaging) is architecturally EXCLUDED from
    arrival logic (Blueprint 8.4) and consumed only as reassessment input.
    """
    # --- registration window (t ~ 0) --------------------------------------
    patient_ref: str                                  # pseudonymous identifier
    arrival_timestamp_min: float                      # monotonic source
    display_name: str = ""                            # ONLY for role-scoped views
    chair: str = ""                                   # physical location label

    age_days: Optional[int] = None
    age_source: AgeSource = AgeSource.UNKNOWN
    age_estimated: bool = False
    sex: str = "unknown"                              # gates the pregnancy field ONLY
    arrival_mode: ArrivalMode = ArrivalMode.WALK_IN
    arrival_mode_assumed: bool = False

    record_id: Optional[str] = None
    identity: IdentityLink = field(default_factory=IdentityLink)

    stated_chief_complaint: str = ""
    complaint_concepts: List[str] = field(default_factory=list)
    complaint_ambiguous: bool = False
    complaint_unmapped: bool = False

    communication_barrier: bool = False
    communication_barrier_kind: CommunicationBarrier = CommunicationBarrier.NONE
    self_report_channel_available: bool = True

    # --- record-derived (only usable per rule P3) --------------------------
    prior_ed_visits_90d: Optional[int] = None
    known_conditions: List[str] = field(default_factory=list)
    baseline_systolic_bp: Optional[int] = None
    baseline_bp_age_days: Optional[int] = None
    baseline_oriented: Optional[bool] = None
    rate_control_medication: Optional[bool] = None    # None == unknown
    frailty_indicator: Optional[bool] = None
    immunisations_incomplete: Optional[bool] = None
    spinal_cord_injury: bool = False

    # --- observations: field_name -> Observation ---------------------------
    observations: Dict[str, Observation] = field(default_factory=dict)

    # --- observation history for trend detection (L7) -----------------------
    observation_history: List[Observation] = field(default_factory=list)

    # --- state -------------------------------------------------------------
    pregnancy_status: PregnancyStatus = PregnancyStatus.NOT_APPLICABLE
    exit_state: ExitState = ExitState.WAITING
    exit_at_min: Optional[float] = None
    open_tasks: List[OpenTask] = field(default_factory=list)
    pathway_clocks: List[PathwayClock] = field(default_factory=list)

    # human decision - Blueprint 8.3 nurse_assigned_acuity
    nurse_assigned_acuity: Optional[int] = None
    nurse_assigned_at_min: Optional[float] = None
    nurse_assigned_by: Optional[str] = None

    # last human reassessment (drives class E / trigger T1)
    last_reassessed_at_min: Optional[float] = None
    last_reassessed_by: Optional[str] = None

    # last time ANY new data arrived (drives trigger T5 silence)
    last_data_at_min: Optional[float] = None

    # ratchet state - Blueprint 13.1 invariant I1/I2
    current_ttl_minutes: Optional[float] = None
    ttl_set_at_min: Optional[float] = None
    ttl_lengthened_by_human: List[Tuple[float, str]] = field(default_factory=list)

    # hidden simulator truth - NEVER read by the engine (Blueprint 18 G3/G4)
    _latent: Optional[Dict[str, Any]] = None

    # bookkeeping
    recommendation_versions: List[Any] = field(default_factory=list)
    rank_history: List[Tuple[float, int]] = field(default_factory=list)
    scenario_id: Optional[str] = None
    synthetic: bool = True

    # ------------------------------------------------------------------
    def age_years(self) -> Optional[float]:
        if self.age_days is None:
            return None
        return self.age_days / 365.25

    @property
    def age_known(self) -> bool:
        return self.age_days is not None and self.age_source != AgeSource.UNKNOWN

    def get(self, field_name: str) -> Optional[Observation]:
        return self.observations.get(field_name)

    def value(self, field_name: str) -> Any:
        obs = self.observations.get(field_name)
        if obs is None or obs.quarantined:
            return None
        return obs.value

    def set_observation(self, obs: Observation) -> None:
        """Record a new observation.  Blueprint 12.3 step 1: new observations enter
        through L0 with provenance, timestamp and quality flags.  Prior values are
        pushed to history for trend detection - nothing is overwritten silently."""
        prior = self.observations.get(obs.field_name)
        if prior is not None:
            self.observation_history.append(prior)
        self.observations[obs.field_name] = obs
        if obs.value is not None:
            if self.last_data_at_min is None or obs.timestamp_min > self.last_data_at_min:
                self.last_data_at_min = obs.timestamp_min

    def history_for(self, field_name: str) -> List[Observation]:
        """Chronological series for one field, including the current value."""
        series = [o for o in self.observation_history if o.field_name == field_name]
        cur = self.observations.get(field_name)
        if cur is not None:
            series = series + [cur]
        return sorted(series, key=lambda o: o.timestamp_min)

    def minutes_since_arrival(self, now_min: float) -> float:
        return max(0.0, now_min - self.arrival_timestamp_min)

    def minutes_since_data(self, now_min: float) -> float:
        base = self.last_data_at_min
        if base is None:
            base = self.arrival_timestamp_min
        return max(0.0, now_min - base)

    def open_task_list(self) -> List[OpenTask]:
        return [t for t in self.open_tasks if t.is_open]

    def add_task(self, task: OpenTask) -> None:
        for existing in self.open_tasks:
            if existing.is_open and existing.kind == task.kind and existing.field_name == task.field_name:
                return
        self.open_tasks.append(task)

    def close_tasks_for(self, field_name: str, now_min: float, actor: str) -> List[str]:
        """Blueprint 12.3 step 8: any open data-completion task satisfied by the
        new data is closed and logged."""
        closed = []
        for t in self.open_tasks:
            if t.is_open and t.field_name == field_name:
                t.closed_at_min = now_min
                t.closed_by = actor
                closed.append(t.task_id)
        return closed

    # ------------------------------------------------------------------
    def snapshot(self, now_min: float) -> Dict[str, Any]:
        """The input_snapshot for the audit record - Blueprint 15.1: 'the single
        most important field.  Without it, no past decision can be reproduced or
        defended.'  Every field used, with value, provenance, timestamp and
        quality flag.  Deterministic key ordering so hashes are stable.
        """
        obs = {}
        for name in sorted(self.observations):
            o = self.observations[name]
            obs[name] = {
                "value": o.value,
                "prov": o.provenance.value,
                "t": round(o.timestamp_min, 4),
                "q": o.quality.value,
                "conf": round(o.source_confidence, 4),
                "missing_reason": o.missing_reason.value if o.missing_reason else None,
                "quarantined": o.quarantined,
                "age_min": round(o.age_minutes(now_min), 3),
            }
        return {
            "patient_ref": self.patient_ref,
            "arrival_timestamp_min": round(self.arrival_timestamp_min, 4),
            "now_min": round(now_min, 4),
            "age_days": self.age_days,
            "age_source": self.age_source.value,
            "age_estimated": self.age_estimated,
            "sex": self.sex,
            "arrival_mode": self.arrival_mode.value,
            "pregnancy_status": self.pregnancy_status.value,
            "communication_barrier": self.communication_barrier,
            "communication_barrier_kind": self.communication_barrier_kind.value,
            "self_report_channel_available": self.self_report_channel_available,
            "stated_chief_complaint": self.stated_chief_complaint,
            "complaint_concepts": sorted(self.complaint_concepts),
            "complaint_ambiguous": self.complaint_ambiguous,
            "complaint_unmapped": self.complaint_unmapped,
            "identity": {
                "match_state": self.identity.match_state.value,
                "identity_confidence": round(self.identity.identity_confidence, 4),
                "matched_fields": sorted(self.identity.matched_fields),
                "rejected": self.identity.rejected,
            },
            "record_derived": {
                "prior_ed_visits_90d": self.prior_ed_visits_90d,
                "known_conditions": sorted(self.known_conditions),
                "baseline_systolic_bp": self.baseline_systolic_bp,
                "baseline_oriented": self.baseline_oriented,
                "rate_control_medication": self.rate_control_medication,
                "frailty_indicator": self.frailty_indicator,
                "immunisations_incomplete": self.immunisations_incomplete,
                "spinal_cord_injury": self.spinal_cord_injury,
            },
            "observations": obs,
            "open_tasks": sorted(
                [t.task_id for t in self.open_tasks if t.is_open]
            ),
        }


# ---------------------------------------------------------------------------
# Uncertainty + recommendation output objects
# ---------------------------------------------------------------------------

class UncertaintyClass(str, Enum):
    """Blueprint 10.3 - four named states with DEFINED, TESTABLE behaviours.
    'Confidence: 87%' appears nowhere in the interface."""
    CLEAR = "CLEAR"
    THIN = "THIN"
    CONFLICTED = "CONFLICTED"
    BLIND = "BLIND"


UNCERTAINTY_PRECEDENCE = [
    UncertaintyClass.BLIND,
    UncertaintyClass.CONFLICTED,
    UncertaintyClass.THIN,
    UncertaintyClass.CLEAR,
]


def worse_uncertainty(a: UncertaintyClass, b: UncertaintyClass) -> UncertaintyClass:
    """Class precedence BLIND > CONFLICTED > THIN > CLEAR (Blueprint 21.1)."""
    return a if UNCERTAINTY_PRECEDENCE.index(a) <= UNCERTAINTY_PRECEDENCE.index(b) else b


@dataclass
class UncertaintyComponents:
    """Blueprint 10.1 - the five orthogonal components.  Each is in [0, 1] where
    HIGHER MEANS MORE UNCERTAIN.  They fail independently and demand different
    responses, which is why collapsing them into one percentage discards exactly
    the information a nurse would use to decide what to do next."""
    u1_completeness: float = 0.0
    u2_freshness: float = 0.0
    u3_provenance: float = 0.0
    u4_coherence: float = 0.0
    u5_model_applicability: float = 0.0

    # supporting detail, shown one tap away
    completeness_score: float = 1.0
    missing_critical1: List[str] = field(default_factory=list)
    missing_fields: Dict[str, str] = field(default_factory=dict)
    stale_fields: Dict[str, float] = field(default_factory=dict)
    weak_provenance_fields: List[str] = field(default_factory=list)
    reassurance_is_self_reported_only: bool = False
    out_of_distribution: bool = False
    applicability_exclusions: List[str] = field(default_factory=list)

    def composite(self) -> float:
        """Escalate-only aggregate used by the queue's harm score (w_unc term).
        Deliberately a max-biased blend: a single blown component must not be
        averaged away by four healthy ones."""
        vals = [self.u1_completeness, self.u2_freshness, self.u3_provenance,
                self.u4_coherence, self.u5_model_applicability]
        return max(vals) * 0.6 + (sum(vals) / len(vals)) * 0.4

    def to_dict(self) -> Dict[str, Any]:
        return {
            "U1_completeness": round(self.u1_completeness, 4),
            "U2_freshness": round(self.u2_freshness, 4),
            "U3_provenance": round(self.u3_provenance, 4),
            "U4_coherence": round(self.u4_coherence, 4),
            "U5_model_applicability": round(self.u5_model_applicability, 4),
            "completeness_score": round(self.completeness_score, 4),
            "missing_critical1": sorted(self.missing_critical1),
            "missing_fields": dict(sorted(self.missing_fields.items())),
            "stale_fields": {k: round(v, 2) for k, v in sorted(self.stale_fields.items())},
            "weak_provenance_fields": sorted(self.weak_provenance_fields),
            "reassurance_is_self_reported_only": self.reassurance_is_self_reported_only,
            "out_of_distribution": self.out_of_distribution,
            "applicability_exclusions": sorted(self.applicability_exclusions),
        }


@dataclass
class Contradiction:
    """Blueprint 10.2.  The system NEVER auto-resolves.  A contradiction means one
    input is wrong and the system cannot know which - averaging two contradictory
    inputs produces a number that describes no patient."""
    detector_id: str
    name: str
    card_text: str
    values: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FiredRule:
    rule_id: str
    name: str
    floor_level: int
    reason_string: str
    action_verb: str
    source: str
    citation_text: str
    triggering_values: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Recommendation:
    """Blueprint 10.6 - what the prototype outputs per patient.  The interface
    shows the first six without a click.

    DELIBERATELY ABSENT from every surface: a confidence percentage.
    """
    version: int
    patient_ref: str
    created_at_min: float

    acted_level: int                          # most acute member of the set
    point_estimate_level: int                 # what a plain argmax would have said
    prediction_set: List[int] = field(default_factory=list)
    rule_floor_level: Optional[int] = None

    uncertainty_class: UncertaintyClass = UncertaintyClass.CLEAR
    uncertainty: UncertaintyComponents = field(default_factory=UncertaintyComponents)
    contradictions: List[Contradiction] = field(default_factory=list)

    dominant_reason: str = ""
    secondary_reasons: List[str] = field(default_factory=list)
    action_verb: str = ""
    routing_suggestion: Optional[str] = None
    routing_blocked_reason: Optional[str] = None
    transfer_consideration: Optional[str] = None

    envelope_id: str = ""
    envelope_version: str = ""
    envelope_note: str = ""
    fired_rules: List[FiredRule] = field(default_factory=list)

    risk_score: float = 0.0
    risk_components: Dict[str, Any] = field(default_factory=dict)
    risk_suppressed: bool = False
    risk_suppression_reason: Optional[str] = None

    deterioration_estimate: Optional[float] = None
    model_used: bool = True
    model_rule_disagreement: bool = False

    ttl_minutes: Optional[float] = None
    ttl_basis: str = ""
    ttl_candidates: Dict[str, Optional[float]] = field(default_factory=dict)
    ttl_expires_at_min: Optional[float] = None

    pathway_clocks: List[Dict[str, Any]] = field(default_factory=list)

    escalation_premium: float = 0.0
    change_reason: Optional[str] = None

    engine_version: str = ""
    rule_version: str = ""
    model_version: str = ""
    calibration_id: str = ""
    alpha: float = 0.10
    degradation_rung: str = "L0_FULL"
    operating_mode: str = "NORMAL"
    snapshot_hash: str = ""
    input_snapshot: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "patient_ref": self.patient_ref,
            "created_at_min": round(self.created_at_min, 4),
            "acted_level": self.acted_level,
            "point_estimate_level": self.point_estimate_level,
            "prediction_set": sorted(self.prediction_set),
            "rule_floor_level": self.rule_floor_level,
            "uncertainty_class": self.uncertainty_class.value,
            "uncertainty": self.uncertainty.to_dict(),
            "contradictions": [
                {"detector_id": c.detector_id, "name": c.name,
                 "card_text": c.card_text, "values": c.values}
                for c in self.contradictions
            ],
            "dominant_reason": self.dominant_reason,
            "secondary_reasons": list(self.secondary_reasons),
            "action_verb": self.action_verb,
            "routing_suggestion": self.routing_suggestion,
            "routing_blocked_reason": self.routing_blocked_reason,
            "transfer_consideration": self.transfer_consideration,
            "envelope_id": self.envelope_id,
            "envelope_version": self.envelope_version,
            "envelope_note": self.envelope_note,
            "fired_rules": [
                {"rule_id": r.rule_id, "name": r.name, "floor_level": r.floor_level,
                 "reason_string": r.reason_string, "action_verb": r.action_verb,
                 "source": r.source, "citation_text": r.citation_text,
                 "triggering_values": r.triggering_values}
                for r in self.fired_rules
            ],
            "risk_score": round(self.risk_score, 4),
            "risk_components": self.risk_components,
            "risk_suppressed": self.risk_suppressed,
            "risk_suppression_reason": self.risk_suppression_reason,
            "deterioration_estimate": (
                round(self.deterioration_estimate, 4)
                if self.deterioration_estimate is not None else None
            ),
            "model_used": self.model_used,
            "model_rule_disagreement": self.model_rule_disagreement,
            "ttl_minutes": (round(self.ttl_minutes, 3)
                            if self.ttl_minutes is not None else None),
            "ttl_basis": self.ttl_basis,
            "ttl_candidates": {
                k: (round(v, 3) if v is not None else None)
                for k, v in sorted(self.ttl_candidates.items())
            },
            "ttl_expires_at_min": (round(self.ttl_expires_at_min, 4)
                                   if self.ttl_expires_at_min is not None else None),
            "pathway_clocks": self.pathway_clocks,
            "escalation_premium": round(self.escalation_premium, 4),
            "change_reason": self.change_reason,
            "engine_version": self.engine_version,
            "rule_version": self.rule_version,
            "model_version": self.model_version,
            "calibration_id": self.calibration_id,
            "alpha": self.alpha,
            "degradation_rung": self.degradation_rung,
            "operating_mode": self.operating_mode,
            "snapshot_hash": self.snapshot_hash,
        }


# ---------------------------------------------------------------------------
# Acuity helpers.  Level 1 is MOST acute.
# ---------------------------------------------------------------------------

ACUITY_LEVELS = [1, 2, 3, 4, 5]


def most_acute(levels) -> int:
    """Blueprint 10.4 step 5: ACTED LEVEL = min(S), where 'min' means the MOST
    ACUTE member."""
    return min(levels)


def is_at_least_as_acute(a: int, b: int) -> bool:
    """True when a is at least as acute as b (lower number == more acute)."""
    return a <= b


def clamp_level(level: int) -> int:
    return max(1, min(5, int(level)))
