"""
The degradation ladder - Blueprint 13.2.

"Every rung is announced by a PERSISTENT BANNER, logged as a mode-change event, and
produces clocks AT LEAST AS SHORT as the rung above it.  THE SYSTEM DEGRADES UPWARD
INTO VIGILANCE, NEVER DOWNWARD INTO SILENCE."

    L0 FULL        all components healthy
    L1 NO-MODEL    model unavailable, or drift alarm sustained
    L2 NO-HISTORY  EHR or record feed unavailable
    L3 NO-ENGINE   engine or compute failure -> static protocol mode
    L4 DARK        total failure, including audit store

INVARIANT I5: degradation never lengthens a TTL.  Enforced in app/queue/ttl.py,
verified by the chaos tests in tests/degradation/.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

RUNGS = ["L0_FULL", "L1_NO_MODEL", "L2_NO_HISTORY", "L3_NO_ENGINE", "L4_DARK"]

RUNG_INDEX = {r: i for i, r in enumerate(RUNGS)}


@dataclass
class RungSpec:
    rung: str
    banner_severity: str          # "none" | "amber" | "red" | "red_fullscreen"
    banner_text: str
    lost: str
    retained: str
    model_available: bool
    history_available: bool
    engine_available: bool
    audit_available: bool
    recommendations_produced: bool


LADDER: Dict[str, RungSpec] = {
    "L0_FULL": RungSpec(
        rung="L0_FULL", banner_severity="none", banner_text="",
        lost="Nothing.", retained="Normal operation.",
        model_available=True, history_available=True, engine_available=True,
        audit_available=True, recommendations_produced=True,
    ),
    "L1_NO_MODEL": RungSpec(
        rung="L1_NO_MODEL", banner_severity="amber",
        banner_text="Model offline - running on rules and protocol intervals. Clocks shortened.",
        lost="Trend-based shortening and set-width nuance.",
        retained="Everything the competition requires: rules, published scores, "
                 "conservative sets, TTLs from protocol floors and uncertainty.",
        model_available=False, history_available=True, engine_available=True,
        audit_available=True, recommendations_produced=True,
    ),
    "L2_NO_HISTORY": RungSpec(
        rung="L2_NO_HISTORY", banner_severity="amber",
        banner_text="Patient records unavailable - all patients treated as first-time. Clocks tightened.",
        lost="Baselines and comorbidity context.",
        retained="Full triage function. Zero-history is a first-class supported path.",
        model_available=True, history_available=False, engine_available=True,
        audit_available=True, recommendations_produced=True,
    ),
    "L3_NO_ENGINE": RungSpec(
        rung="L3_NO_ENGINE", banner_severity="red",
        banner_text="Recommendations unavailable - protocol intervals only.",
        lost="All recommendation logic.",
        retained="The clock discipline, which is the product's core value. Every "
                 "patient falls to the published reassessment interval for their "
                 "last human-assigned acuity [S17].",
        model_available=False, history_available=False, engine_available=False,
        audit_available=True, recommendations_produced=False,
    ),
    "L4_DARK": RungSpec(
        rung="L4_DARK", banner_severity="red_fullscreen",
        banner_text="SYSTEM UNAVAILABLE - exportable protocol snapshot only. "
                    "No clinical decision may be recorded while the audit store is down.",
        lost="Everything except the queue and the published intervals.",
        retained="Today's standard of care: a paper list and fixed intervals. "
                 "Degrades to the standard of care, never below it.",
        model_available=False, history_available=False, engine_available=False,
        audit_available=False, recommendations_produced=False,
    ),
}


@dataclass
class DegradationState:
    """Which rung we are on, and why.  Rung is derived from component health, so a
    component recovering is visible, but the CLOCKS DO NOT LENGTHEN when it does
    (constraint C1) - a recovered component buys a fresh look, not a longer leash.
    """
    model_healthy: bool = True
    history_healthy: bool = True
    engine_healthy: bool = True
    audit_healthy: bool = True
    network_healthy: bool = True
    clock_healthy: bool = True
    drift_alarm: bool = False
    events: List[Dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------
    @property
    def rung(self) -> str:
        if not self.audit_healthy:
            return "L4_DARK"
        if not self.engine_healthy:
            return "L3_NO_ENGINE"
        if not self.history_healthy:
            return "L2_NO_HISTORY"
        if not self.model_healthy or self.drift_alarm:
            return "L1_NO_MODEL"
        return "L0_FULL"

    @property
    def spec(self) -> RungSpec:
        return LADDER[self.rung]

    @property
    def banners(self) -> List[Dict[str, str]]:
        """Every degraded state is a BANNER, never a blank screen or a stale one
        (Blueprint complexity 19)."""
        out: List[Dict[str, str]] = []
        spec = self.spec
        if spec.banner_severity != "none":
            out.append({"severity": spec.banner_severity, "text": spec.banner_text,
                        "rung": self.rung})
        if not self.network_healthy:
            out.append({
                "severity": "amber",
                "text": "Network unavailable - engine running locally. "
                        "No cloud dependency in the decision path.",
                "rung": self.rung,
            })
        if not self.clock_healthy:
            out.append({
                "severity": "amber",
                "text": "Clock anomaly detected - clocks shortened as a precaution.",
                "rung": self.rung,
            })
        if self.drift_alarm:
            out.append({
                "severity": "amber",
                "text": "Distribution-shift alarm: conformal set width has risen. "
                        "Falling back toward the deterministic floor; flagged for "
                        "recalibration.",
                "rung": self.rung,
            })
        return out

    # ------------------------------------------------------------------
    def set_component(self, component: str, healthy: bool, now_min: float,
                      detail: str = "") -> str:
        before = self.rung
        setattr(self, f"{component}_healthy", healthy)
        after = self.rung
        if before != after:
            self.events.append({
                "at_min": round(now_min, 4),
                "from_rung": before,
                "to_rung": after,
                "component": component,
                "healthy": healthy,
                "detail": detail,
                "banner": LADDER[after].banner_text,
            })
        return after

    def set_drift_alarm(self, active: bool, now_min: float, delta: float = 0.0) -> str:
        before = self.rung
        self.drift_alarm = active
        after = self.rung
        if before != after:
            self.events.append({
                "at_min": round(now_min, 4),
                "from_rung": before, "to_rung": after,
                "component": "conformal_calibration",
                "healthy": not active,
                "detail": f"mean set width delta {delta:+.3f}",
                "banner": LADDER[after].banner_text,
            })
        return after

    def to_dict(self) -> Dict[str, Any]:
        spec = self.spec
        return {
            "rung": self.rung,
            "banner_severity": spec.banner_severity,
            "banners": self.banners,
            "lost": spec.lost,
            "retained": spec.retained,
            "components": {
                "model": self.model_healthy,
                "history": self.history_healthy,
                "engine": self.engine_healthy,
                "audit": self.audit_healthy,
                "network": self.network_healthy,
                "clock": self.clock_healthy,
            },
            "drift_alarm": self.drift_alarm,
            "events": list(self.events),
            "recommendations_produced": spec.recommendations_produced,
        }


def rung_is_at_least_as_degraded(a: str, b: str) -> bool:
    return RUNG_INDEX.get(a, 0) >= RUNG_INDEX.get(b, 0)


def protocol_snapshot(entries: List[Dict[str, Any]], kb, profile: Dict[str, Any],
                      now_min: float) -> Dict[str, Any]:
    """Rung L4 - the EXPORTABLE PROTOCOL SNAPSHOT.

    Blueprint 13.2: "the current queue with published intervals, printable or
    displayable, plus a persistent unavailability notice.  THIS IS TODAY'S STANDARD
    OF CARE: a paper list and fixed intervals."

    Blueprint 2.3 DISCARD: "Keep the promise, mock the mechanism.  A print driver is
    demo risk with zero judge value; an on-screen exportable protocol snapshot
    proves the same point."  So this returns data, and the UI renders it.
    """
    rows = []
    for e in sorted(entries, key=lambda r: (r.get("acted_level", 5),
                                            r.get("patient_ref", ""))):
        level = e.get("nurse_assigned_level") or e.get("acted_level", 3)
        interval = kb.protocol_floor(level, profile)
        rows.append({
            "patient_ref": e.get("patient_ref"),
            "chair": e.get("chair"),
            "level": level,
            "level_source": ("nurse-assigned" if e.get("nurse_assigned_level")
                             else "last recommended"),
            "published_reassessment_interval_min": interval,
            "citation": "CTAS mandatory reassessment intervals [S17]",
        })
    return {
        "generated_at_min": round(now_min, 4),
        "notice": (
            "SYSTEM UNAVAILABLE. This is the current queue with published "
            "reassessment intervals - today's standard of care. Recommendations, "
            "clocks and audit are not running. Do not treat any value here as live."
        ),
        "profile": profile.get("profile_id"),
        "rows": rows,
        "source": "Canadian Triage and Acuity Scale [S17]",
    }
