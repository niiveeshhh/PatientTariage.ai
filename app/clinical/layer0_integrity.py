"""
L0 - Data integrity and identity gate.  Blueprint 9.2.

"Without it, an impossible value becomes a false emergency or is silently dropped,
and a wrong record match imports someone else's baseline. This layer is new in
Round 2 and it is the layer most teams will not have."

Two opposite failure modes must be avoided AT ONCE (Blueprint section 5 item 4):
  - treating an artefact as a genuine emergency (which trains staff to ignore the system)
  - silently discarding it (which can drop a real, extreme value)
The correct response is NEITHER TRUST NOR DELETION: quarantine, with a re-measure task.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.core.knowledge import Knowledge
from app.core.models import (
    DataCondition, IdentityLink, MatchState, MissingReason, Observation,
    OpenTask, Patient, Provenance, Quality, effective_provenance,
)


@dataclass
class IntegrityResult:
    quarantined: List[Tuple[str, Any, str]] = field(default_factory=list)
    downweighted: List[Tuple[str, Any, str]] = field(default_factory=list)
    implausible_but_possible: List[Tuple[str, Any]] = field(default_factory=list)
    opened_tasks: List[str] = field(default_factory=list)
    provenance_normalised: List[str] = field(default_factory=list)
    identity_state: MatchState = MatchState.UNMATCHED
    identity_confidence: float = 0.0
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "quarantined": [
                {"field": f, "value": v, "reason": r} for f, v, r in self.quarantined
            ],
            "downweighted": [
                {"field": f, "value": v, "reason": r} for f, v, r in self.downweighted
            ],
            "implausible_but_possible": [
                {"field": f, "value": v} for f, v in self.implausible_but_possible
            ],
            "opened_tasks": list(self.opened_tasks),
            "provenance_normalised": sorted(self.provenance_normalised),
            "identity_state": self.identity_state.value,
            "identity_confidence": round(self.identity_confidence, 4),
            "notes": list(self.notes),
        }


# Blueprint section 5 item 4: quarantined values get a re-measure task with a
# deliberately tight deadline, because the field is now a known unknown.
QUARANTINE_TASK_DEADLINE_MIN = 10.0     # ASM
MISSING_CRITICAL_TASK_DEADLINE_MIN = 15.0   # ASM, matches rule RF-X03


def _band_key_for_bounds(envelope_hint: str) -> str:
    """The plausibility table is keyed by three coarse envelopes."""
    if envelope_hint in ("paediatric",):
        return "paediatric"
    if envelope_hint in ("geriatric",):
        return "geriatric"
    return "adult"


def run_layer0(patient: Patient, kb: Knowledge, now_min: float,
               envelope_hint: str = "adult",
               history_available: bool = True) -> IntegrityResult:
    """Validate every value, quarantine the impossible, normalise provenance, and
    resolve the identity state.  Mutates the patient's observations in place
    (marking quarantine) and opens re-measure tasks.

    envelope_hint is the *provisional* envelope; L1 may refine it.  Plausibility
    bounds are age-dependent, so L0 needs a first guess - and a wrong guess here
    can only widen the plausible range, never narrow it below adult bounds.
    """
    result = IntegrityResult()
    bounds = kb.red_flags["physiological_plausibility_bounds"]
    band_key = _band_key_for_bounds(envelope_hint)

    for field_name in sorted(patient.observations):
        obs = patient.observations[field_name]

        # --- provenance normalisation (Blueprint 8.5) ----------------------
        eff = effective_provenance(obs.provenance)
        if eff != obs.provenance:
            obs.provenance = eff
            result.provenance_normalised.append(field_name)
            result.notes.append(
                f"{field_name}: provenance unknown, treated as patient-stated "
                f"(least trusted plausible class)"
            )

        if obs.value is None:
            continue

        spec = bounds.get(field_name)
        if spec is None:
            continue

        val = obs.value
        if not isinstance(val, (int, float)):
            continue

        imp = spec["impossible"]
        if val < imp["min"] or val > imp["max"]:
            # Blueprint 8.7: IMPOSSIBLE values are QUARANTINED, never deleted.
            # Deletion can drop a real extreme value; trust can trigger a false
            # emergency.  Quarantine avoids both.
            obs.quality = Quality.IMPOSSIBLE
            obs.quarantined = True
            obs.quarantine_note = (
                f"physiologically impossible ({val}); outside "
                f"[{imp['min']}, {imp['max']}] - unreliable, re-measure"
            )
            result.quarantined.append((field_name, val, obs.quarantine_note))
            task = OpenTask(
                task_id=f"{patient.patient_ref}:remeasure:{field_name}",
                kind="re_measure",
                field_name=field_name,
                opened_at_min=now_min,
                deadline_min=now_min + QUARANTINE_TASK_DEADLINE_MIN,
                reason=f"{field_name} reading impossible - re-measure",
            )
            patient.add_task(task)
            result.opened_tasks.append(task.task_id)
            continue

        implaus = spec.get("implausible", {}).get(band_key)
        if implaus and (val < implaus["min"] or val > implaus["max"]):
            # Blueprint section 5 item 4: "An implausible-but-possible extreme is
            # treated as REAL until re-measured."  We do NOT quarantine it; we
            # raise a verification task and mark it suspect so U3 rises.
            result.implausible_but_possible.append((field_name, val))
            if obs.quality == Quality.CLEAN:
                obs.quality = Quality.SUSPECT
                result.downweighted.append(
                    (field_name, val, "implausible-but-possible extreme; treated as "
                                      "real until re-measured")
                )
            task = OpenTask(
                task_id=f"{patient.patient_ref}:verify:{field_name}",
                kind="verify",
                field_name=field_name,
                opened_at_min=now_min,
                deadline_min=now_min + QUARANTINE_TASK_DEADLINE_MIN,
                reason=f"{field_name}={val} is an extreme value - confirm",
            )
            patient.add_task(task)
            result.opened_tasks.append(task.task_id)

        elif obs.quality == Quality.ARTEFACT:
            # Blueprint 8.7: artefactual values are DOWN-WEIGHTED but RETAINED.
            result.downweighted.append(
                (field_name, val, "quality flag artefact - down-weighted, retained")
            )
            task = OpenTask(
                task_id=f"{patient.patient_ref}:remeasure:{field_name}",
                kind="re_measure",
                field_name=field_name,
                opened_at_min=now_min,
                deadline_min=now_min + QUARANTINE_TASK_DEADLINE_MIN,
                reason=f"{field_name} measurement artefact - re-measure",
            )
            patient.add_task(task)
            result.opened_tasks.append(task.task_id)

    # --- staleness past 2x half-life becomes ABSENT with a task -------------
    for field_name in sorted(patient.observations):
        obs = patient.observations[field_name]
        if obs.value is None or obs.quarantined:
            continue
        hl = kb.half_life(field_name)
        cond = obs.condition(now_min, hl)
        if cond == DataCondition.ABSENT and hl:
            # Blueprint 8.7: "past 2x half-life it becomes Absent and generates a
            # task."  We do NOT null the value - the reviewer must still see what
            # was recorded - we open the task and let U1/U2 treat it as absent.
            task = OpenTask(
                task_id=f"{patient.patient_ref}:remeasure_stale:{field_name}",
                kind="re_measure",
                field_name=field_name,
                opened_at_min=now_min,
                deadline_min=now_min + QUARANTINE_TASK_DEADLINE_MIN,
                reason=(f"{field_name} last measured "
                        f"{obs.age_minutes(now_min):.0f} min ago "
                        f"(>2x its {hl:.0f} min half-life)"),
            )
            patient.add_task(task)
            result.opened_tasks.append(task.task_id)

    # --- identity gate (Blueprint complexity 6) -----------------------------
    resolve_identity(patient, now_min, history_available=history_available)
    result.identity_state = patient.identity.match_state
    result.identity_confidence = patient.identity.identity_confidence

    if patient.identity.match_state == MatchState.PROVISIONAL:
        result.notes.append(
            "Identity PROVISIONAL: record-derived data may raise risk only "
            "(provenance rule P3). It may never provide reassurance."
        )
        task = OpenTask(
            task_id=f"{patient.patient_ref}:confirm_identity",
            kind="confirm_identity",
            field_name=None,
            opened_at_min=now_min,
            deadline_min=now_min + MISSING_CRITICAL_TASK_DEADLINE_MIN,
            reason=(f"provisional match on "
                    f"{'+'.join(sorted(patient.identity.matched_fields)) or 'unknown fields'}"
                    f" - confirm or reject"),
        )
        patient.add_task(task)
        result.opened_tasks.append(task.task_id)

    return result


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

# ASM thresholds.  Blueprint complexity 6 defines three states and says the
# dangerous case is the confident wrong match; it does not publish cut-offs.
MATCH_CONFIDENCE_HIGH = 0.90
MATCH_CONFIDENCE_LOW = 0.50


def resolve_identity(patient: Patient, now_min: float,
                     history_available: bool = True) -> IdentityLink:
    """Blueprint complexity 6 and 13.3.

    Three states: MATCHED (high) / PROVISIONAL (ambiguous) / UNMATCHED.
    If identity cannot be resolved -> treat as zero-history and raise uncertainty;
    NEVER blend two candidate records.
    """
    link = patient.identity

    if not history_available:
        # Degradation rung L2 (NO-HISTORY): every patient becomes effectively
        # zero-history - a fully supported path, not an error state.
        link.match_state = MatchState.UNMATCHED
        link.identity_confidence = 0.0
        link.resolved_at_min = now_min
        return link

    if link.rejected:
        link.match_state = MatchState.UNMATCHED
        link.identity_confidence = 0.0
        link.resolved_at_min = now_min
        return link

    if len(link.candidate_record_ids) > 1:
        # Blueprint 13.3: duplicate registration -> both records surfaced; merge is
        # a human action with an audit entry.  NEVER auto-merged.
        link.match_state = MatchState.PROVISIONAL
        link.identity_confidence = min(link.identity_confidence, MATCH_CONFIDENCE_HIGH - 0.01)
        link.resolved_at_min = now_min
        return link

    if not link.candidate_record_ids and patient.record_id is None:
        link.match_state = MatchState.UNMATCHED
        link.identity_confidence = 0.0
        link.resolved_at_min = now_min
        return link

    if link.identity_confidence >= MATCH_CONFIDENCE_HIGH:
        link.match_state = MatchState.MATCHED
    elif link.identity_confidence >= MATCH_CONFIDENCE_LOW:
        link.match_state = MatchState.PROVISIONAL
    else:
        link.match_state = MatchState.UNMATCHED
        link.identity_confidence = 0.0
    link.resolved_at_min = now_min
    return link


def record_derived_provenance(patient: Patient) -> Provenance:
    """Which provenance class applies to anything read from the linked record."""
    return patient.identity.record_provenance()


def record_may_reassure(patient: Patient) -> bool:
    """Provenance rule P3, enforced at every read site rather than trusted to
    discipline.  Under PROVISIONAL, history may only RAISE risk."""
    return patient.identity.may_reassure


def reject_match(patient: Patient, now_min: float, actor: str) -> None:
    """Blueprint complexity 6: 'Match is a reversible, logged event; rejecting a
    match rewrites the risk read and is audited.'  The caller writes the audit
    record - this function only changes state."""
    patient.identity.rejected = True
    patient.identity.match_state = MatchState.UNMATCHED
    patient.identity.identity_confidence = 0.0
    patient.identity.confirmed_by_actor = actor
    patient.identity.resolved_at_min = now_min
    # Record-derived reassurance is withdrawn with the match.
    patient.baseline_systolic_bp = None
    patient.baseline_oriented = None
    patient.prior_ed_visits_90d = None


def confirm_match(patient: Patient, now_min: float, actor: str) -> None:
    patient.identity.rejected = False
    patient.identity.match_state = MatchState.MATCHED
    patient.identity.identity_confidence = 1.0
    patient.identity.confirmed_by_actor = actor
    patient.identity.resolved_at_min = now_min
