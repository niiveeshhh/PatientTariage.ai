"""
L7 - Continuous reassessment.  Blueprint 12.

Round 2 requires the system to monitor patients already in the waiting queue and
trigger reassessment if wait time exceeds safe thresholds for their severity level
OR if vitals are re-recorded as worsening.  "Both are necessary; NEITHER IS
SUFFICIENT.  We implement FIVE trigger classes."

    T1 TIME         TTL reaches zero without a recorded reassessment.  Also fires at
                    80% of TTL as a pre-warning INSIDE the worklist ranking, so
                    expiry is ANTICIPATED rather than discovered.
    T2 EVENT        Any new observation; a red-flag rule newly fires; record data
                    resolves; a result returns.  Evaluated on BOTH absolute
                    thresholds AND envelope-normalised DELTAS.
    T3 OBSERVATION  A one-tap concern from any staff member, or from a parent,
                    carer or accompanying relative.  ESCALATE-ONLY.
    T4 QUEUE        Occupancy crosses a threshold; staffing changes; mode changes;
                    a resource goes offline.
    T5 SILENCE      No new data of any kind for a defined interval while a
                    data-completion task is open.  ABSENCE OF ACTIVITY IS ITSELF
                    THE EVENT.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.core.knowledge import Knowledge
from app.core.models import Observation, Patient
from app.clinical.layer1_envelope import EnvelopeSelection
from app.queue.ttl import is_expired, remaining_ttl, ttl_fraction_elapsed

# ASM parameters.
PRE_WARNING_FRACTION = 0.80         # Blueprint 12.2 T1: fires at 80% of TTL
SILENCE_INTERVAL_MIN = 30.0         # Blueprint 12.2 T5 worked example
GRACE_INTERVAL_MIN = 5.0            # Blueprint 12.1: charge-nurse escalation grace


@dataclass
class Trigger:
    trigger_class: str          # T1..T5
    reason: str
    escalate_only: bool = True
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trigger_class": self.trigger_class,
            "reason": self.reason,
            "escalate_only": self.escalate_only,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Envelope-normalised deltas.  Blueprint complexity 22: "Deltas are normalised to
# the age envelope: a 20-bpm rise means different things at 3 and 73."
# ---------------------------------------------------------------------------

# ASM: the fraction of an envelope's own threshold that constitutes a meaningful
# move.  Expressed as a fraction so the same rule transfers across age bands.
DELTA_RULES: Dict[str, Dict[str, float]] = {
    "heart_rate":       {"rise_fraction": 0.18, "fall_fraction": 0.25, "abs_floor": 12.0},
    "respiratory_rate": {"rise_fraction": 0.20, "fall_fraction": 0.30, "abs_floor": 3.0},
    "spo2":             {"fall_absolute": 3.0},
    "systolic_bp":      {"fall_fraction": 0.15, "abs_floor": 12.0},
    "temperature_c":    {"rise_absolute": 1.0},
    "capillary_refill_seconds": {"rise_absolute": 1.0},
}


def _envelope_reference(field_name: str, selection: EnvelopeSelection) -> Optional[float]:
    """The envelope's own threshold for this field, used to normalise a delta."""
    band = selection.age_band
    if band:
        if field_name == "heart_rate":
            return float(band.get("hr_high", 100))
        if field_name == "respiratory_rate":
            return float(band.get("rr_high", 20))
    if field_name == "heart_rate":
        return 100.0
    if field_name == "respiratory_rate":
        return 20.0
    if field_name == "systolic_bp":
        return 110.0
    return None


def detect_trend(patient: Patient, selection: EnvelopeSelection,
                 now_min: float, lookback_min: float = 60.0) -> List[Trigger]:
    """T2 on DELTAS.  Blueprint 9.6 "Normal now, deteriorating later":

    "Trend rules fire on deltas normalised to the envelope, EVEN THOUGH NO ABSOLUTE
    THRESHOLD IS CROSSED.  Patient moves to the top of the worklist with the delta
    as the stated reason."

    Blueprint scenario S-27: HR 92 -> 118 and SpO2 98 -> 93 over eighteen minutes.
    Neither value alone crosses an adult threshold; the TREND does.
    """
    triggers: List[Trigger] = []
    moves: List[str] = []
    detail: Dict[str, Any] = {}

    for field_name, rule in DELTA_RULES.items():
        series = [o for o in patient.history_for(field_name)
                  if o.value is not None and not o.quarantined
                  and now_min - o.timestamp_min <= lookback_min]
        if len(series) < 2:
            continue
        first, last = series[0], series[-1]
        try:
            delta = float(last.value) - float(first.value)
        except (TypeError, ValueError):
            continue
        span = last.timestamp_min - first.timestamp_min
        if span <= 0:
            continue

        ref = _envelope_reference(field_name, selection)
        fired = False
        direction = ""

        if "rise_fraction" in rule and ref:
            if delta >= rule["rise_fraction"] * ref and delta >= rule.get("abs_floor", 0):
                fired, direction = True, "rise"
        if not fired and "fall_fraction" in rule and ref:
            if -delta >= rule["fall_fraction"] * ref and -delta >= rule.get("abs_floor", 0):
                fired, direction = True, "fall"
        if not fired and "rise_absolute" in rule and delta >= rule["rise_absolute"]:
            fired, direction = True, "rise"
        if not fired and "fall_absolute" in rule and -delta >= rule["fall_absolute"]:
            fired, direction = True, "fall"

        if fired:
            label = _pretty(field_name)
            sign = "+" if delta > 0 else ""
            moves.append(f"{label} {sign}{delta:.0f}")
            detail[field_name] = {
                "from": first.value, "to": last.value,
                "delta": round(delta, 2), "span_min": round(span, 1),
                "direction": direction,
                "envelope_reference": ref,
            }

    if moves:
        span_min = max(
            (now_min - o.timestamp_min)
            for f in detail
            for o in patient.history_for(f)[:1]
        )
        triggers.append(Trigger(
            trigger_class="T2",
            reason=f"{', '.join(moves)} in {span_min:.0f} min",
            detail=detail,
        ))
    return triggers


def _pretty(field_name: str) -> str:
    return {
        "heart_rate": "HR", "respiratory_rate": "RR", "spo2": "SpO2",
        "systolic_bp": "BP", "temperature_c": "T",
        "capillary_refill_seconds": "cap refill",
    }.get(field_name, field_name)


# ---------------------------------------------------------------------------

def evaluate_triggers(patient: Patient, selection: EnvelopeSelection, kb: Knowledge,
                      now_min: float, new_observation: Optional[Observation] = None,
                      queue_event: Optional[str] = None,
                      previous_fired_rules: Optional[List[str]] = None,
                      current_fired_rules: Optional[List[str]] = None) -> List[Trigger]:
    """Evaluate all five classes.  Returns every trigger that fires - the system
    does not pick one."""
    triggers: List[Trigger] = []

    # --- T1 TIME -----------------------------------------------------------
    if is_expired(patient, now_min):
        rem = remaining_ttl(patient, now_min) or 0.0
        triggers.append(Trigger(
            trigger_class="T1",
            reason=f"decision expired {abs(rem):.0f} min ago",
            detail={"overdue_min": round(abs(rem), 2),
                    "escalate_to_charge_nurse_after_min": GRACE_INTERVAL_MIN},
        ))
    else:
        frac = ttl_fraction_elapsed(patient, now_min)
        if frac >= PRE_WARNING_FRACTION:
            rem = remaining_ttl(patient, now_min)
            triggers.append(Trigger(
                trigger_class="T1",
                reason=f"re-look due in {max(0.0, rem or 0.0):.0f} min",
                detail={"fraction_elapsed": round(frac, 3),
                        "pre_warning": True},
            ))

    # --- T2 EVENT ----------------------------------------------------------
    if new_observation is not None and new_observation.value is not None:
        triggers.append(Trigger(
            trigger_class="T2",
            reason=f"new {_pretty(new_observation.field_name)} recorded",
            detail={"field": new_observation.field_name,
                    "value": new_observation.value,
                    "provenance": new_observation.provenance.value},
        ))
    triggers.extend(detect_trend(patient, selection, now_min))

    if previous_fired_rules is not None and current_fired_rules is not None:
        newly = sorted(set(current_fired_rules) - set(previous_fired_rules))
        if newly:
            triggers.append(Trigger(
                trigger_class="T2",
                reason=f"red-flag rule newly firing: {', '.join(newly)}",
                detail={"new_rules": newly},
            ))

    # --- T3 OBSERVATION (escalate-only) ------------------------------------
    if patient.value("clinician_gestalt_concern"):
        triggers.append(Trigger(
            trigger_class="T3",
            reason="clinician concerned",
            detail={"source": "clinician_gestalt"},
        ))
    if patient.value("carer_concern"):
        triggers.append(Trigger(
            trigger_class="T3",
            reason="carer concerned",
            detail={"source": "carer_concern",
                    "citation": "UK national PEWS parent/carer concern trigger [S11]"},
        ))
    if patient.value("relative_reports_change"):
        triggers.append(Trigger(
            trigger_class="T3",
            reason=str(patient.value("relative_reports_change")),
            detail={"source": "accompanying_relative"},
        ))

    # --- T4 QUEUE ----------------------------------------------------------
    if queue_event:
        triggers.append(Trigger(
            trigger_class="T4",
            reason=queue_event,
            detail={"scope": "department"},
        ))

    # --- T5 SILENCE --------------------------------------------------------
    # "Absence of activity is itself the event."  This is how a patient who is
    # simply never returned to becomes VISIBLE.
    open_tasks = patient.open_task_list()
    silent_for = patient.minutes_since_data(now_min)
    if open_tasks and silent_for >= SILENCE_INTERVAL_MIN:
        task = open_tasks[0]
        what = task.field_name or task.kind
        triggers.append(Trigger(
            trigger_class="T5",
            reason=(f"no new data in {silent_for:.0f} min - "
                    f"{_pretty(what) if task.field_name else what} still outstanding"),
            detail={"silent_for_min": round(silent_for, 1),
                    "open_task": task.task_id,
                    "field": task.field_name},
        ))
    elif silent_for >= SILENCE_INTERVAL_MIN * 1.5:
        triggers.append(Trigger(
            trigger_class="T5",
            reason=f"no new data in {silent_for:.0f} min",
            detail={"silent_for_min": round(silent_for, 1)},
        ))

    return triggers


def dominant_trigger_reason(triggers: List[Trigger]) -> Optional[str]:
    """One line for the board.  Trend beats expiry beats silence beats the rest,
    because a change in the patient is more informative than the passage of time."""
    if not triggers:
        return None
    order = {"T2": 0, "T3": 1, "T1": 2, "T5": 3, "T4": 4}
    trend = [t for t in triggers if t.trigger_class == "T2" and "in " in t.reason]
    if trend:
        return trend[0].reason
    return sorted(triggers, key=lambda t: order.get(t.trigger_class, 9))[0].reason


def needs_charge_nurse_escalation(patient: Patient, now_min: float) -> bool:
    """Blueprint 12.1: "Expiry -> class E -> charge nurse notification after a
    grace interval -> departmental escalation.  AN ESCALATION CHAIN, NOT A
    REPEATING ALERT."  Repetition is how alerts get muted."""
    if not is_expired(patient, now_min):
        return False
    rem = remaining_ttl(patient, now_min)
    return rem is not None and abs(rem) >= GRACE_INTERVAL_MIN
