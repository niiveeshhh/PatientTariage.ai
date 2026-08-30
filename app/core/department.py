"""
The department - the runtime that owns the room.

Composes the per-patient engine (L0-L5) with L6 (queue), L7 (triggers) and L8
(human decision + audit).  This is where the AUDIT-BEFORE-DISPLAY ordering of
Blueprint 15.2 is enforced: nothing reaches a caller until its record is durable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.audit.chain import AccessLogChain, AuditChain, AuditStoreUnavailable
from app.audit.records import (
    ActorRole, ClinicianDecision, OverrideDirection, OverrideRecord, OverrideRefused,
    build_audit_payload, validate_override,
)
from app.core.clock import SimulationClock
from app.core.engine import EngineContext, commit_ttl, evaluate, freshness_summary
from app.core.knowledge import Knowledge, load_knowledge
from app.core.models import Patient, Recommendation, UncertaintyClass
from app.models.deterioration import DeteriorationEstimator
from app.queue.living_queue import QueueState, build_queue, derive_k
from app.queue.triggers import (
    Trigger, dominant_trigger_reason, evaluate_triggers, needs_charge_nurse_escalation,
)
from app.queue.ttl import human_lengthen_ttl, is_expired, remaining_ttl
from app.safety.degradation import DegradationState, protocol_snapshot
from app.uncertainty.conformal import CalibrationSet, drift_alarm, mean_set_width


@dataclass
class Department:
    kb: Knowledge
    profile: Dict[str, Any]
    clock: SimulationClock = field(default_factory=SimulationClock)
    degradation: DegradationState = field(default_factory=DegradationState)
    estimator: Optional[DeteriorationEstimator] = None
    calibrations: Dict[str, CalibrationSet] = field(default_factory=dict)
    audit: AuditChain = field(default_factory=AuditChain)
    access_log: AccessLogChain = field(default_factory=AccessLogChain)

    patients: Dict[str, Patient] = field(default_factory=dict)
    latest: Dict[str, Recommendation] = field(default_factory=dict)
    triggers: Dict[str, List[Trigger]] = field(default_factory=dict)
    red_flag_actioned: Dict[str, bool] = field(default_factory=dict)
    previous_ranks: Dict[str, int] = field(default_factory=dict)
    queue: Optional[QueueState] = None

    mode: str = "NORMAL"
    mode_declared_by: Optional[str] = None
    staff_override: Optional[int] = None
    offline_resources: List[str] = field(default_factory=list)
    displayed_recommendations: int = 0
    escalations: List[Dict[str, Any]] = field(default_factory=list)
    baseline_set_widths: List[List[int]] = field(default_factory=list)
    suspend_discretionary: bool = False

    # ------------------------------------------------------------------
    @property
    def now(self) -> float:
        return self.clock.now_min

    def waiting(self) -> List[Patient]:
        return [p for p in self.patients.values() if p.exit_state.value == "waiting"]

    def occupancy_ratio(self) -> float:
        """Normalised by capacity, not absolute counts - Blueprint complexity 10:
        "the single most important step for cross-hospital transfer"."""
        bays = float(self.profile["capacity"]["treatment_bays"])
        boarding = float(self.profile["capacity"].get("boarding_load_fraction", 0.0))
        effective = max(1.0, bays * (1.0 - boarding))
        return len(self.waiting()) / effective

    def context(self) -> EngineContext:
        prof = dict(self.profile)
        prof["_offline_resources"] = list(self.offline_resources)
        return EngineContext(
            kb=self.kb, profile=prof, now_min=self.now, mode=self.mode,
            occupancy_ratio=self.occupancy_ratio(), degradation=self.degradation,
            estimator=self.estimator, calibrations=self.calibrations,
            staff_override=self.staff_override,
        )

    # ------------------------------------------------------------------
    def admit(self, patient: Patient, actor: str = "engine",
              actor_role: ActorRole = ActorRole.ENGINE) -> Recommendation:
        self.patients[patient.patient_ref] = patient
        return self.recompute(patient, actor=actor, actor_role=actor_role)

    def recompute(self, patient: Patient, actor: str = "engine",
                  actor_role: ActorRole = ActorRole.ENGINE,
                  change_reason: Optional[str] = None,
                  new_observation=None, queue_event: Optional[str] = None
                  ) -> Recommendation:
        """Produce a new versioned recommendation, write its audit record DURABLY,
        and only then make it visible.  Blueprint 15.2."""
        if not self.degradation.spec.recommendations_produced:
            # Rungs L3/L4: no new recommendations.  The clock discipline continues
            # on the last human-assigned acuity at the published interval.
            return self.latest.get(patient.patient_ref)

        ctx = self.context()
        prev_rules = [r.rule_id for r in self.latest[patient.patient_ref].fired_rules] \
            if patient.patient_ref in self.latest else None

        rec, trace = evaluate(patient, ctx, change_reason=change_reason)
        commit_ttl(patient, rec, ctx)

        trig = evaluate_triggers(
            patient, trace.l1, self.kb, self.now,
            new_observation=new_observation, queue_event=queue_event,
            previous_fired_rules=prev_rules,
            current_fired_rules=[r.rule_id for r in rec.fired_rules])
        self.triggers[patient.patient_ref] = trig
        if change_reason is None:
            rec.change_reason = dominant_trigger_reason(trig) if prev_rules is not None else None

        payload = build_audit_payload(
            {**rec.to_dict(),
             "_input_snapshot": rec.input_snapshot,
             "_freshness_summary": freshness_summary(patient, self.kb, self.now)},
            patient_ref=patient.patient_ref, actor=actor, actor_role=actor_role,
            timestamp_min=self.now,
            queue_state=self._queue_snapshot_for(patient.patient_ref),
            reassessment_events=[t.to_dict() for t in trig],
            mode=self.mode, occupancy_ratio=ctx.occupancy_ratio,
            staffing=self.profile.get("staffing", {}),
            degradation_rung=self.degradation.rung,
            event_type="recommendation",
            clock_snapshot=self.clock.snapshot(),
        )
        # INVARIANT I7: written BEFORE display.  If this raises, nothing is shown.
        self.audit.append(payload)
        self.displayed_recommendations += 1
        self.latest[patient.patient_ref] = rec
        return rec

    def _queue_snapshot_for(self, ref: str) -> Dict[str, Any]:
        if self.queue is None:
            return {}
        for e in self.queue.entries:
            if e.patient_ref == ref:
                return {"hard_class": e.hard_class, "rank": e.rank,
                        "on_worklist": e.on_worklist, "k": self.queue.k,
                        "deficit_active": self.queue.deficit.active}
        return {"k": self.queue.k, "deficit_active": self.queue.deficit.active}

    # ------------------------------------------------------------------
    def rebuild_queue(self) -> QueueState:
        pairs = [(p, self.latest[p.patient_ref]) for p in self.waiting()
                 if p.patient_ref in self.latest and self.latest[p.patient_ref]]
        self.queue = build_queue(
            pairs, self.kb, self.profile, self.now, mode=self.mode,
            occupancy_ratio=self.occupancy_ratio(),
            resource_state={"offline": list(self.offline_resources)},
            staff_override=self.staff_override,
            red_flag_actioned=self.red_flag_actioned,
            previous_ranks=dict(self.previous_ranks),
        )
        self.previous_ranks = {e.patient_ref: e.rank for e in self.queue.entries}
        self._check_escalations()
        return self.queue

    def _check_escalations(self) -> None:
        """Blueprint 12.1: an ESCALATION CHAIN, not a repeating alert.  Each patient
        escalates to the charge nurse ONCE per expiry, then departmentally."""
        for p in self.waiting():
            if needs_charge_nurse_escalation(p, self.now):
                key = (p.patient_ref, round(p.ttl_set_at_min or 0.0, 2))
                if any(e["key"] == list(key) for e in self.escalations):
                    continue
                rem = remaining_ttl(p, self.now) or 0.0
                self.escalations.append({
                    "key": list(key), "patient_ref": p.patient_ref,
                    "at_min": round(self.now, 2),
                    "overdue_min": round(abs(rem), 2),
                    "to": "charge_nurse",
                    "reason": f"TTL expired {abs(rem):.0f} min ago, unacknowledged",
                })

    # ------------------------------------------------------------------
    def evaluate_mode(self) -> str:
        """Blueprint 16.3.  The system may AUTO-ENTER a more vigilant mode; only a
        HUMAN may LEAVE one."""
        occ = self.occupancy_ratio()
        st = self.profile["surge_thresholds"]
        order = {"NORMAL": 0, "STRAINED": 1, "SURGE": 2, "MCI": 3}

        resus = float(self.profile["capacity"]["resuscitation_bays"])
        critical_now = sum(1 for p in self.waiting()
                           if self.latest.get(p.patient_ref)
                           and self.latest[p.patient_ref].acted_level == 1)

        if occ >= float(st["mci_load_multiplier"]) * 0.9 or critical_now > resus:
            proposed = "MCI"
        elif occ >= float(st["surge_occupancy"]):
            proposed = "SURGE"
        elif occ >= float(st["strained_occupancy"]):
            proposed = "STRAINED"
        else:
            proposed = "NORMAL"

        if order[proposed] > order[self.mode]:
            self.set_mode(proposed, actor="engine", actor_role=ActorRole.ENGINE,
                          auto=True)
        return self.mode

    def set_mode(self, mode: str, actor: str, actor_role: ActorRole,
                 auto: bool = False) -> str:
        order = {"NORMAL": 0, "STRAINED": 1, "SURGE": 2, "MCI": 3}
        if auto and order[mode] < order[self.mode]:
            raise PermissionError(
                "Only a human may LEAVE a more vigilant mode. Blueprint 9.3: "
                "vigilance is free to increase itself and never free to decrease "
                "itself."
            )
        prev = self.mode
        self.mode = mode
        self.mode_declared_by = f"{actor_role.value}:{actor}"
        # Blueprint 16.3 SURGE: discretionary work is suspended BEFORE safety work.
        self.suspend_discretionary = mode in ("SURGE", "MCI")
        self.audit.append({
            "event_type": "mode_change", "patient_ref": None,
            "timestamp_min": round(self.now, 4), "actor": actor,
            "actor_role": actor_role.value, "from_mode": prev, "to_mode": mode,
            "automatic": auto, "occupancy_ratio": round(self.occupancy_ratio(), 4),
            "objective": ("minimax_worst_case_harm" if mode in ("SURGE", "MCI")
                          else "expected_harm"),
            "discretionary_work_suspended": self.suspend_discretionary,
        })
        return self.mode

    def mci_declaration(self) -> Optional[Dict[str, Any]]:
        """Blueprint 13.4: "The system declares itself OUT OF PROTOCOL, states that
        per-patient TTL mathematics is no longer meaningful at that scale, and
        defers to the hospital's MCI protocol." """
        if self.mode != "MCI":
            return None
        return {
            "out_of_protocol": True,
            "headline": "OUT OF PROTOCOL - mass-casualty scale",
            "statement": (
                "Per-patient TTL mathematics is no longer meaningful at this scale. "
                "This system is outside its competence. Hand over to the hospital's "
                "mass-casualty incident protocol."
            ),
            "occupancy_ratio": round(self.occupancy_ratio(), 3),
            "waiting": len(self.waiting()),
            "resus_bays": self.profile["capacity"]["resuscitation_bays"],
            "exit_requires_human": True,
        }

    # ------------------------------------------------------------------
    # L8 - human decision boundary
    # ------------------------------------------------------------------
    def assign_acuity(self, patient_ref: str, level: int, actor: str,
                      actor_role: ActorRole) -> Dict[str, Any]:
        """The nurse's decision.  Recorded SEPARATELY from the recommendation, with
        its own timestamp, so agreement and its latency are both measurable."""
        patient = self.patients[patient_ref]
        rec = self.latest[patient_ref]
        latency = self.now - rec.created_at_min
        decision = ClinicianDecision(actor=actor, actor_role=actor_role,
                                     assigned_level=int(level),
                                     timestamp_min=self.now,
                                     latency_from_display_min=latency)
        payload = build_audit_payload(
            {**rec.to_dict(), "_input_snapshot": rec.input_snapshot},
            patient_ref=patient_ref, actor=actor, actor_role=actor_role,
            timestamp_min=self.now, clinician_decision=decision,
            queue_state=self._queue_snapshot_for(patient_ref),
            mode=self.mode, occupancy_ratio=self.occupancy_ratio(),
            staffing=self.profile.get("staffing", {}),
            degradation_rung=self.degradation.rung, event_type="clinician_decision",
            clock_snapshot=self.clock.snapshot())
        self.audit.append(payload)
        patient.nurse_assigned_acuity = int(level)
        patient.nurse_assigned_at_min = self.now
        patient.nurse_assigned_by = f"{actor_role.value}:{actor}"
        return decision.to_dict()

    def override(self, patient_ref: str, to_level: int, actor: str,
                 actor_role: ActorRole, reason_category: Optional[str] = None,
                 free_text: Optional[str] = None) -> Dict[str, Any]:
        """ASYMMETRIC FRICTION.  Escalate: one interaction.  De-escalate: reason
        category required, plus free text when a red flag is firing.

        If the audit write fails, the override is REFUSED (Blueprint 13.3)."""
        patient = self.patients[patient_ref]
        rec = self.latest[patient_ref]
        from_level = patient.nurse_assigned_acuity or rec.acted_level
        to_level = int(to_level)

        direction = (OverrideDirection.ESCALATE if to_level < from_level
                     else OverrideDirection.DE_ESCALATE if to_level > from_level
                     else OverrideDirection.LATERAL)
        red_flag_active = any(r.floor_level <= 2 for r in rec.fired_rules)

        validate_override(direction, reason_category, free_text, red_flag_active)

        record = OverrideRecord(
            actor=actor, actor_role=actor_role, direction=direction,
            from_level=from_level, to_level=to_level,
            reason_category=reason_category, free_text=free_text,
            timestamp_min=self.now,
            elapsed_from_display_min=self.now - rec.created_at_min,
            ai_stated_reasons=[rec.dominant_reason] + list(rec.secondary_reasons),
            red_flag_active_at_override=red_flag_active,
        )
        payload = build_audit_payload(
            {**rec.to_dict(), "_input_snapshot": rec.input_snapshot},
            patient_ref=patient_ref, actor=actor, actor_role=actor_role,
            timestamp_min=self.now, override=record,
            queue_state=self._queue_snapshot_for(patient_ref),
            mode=self.mode, occupancy_ratio=self.occupancy_ratio(),
            staffing=self.profile.get("staffing", {}),
            degradation_rung=self.degradation.rung, event_type="override",
            clock_snapshot=self.clock.snapshot())
        try:
            self.audit.append(payload)
        except AuditStoreUnavailable as exc:
            raise OverrideRefused(
                "Override REFUSED: the audit store is unavailable, so this clinical "
                "change cannot be durably logged. No unlogged clinical change, ever. "
                f"({exc})"
            ) from exc

        patient.nurse_assigned_acuity = to_level
        patient.nurse_assigned_at_min = self.now
        patient.nurse_assigned_by = f"{actor_role.value}:{actor}"
        if direction == OverrideDirection.ESCALATE:
            self.red_flag_actioned[patient_ref] = False
        return record.to_dict()

    def acknowledge(self, patient_ref: str, actor: str, actor_role: ActorRole,
                    reassessed: bool = True) -> Dict[str, Any]:
        """A human looked.  This is what clears class R / class E."""
        patient = self.patients[patient_ref]
        if reassessed:
            patient.last_reassessed_at_min = self.now
            patient.last_reassessed_by = f"{actor_role.value}:{actor}"
        self.red_flag_actioned[patient_ref] = True
        self.audit.append({
            "event_type": "reassessment", "patient_ref": patient_ref,
            "timestamp_min": round(self.now, 4), "actor": actor,
            "actor_role": actor_role.value, "reassessed": reassessed,
        })
        return {"patient_ref": patient_ref, "at_min": self.now}

    def extend_clock(self, patient_ref: str, new_ttl_minutes: float, actor: str,
                     actor_role: ActorRole, reason: str) -> float:
        """The ONLY path by which a TTL may lengthen (invariant I1).  Audit first."""
        patient = self.patients[patient_ref]
        written = True
        try:
            self.audit.append({
                "event_type": "ttl_extension", "patient_ref": patient_ref,
                "timestamp_min": round(self.now, 4), "actor": actor,
                "actor_role": actor_role.value,
                "from_ttl": patient.current_ttl_minutes,
                "to_ttl": float(new_ttl_minutes), "reason": reason,
                "note": ("I1 exception: TTL lengthened by an explicit, attributed "
                         "human action recorded in the audit log."),
            })
        except AuditStoreUnavailable:
            written = False
        return human_lengthen_ttl(patient, new_ttl_minutes, self.now, actor,
                                  actor_role.value, reason, audit_written=written)

    # ------------------------------------------------------------------
    def tick(self, minutes: float = 1.0) -> None:
        """Advance the clock and recompute.  Blueprint 7.4: queue/TTL recompute
        cadence 10 s; the simulator drives it in coarser steps."""
        self.clock.advance(minutes)
        if self.clock.has_anomaly and self.degradation.clock_healthy:
            self.degradation.set_component("clock", False, self.now,
                                           self.clock.anomaly_summary() or "")
        self.evaluate_mode()
        for p in self.waiting():
            if self.degradation.spec.recommendations_produced:
                self.recompute(p)
        self.rebuild_queue()

    def check_drift(self) -> bool:
        """Blueprint 6.11: conformal set-width drift as the shift alarm."""
        widths = [r.prediction_set for r in self.latest.values() if r]
        if len(widths) < 20 or not self.baseline_set_widths:
            return False
        delta = mean_set_width(widths) - mean_set_width(self.baseline_set_widths)
        if drift_alarm(delta):
            self.degradation.set_drift_alarm(True, self.now, delta)
            return True
        return False

    def protocol_snapshot(self) -> Dict[str, Any]:
        rows = []
        for p in self.waiting():
            rec = self.latest.get(p.patient_ref)
            rows.append({
                "patient_ref": p.patient_ref, "chair": p.chair,
                "acted_level": rec.acted_level if rec else 3,
                "nurse_assigned_level": p.nurse_assigned_acuity,
            })
        return protocol_snapshot(rows, self.kb, self.profile, self.now)


def new_department(profile_id: str = "H-L", seed: int = 20260825,
                   kb: Optional[Knowledge] = None,
                   estimator: Optional[DeteriorationEstimator] = None,
                   calibrations: Optional[Dict[str, CalibrationSet]] = None
                   ) -> Department:
    kb = kb or load_knowledge()
    try:
        profile = kb.profile(profile_id)
    except Exception:
        profile = kb.conservative_default_profile()
    return Department(kb=kb, profile=profile, estimator=estimator,
                      calibrations=calibrations or {})
