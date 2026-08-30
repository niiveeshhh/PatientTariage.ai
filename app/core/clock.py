"""
Clock integrity - Blueprint section 5 item 17 (SHOULD build, score 16).

"A product whose entire safety argument rests on elapsed time must have a
trustworthy time source. Skew, drift, or a paused simulation clock silently
invalidates every TTL."

Monotonic clock source with skew detection; a clock anomaly is a DEGRADATION EVENT
that shortens clocks (Blueprint 13.3).

All engine time is expressed in SIMULATED MINUTES since department epoch.  The core
never calls wall-clock time directly - that is what makes a demo reproducible from a
seed and what makes the property tests able to fast-forward.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class ClockAnomaly:
    at_min: float
    kind: str            # "non_monotonic" | "skew" | "large_jump"
    detail: str
    magnitude_min: float


@dataclass
class SimulationClock:
    """Monotonic simulated clock.

    Blueprint 8.1: arrival_timestamp uses a monotonic source, and a record without
    a trustworthy origin is REFUSED - "a clock-based product cannot function
    without a trustworthy origin".
    """
    now_min: float = 0.0
    max_forward_jump_min: float = 240.0     # ASM: anything larger is suspect
    anomalies: List[ClockAnomaly] = field(default_factory=list)
    _last_set: float = 0.0
    paused: bool = False

    # ------------------------------------------------------------------
    def advance(self, minutes: float) -> float:
        """Advance the clock.  Negative advances are rejected AND recorded as a
        non-monotonic anomaly; the clock does not move backwards."""
        if minutes < 0:
            self.anomalies.append(ClockAnomaly(
                at_min=self.now_min, kind="non_monotonic",
                detail=f"rejected backwards advance of {minutes:.2f} min",
                magnitude_min=abs(minutes),
            ))
            return self.now_min
        if minutes > self.max_forward_jump_min:
            self.anomalies.append(ClockAnomaly(
                at_min=self.now_min, kind="large_jump",
                detail=f"forward jump of {minutes:.2f} min exceeds {self.max_forward_jump_min:.0f}",
                magnitude_min=minutes,
            ))
        if not self.paused:
            self.now_min += minutes
            self._last_set = self.now_min
        return self.now_min

    def set_to(self, minutes: float) -> float:
        """Absolute set.  Used by replay and by scenario injection."""
        if minutes < self.now_min:
            self.anomalies.append(ClockAnomaly(
                at_min=self.now_min, kind="non_monotonic",
                detail=f"rejected set_to({minutes:.2f}) below current {self.now_min:.2f}",
                magnitude_min=self.now_min - minutes,
            ))
            return self.now_min
        self.now_min = minutes
        self._last_set = minutes
        return self.now_min

    def inject_skew(self, minutes: float, detail: str = "injected skew") -> None:
        """Chaos test hook (Blueprint 21.4 'clock anomaly').  Records the anomaly
        WITHOUT moving the clock backwards - the anomaly is the point, not the
        corruption."""
        self.anomalies.append(ClockAnomaly(
            at_min=self.now_min, kind="skew", detail=detail,
            magnitude_min=abs(minutes),
        ))

    # ------------------------------------------------------------------
    @property
    def has_anomaly(self) -> bool:
        return len(self.anomalies) > 0

    def anomaly_summary(self) -> Optional[str]:
        if not self.anomalies:
            return None
        latest = self.anomalies[-1]
        return f"{latest.kind}: {latest.detail}"

    def clear_anomalies(self) -> None:
        self.anomalies.clear()

    def snapshot(self) -> dict:
        return {
            "now_min": round(self.now_min, 4),
            "paused": self.paused,
            "anomaly_count": len(self.anomalies),
            "latest_anomaly": self.anomaly_summary(),
        }


def format_clock(minutes: Optional[float]) -> str:
    """MM:SS display for the card's progress ring.  Negative means overdue."""
    if minutes is None:
        return "--:--"
    sign = "-" if minutes < 0 else ""
    m = abs(minutes)
    whole = int(m)
    secs = int(round((m - whole) * 60))
    if secs == 60:
        whole += 1
        secs = 0
    return f"{sign}{whole:02d}:{secs:02d}"
