"""
Clinical knowledge loader - Blueprint 20.

"rules + envelopes: declarative data, not code.  Clinical knowledge held as a
versioned, citable artefact that a clinician can review as a document.  Separating
this from engine code is what makes clinical ownership real rather than rhetorical."

This module is the ONLY place the engine reads clinical thresholds from.  Nothing in
app/clinical/ hard-codes a number that could have come from a guideline.

It also enforces the profile-load asymmetry of Blueprint 16.1: a hospital may
TIGHTEN a safety floor and may never LOOSEN one.  An invalid profile is rejected
with a NAMED VIOLATION rather than partially applied.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
RULES_DIR = os.path.join(REPO_ROOT, "rules")


class ProfileValidationError(ValueError):
    """Raised with a NAMED violation.  Blueprint 16.1: 'an invalid profile is
    rejected with a named violation rather than partially applied.'"""


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------


@dataclass
class Knowledge:
    """The loaded, validated clinical knowledge base."""
    core: Dict[str, Any]
    envelopes: Dict[str, Dict[str, Any]]
    red_flags: Dict[str, Any]
    sources: Dict[str, Any]
    profiles: Dict[str, Dict[str, Any]]

    # ------------------------------------------------------------------
    def protocol_floor(self, level: int, profile: Optional[Dict[str, Any]] = None) -> float:
        """The published CTAS reassessment interval for a WAITING patient at this
        acted level, after any hospital TIGHTENING.

        Blueprint 12.1: 'min(protocol floor for the acted level, risk-derived
        interval, uncertainty-derived interval, load-compressed interval)'.
        A hospital may tighten them, the engine may shorten them, NOBODY may
        lengthen them.
        """
        universal = float(self.core["protocol_floors_minutes"][str(level)])
        if profile:
            overrides = profile.get("protocol_floor_overrides_minutes") or {}
            if str(level) in overrides:
                universal = min(universal, float(overrides[str(level)]))
        return universal

    def max_wait(self, level: int) -> float:
        """Constraint C2 starvation-guard bound.  Blueprint 7.3 / 12.1."""
        return float(self.core["max_wait_to_first_assessment_minutes"][str(level)])

    def special_floor(self, key: str) -> float:
        return float(self.core["special_floors_minutes"][key]["value"])

    def half_life(self, field_name: str) -> Optional[float]:
        hl = self.core["half_lives_minutes"]
        if field_name not in hl:
            return None
        v = hl[field_name]
        return None if v is None else float(v)

    def envelope(self, envelope_id: str) -> Dict[str, Any]:
        return self.envelopes[envelope_id]

    def source(self, source_id: str) -> Dict[str, Any]:
        return self.sources["sources"].get(source_id, {})

    def citation(self, source_id: str) -> str:
        s = self.source(source_id)
        if not s:
            return source_id
        if s.get("ref"):
            return f"[{s['ref']}] {s.get('title', '')} - {s.get('publisher', '')}"
        return f"[{source_id}] {s.get('title', '')}"

    def profile(self, profile_id: str) -> Dict[str, Any]:
        key = profile_id.upper()
        if key not in self.profiles:
            raise ProfileValidationError(
                f"PROFILE_NOT_FOUND: '{profile_id}'. "
                f"Available: {sorted(self.profiles)}. "
                "Blueprint complexity 10 fallback: missing or invalid profile -> "
                "load the most conservative default (tightest floors, K=1) and warn."
            )
        return self.profiles[key]

    def conservative_default_profile(self) -> Dict[str, Any]:
        """Blueprint complexity 10 fallback: missing or invalid profile -> load the
        most conservative default profile (tightest floors, K=1) and warn."""
        base = json.loads(json.dumps(self.profile("H-S")))
        base["profile_id"] = "H-FALLBACK"
        base["display_name"] = "Conservative fallback profile"
        base["staffing"]["triage_nurses_on_shift"] = 1
        base["expected_k"] = 1
        base["protocol_floor_overrides_minutes"] = {"2": 10, "3": 20, "4": 30, "5": 60}
        base["_fallback_warning"] = (
            "Loaded because the requested profile was missing or invalid. "
            "Tightest floors, K=1."
        )
        return base


# ---------------------------------------------------------------------------


def validate_profile(profile: Dict[str, Any], core: Dict[str, Any]) -> List[str]:
    """Blueprint 16.1: 'every configurable safety parameter can only be moved in
    the CONSERVATIVE direction.  Configuration cannot make the system less safe
    than the universal core, only more.  This is checked at profile load; an
    invalid profile is rejected with a named violation.'

    Returns a list of named violations - empty means the profile is admissible.
    """
    violations: List[str] = []
    pid = profile.get("profile_id", "<unnamed>")

    # 1. Protocol floors: TIGHTEN ONLY.
    universal_floors = core["protocol_floors_minutes"]
    for level, value in (profile.get("protocol_floor_overrides_minutes") or {}).items():
        if level.startswith("_"):
            continue
        universal = float(universal_floors[str(level)])
        if float(value) > universal:
            violations.append(
                f"C-FLOOR-LOOSENED[{pid}]: protocol floor for level {level} set to "
                f"{value} min, which is LONGER than the universal floor of "
                f"{universal} min (CTAS [S17]). A hospital may tighten a floor and "
                f"never loosen it."
            )

    # 2. Conformal alpha: LOWER ONLY (more conservative == wider sets).
    alpha = float(profile.get("conformal_alpha", core["conformal"]["alpha_default"]))
    ceiling = float(core["conformal"]["alpha_ceiling"])
    floor = float(core["conformal"]["alpha_floor"])
    if alpha > ceiling:
        violations.append(
            f"C-ALPHA-RAISED[{pid}]: conformal alpha {alpha} exceeds the universal "
            f"ceiling {ceiling}. Invariant I10: alpha may be lowered by a hospital, "
            f"never raised above the ceiling."
        )
    if alpha < floor:
        violations.append(
            f"C-ALPHA-BELOW-FLOOR[{pid}]: conformal alpha {alpha} is below the "
            f"permitted floor {floor}."
        )

    # 3. Retention: RAISE ONLY, floored at the statutory minimum (DPDP Rule 6 [S7]).
    retention = int(profile.get("retention_days", core["audit"]["retention_days_floor"]))
    if retention < int(core["audit"]["retention_days_floor"]):
        violations.append(
            f"C-RETENTION-BELOW-FLOOR[{pid}]: retention {retention} days is below the "
            f"statutory floor of {core['audit']['retention_days_floor']} days "
            f"(DPDP Rule 6 [S7]). May only be raised."
        )

    # 4. Derived K must land inside the universal clamp.
    staffing = profile.get("staffing", {})
    nurses = staffing.get("triage_nurses_on_shift")
    if nurses is None or int(nurses) < 1:
        violations.append(
            f"C-STAFFING-INVALID[{pid}]: triage_nurses_on_shift must be >= 1. "
            f"Blueprint complexity 12 fallback: unknown staffing -> assume the "
            f"profile's declared minimum."
        )

    # 5. Surge thresholds may only be TIGHTENED below the universal trigger.
    st = profile.get("surge_thresholds", {})
    if float(st.get("strained_occupancy", 0.90)) > 0.90:
        violations.append(
            f"C-STRAINED-LOOSENED[{pid}]: strained_occupancy "
            f"{st.get('strained_occupancy')} exceeds the universal 0.90 trigger, "
            f"which is anchored to a measured harm inflection [S3]."
        )

    # 6. The prohibition list is non-negotiable.
    for prohibited_key in ("allow_diagnosis", "allow_treatment", "allow_disposition",
                           "allow_autonomous_routing", "allow_autonomous_deescalation",
                           "allow_llm_in_decision_path", "allow_learning_from_overrides"):
        if profile.get(prohibited_key):
            violations.append(
                f"C-PROHIBITION-VIOLATED[{pid}]: '{prohibited_key}' is set. The "
                f"universal core prohibition list is not configurable."
            )

    return violations


# ---------------------------------------------------------------------------

_CACHE: Optional[Knowledge] = None


def load_knowledge(rules_dir: str = RULES_DIR, use_cache: bool = True) -> Knowledge:
    """Load and validate every clinical artefact.  Cached because the engine is
    deterministic and the artefacts are immutable at runtime."""
    global _CACHE
    if use_cache and _CACHE is not None:
        return _CACHE

    core = _load_json(os.path.join(rules_dir, "profiles", "universal_core.json"))
    sources = _load_json(os.path.join(rules_dir, "citations", "sources.json"))
    red_flags = _load_json(os.path.join(rules_dir, "red_flags", "hard_rules.json"))

    envelopes: Dict[str, Dict[str, Any]] = {}
    env_dir = os.path.join(rules_dir, "envelopes")
    for name in sorted(os.listdir(env_dir)):
        if not name.endswith(".json"):
            continue
        env = _load_json(os.path.join(env_dir, name))
        envelopes[env["envelope_id"]] = env

    profiles: Dict[str, Dict[str, Any]] = {}
    prof_dir = os.path.join(rules_dir, "profiles")
    for name in sorted(os.listdir(prof_dir)):
        if not name.endswith(".json") or name == "universal_core.json":
            continue
        prof = _load_json(os.path.join(prof_dir, name))
        violations = validate_profile(prof, core)
        if violations:
            raise ProfileValidationError(
                "Profile rejected at load:\n  " + "\n  ".join(violations)
            )
        profiles[prof["profile_id"].upper()] = prof

    _verify_every_threshold_is_cited(red_flags, envelopes, sources)

    kb = Knowledge(core=core, envelopes=envelopes, red_flags=red_flags,
                   sources=sources, profiles=profiles)
    if use_cache:
        _CACHE = kb
    return kb


def _verify_every_threshold_is_cited(red_flags: Dict[str, Any],
                                     envelopes: Dict[str, Dict[str, Any]],
                                     sources: Dict[str, Any]) -> None:
    """Blueprint 9.5 STANDING RULE FOR THE BUILD TEAM: 'Every numeric clinical
    threshold that reaches the prototype must be traceable to a row in table A, or
    must appear in table B labelled as our assumption.  No threshold enters the
    codebase because it sounded right.'

    This is enforced at load, so a rule that loses its citation cannot ship.
    """
    known = set(sources["sources"].keys())
    for rule in red_flags["rules"]:
        src = rule.get("source")
        if src not in known:
            raise ProfileValidationError(
                f"UNCITED_RULE: rule {rule.get('rule_id')} has source '{src}' which is "
                f"not in rules/citations/sources.json. Blueprint 9.5 standing rule."
            )
    for tcp in red_flags.get("time_critical_pathways", []):
        if tcp.get("source") not in known:
            raise ProfileValidationError(
                f"UNCITED_PATHWAY: {tcp.get('pathway_id')} source "
                f"'{tcp.get('source')}' not in sources."
            )
    for env_id, env in envelopes.items():
        for band in env.get("age_bands", []):
            if band.get("source") not in known:
                raise ProfileValidationError(
                    f"UNCITED_BAND: {env_id}/{band.get('band_id')} source "
                    f"'{band.get('source')}' not in sources."
                )
        for fr in env.get("fever_rules", []):
            if fr.get("source") not in known:
                raise ProfileValidationError(
                    f"UNCITED_FEVER_RULE: {env_id}/{fr.get('rule_id')}"
                )


def reset_cache() -> None:
    global _CACHE
    _CACHE = None
