"""
L1 - Envelope selection.  Blueprint 9.2 and 9.5.

"Without it, a single adult calibration is applied to a 3-year-old - the exact
silent safety risk Round 2 names.  Unknown age resolves to the UNION OF ADJACENT
ENVELOPES' ESCALATIONS, never to adult."

AGE SELECTS THE RULE SET, NOT A COEFFICIENT.

This is the single highest-value change from Round 1 (Blueprint 2.2 weakness 1):
the Royal College of Physicians states NEWS2 "should not be used in children
(ie aged <16 years)" or "women who are pregnant" [S1].
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.core.knowledge import Knowledge
from app.core.models import AgeSource, Patient, PregnancyStatus

# ASM (Blueprint 9.5 table B): estimated age within +/-2 years of a band boundary
# triggers dual-envelope evaluation, acting on the more acute result.
BOUNDARY_TOLERANCE_DAYS = 2 * 365

ADULT_MIN_DAYS = 16 * 365          # NEWS2 becomes valid at 16 [S1]
GERIATRIC_MIN_DAYS = 65 * 365
ADOLESCENT_DUAL_MIN_DAYS = 16 * 365
ADOLESCENT_DUAL_MAX_DAYS = 18 * 365   # ESI's paediatric band runs to 18 [S5]

REPRODUCTIVE_AGE_MIN_DAYS = 12 * 365
REPRODUCTIVE_AGE_MAX_DAYS = 55 * 365


@dataclass
class EnvelopeSelection:
    """The active rule set, threshold table, model envelope and calibration set."""
    primary_id: str
    envelopes: List[str] = field(default_factory=list)   # all to evaluate; act on worst
    age_band: Optional[Dict[str, Any]] = None
    age_bands_considered: List[Dict[str, Any]] = field(default_factory=list)
    news2_permitted: bool = True
    news2_suppression_reason: Optional[str] = None
    aggregate_score_suppressed: bool = False
    forced_uncertainty_class: Optional[str] = None
    ttl_floor_minutes: Optional[float] = None
    dual_scored: bool = False
    dual_reason: Optional[str] = None
    obstetric_pathway: bool = False
    version: str = ""
    note: str = ""
    applicability_exclusions: List[str] = field(default_factory=list)
    critical_1_fields: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "primary_id": self.primary_id,
            "envelopes": list(self.envelopes),
            "age_band": self.age_band.get("band_id") if self.age_band else None,
            "age_bands_considered": [b.get("band_id") for b in self.age_bands_considered],
            "news2_permitted": self.news2_permitted,
            "news2_suppression_reason": self.news2_suppression_reason,
            "aggregate_score_suppressed": self.aggregate_score_suppressed,
            "forced_uncertainty_class": self.forced_uncertainty_class,
            "ttl_floor_minutes": self.ttl_floor_minutes,
            "dual_scored": self.dual_scored,
            "dual_reason": self.dual_reason,
            "obstetric_pathway": self.obstetric_pathway,
            "version": self.version,
            "note": self.note,
            "applicability_exclusions": sorted(self.applicability_exclusions),
            "critical_1_fields": sorted(self.critical_1_fields),
        }

    @property
    def display_name(self) -> str:
        """Blueprint complexity 3: 'The card names the envelope in use:
        "paediatric 1-3 y envelope."'"""
        if self.age_band:
            return f"{self.primary_id} {self.age_band.get('label', '')} envelope".strip()
        return f"{self.primary_id} envelope"


def paediatric_band_for(age_days: int, kb: Knowledge) -> Optional[Dict[str, Any]]:
    env = kb.envelope("paediatric")
    for band in env["age_bands"]:
        if band["min_days"] <= age_days <= band["max_days"]:
            return band
    return None


def adjacent_paediatric_bands(age_days: int, kb: Knowledge,
                              tolerance_days: int) -> List[Dict[str, Any]]:
    """Blueprint 9.5 table B: if age is flagged estimated and lies within +/-2 years
    of a band boundary, BOTH adjacent envelopes are evaluated and the more acute
    result is acted on."""
    env = kb.envelope("paediatric")
    out: List[Dict[str, Any]] = []
    for band in env["age_bands"]:
        lo, hi = band["min_days"], band["max_days"]
        if (age_days - tolerance_days) <= hi and (age_days + tolerance_days) >= lo:
            out.append(band)
    return out


def _is_possibly_pregnant(patient: Patient) -> bool:
    """Blueprint 8.3: 'unknown' in a patient of reproductive age is treated as
    POSSIBLE, which suppresses the adult aggregate score."""
    if patient.pregnancy_status == PregnancyStatus.YES:
        return True
    if patient.pregnancy_status != PregnancyStatus.UNKNOWN:
        return False
    if patient.sex not in ("F", "f", "female"):
        return False
    if patient.age_days is None:
        # Unknown age AND unknown pregnancy in a female: the conservative reading.
        return True
    return REPRODUCTIVE_AGE_MIN_DAYS <= patient.age_days <= REPRODUCTIVE_AGE_MAX_DAYS


def select_envelope(patient: Patient, kb: Knowledge) -> EnvelopeSelection:
    """Deterministic from age and pregnancy status.  Blueprint 9.3 marks this
    AUTONOMOUS precisely because 'being wrong here would be catastrophic, which is
    exactly why it is a rule and not a model.'
    """
    # ------------------------------------------------------------------
    # 1. Unknown age is a FIRST-CLASS STATE, never a default to adult.
    # ------------------------------------------------------------------
    if not patient.age_known:
        env = kb.envelope("unknown_age")
        beh = env["behaviour"]
        sel = EnvelopeSelection(
            primary_id="unknown_age",
            envelopes=list(beh["union_envelopes"]),
            news2_permitted=False,
            news2_suppression_reason=env["news2_suppression_reason"],
            aggregate_score_suppressed=True,
            forced_uncertainty_class=beh["forced_uncertainty_class"],
            ttl_floor_minutes=float(beh["ttl_floor_minutes"]),
            version=env["version"],
            note=beh["card_copy"],
            critical_1_fields=list(env["critical_1_fields"]),
        )
        sel.applicability_exclusions.append("age_unknown")
        # Every paediatric band is in play, so the tightest thresholds apply.
        sel.age_bands_considered = list(kb.envelope("paediatric")["age_bands"])
        return sel

    age_days = int(patient.age_days)

    # ------------------------------------------------------------------
    # 2. Pregnancy overrides the age envelope for SCORING at any age.
    #    NEWS2 must not be used in pregnancy [S1].
    # ------------------------------------------------------------------
    if _is_possibly_pregnant(patient):
        env = kb.envelope("pregnancy")
        beh = env["suppression_behaviour"]
        sel = EnvelopeSelection(
            primary_id="pregnancy",
            envelopes=["pregnancy"],
            news2_permitted=False,
            news2_suppression_reason=env["news2_suppression_reason"],
            aggregate_score_suppressed=True,
            ttl_floor_minutes=float(beh["ttl_floor_minutes"]),
            obstetric_pathway=True,
            forced_uncertainty_class=beh.get("minimum_uncertainty_class"),
            version=env["version"],
            note=beh["card_copy"],
            critical_1_fields=list(env["critical_1_fields"]),
        )
        sel.applicability_exclusions.append(
            "pregnancy" if patient.pregnancy_status == PregnancyStatus.YES
            else "pregnancy_possible_unknown"
        )
        if age_days < ADULT_MIN_DAYS:
            # A pregnant adolescent: the paediatric hard rules still apply on top.
            sel.envelopes.append("paediatric")
            sel.age_band = paediatric_band_for(age_days, kb)
            sel.dual_scored = True
            sel.dual_reason = (
                "pregnant patient under 16: obstetric suppression of the aggregate "
                "score PLUS the paediatric hard-rule set; act on the worse"
            )
        return sel

    # ------------------------------------------------------------------
    # 3. Paediatric (< 16 for scoring).
    # ------------------------------------------------------------------
    if age_days < ADULT_MIN_DAYS:
        env = kb.envelope("paediatric")
        band = paediatric_band_for(age_days, kb)
        sel = EnvelopeSelection(
            primary_id="paediatric",
            envelopes=["paediatric"],
            age_band=band,
            age_bands_considered=[band] if band else [],
            news2_permitted=False,
            news2_suppression_reason=env["news2_suppression_reason"],
            version=env["version"],
            critical_1_fields=list(env["critical_1_fields"]),
        )
        if patient.age_estimated or patient.age_source == AgeSource.ESTIMATED:
            bands = adjacent_paediatric_bands(age_days, kb, BOUNDARY_TOLERANCE_DAYS)
            if len(bands) > 1:
                sel.age_bands_considered = bands
                sel.dual_scored = True
                sel.dual_reason = (
                    "age is estimated and within 2 years of a band boundary: both "
                    "adjacent bands evaluated, acting on the more acute result [ASM]"
                )
                sel.applicability_exclusions.append("estimated_age_near_boundary")
        sel.note = f"paediatric {band['label']} envelope" if band else "paediatric envelope"
        return sel

    # ------------------------------------------------------------------
    # 4. Adolescent 16-18: DUAL-SCORED under both envelopes, act on the worse.
    #    The two authorities disagree about where childhood ends, so we take the
    #    worse answer rather than picking a side (Blueprint 9.5 table B).
    # ------------------------------------------------------------------
    if ADOLESCENT_DUAL_MIN_DAYS <= age_days <= ADOLESCENT_DUAL_MAX_DAYS:
        adult_env = kb.envelope("adult")
        band = paediatric_band_for(age_days, kb)
        sel = EnvelopeSelection(
            primary_id="adult",
            envelopes=["adult", "paediatric"],
            age_band=band,
            age_bands_considered=[band] if band else [],
            news2_permitted=True,
            version=adult_env["version"],
            dual_scored=True,
            dual_reason=(
                "age 16-18: NEWS2 becomes valid at 16 [S1] while ESI's paediatric "
                "band runs to 18 [S5]. Scored under BOTH envelopes; the more acute "
                "result is acted on [ASM]"
            ),
            critical_1_fields=list(adult_env["critical_1_fields"]),
            note="adolescent dual envelope (adult + paediatric 12-18 y)",
        )
        return sel

    # ------------------------------------------------------------------
    # 5. Geriatric (>= 65).
    # ------------------------------------------------------------------
    if age_days >= GERIATRIC_MIN_DAYS:
        env = kb.envelope("geriatric")
        sel = EnvelopeSelection(
            primary_id="geriatric",
            envelopes=["geriatric"],
            news2_permitted=True,
            version=env["version"],
            critical_1_fields=list(env["critical_1_fields"]),
            note="geriatric envelope (NEWS2 + atypical-presentation modifiers)",
        )
        if patient.spinal_cord_injury:
            sel.applicability_exclusions.append("spinal_cord_injury_caveat")
        return sel

    # ------------------------------------------------------------------
    # 6. Adult 16-64 non-pregnant.
    # ------------------------------------------------------------------
    env = kb.envelope("adult")
    sel = EnvelopeSelection(
        primary_id="adult",
        envelopes=["adult"],
        news2_permitted=True,
        version=env["version"],
        critical_1_fields=list(env["critical_1_fields"]),
        note="adult envelope (NEWS2)",
    )
    if patient.spinal_cord_injury:
        # Blueprint 9.5 table B: RCP says NEWS2 "may be unreliable" here; the honest
        # translation is MORE UNCERTAINTY, not a different number.  This widens the
        # conformal set (component U5) rather than altering thresholds.
        sel.applicability_exclusions.append("spinal_cord_injury_caveat")
    return sel


def critical_1_fields_for(selection: EnvelopeSelection, patient: Patient,
                          kb: Knowledge) -> List[str]:
    """Blueprint 10.1 U1: "How much of the decision-critical picture exists at all,
    FOR THIS PATIENT'S ENVELOPE - not for the schema in general. A paediatric case
    missing capillary refill is less complete than an adult case missing it."

    Two mechanisms:

    1. CONDITIONAL fields.  Blueprint 8.3 marks blood glucose and GCS as
       "Critical-1 WHEN INDICATED" - glucose conditional on altered consciousness,
       diabetes, or paediatric unwellness; GCS conditional on ACVPU < A or head
       injury.  A field that is critical for THIS patient and absent must force
       THIN even if the aggregate completeness score looks healthy.

    2. NOT-APPLICABLE fields.  Absence of BP in a small child is "not applicable"
       and carries NO completeness penalty - the only reason that does
       (Blueprint 8.7), because capillary refill substitutes.
    """
    fields = set(selection.critical_1_fields)

    # --- 1. conditional critical-1 -------------------------------------------
    acvpu = patient.value("consciousness_acvpu")
    altered = acvpu is not None and acvpu != "A"
    diabetic = any("diabet" in c.lower() for c in patient.known_conditions)

    paediatric = (selection.primary_id == "paediatric"
                  or "paediatric" in selection.envelopes)
    temp = patient.value("temperature_c")
    behaviour = patient.value("behavioural_state")
    paediatric_unwellness = paediatric and (
        altered
        or (temp is not None and (temp >= 38.0 or temp < 36.0))
        or behaviour in ("inconsolable", "lethargic", "unresponsive")
        or bool(patient.value("carer_concern"))
    )

    if altered or diabetic or paediatric_unwellness:
        # "Hypoglycaemia is a fully reversible cause of altered consciousness and a
        # hard red flag - one of the cheapest, highest-value rules in the set."
        fields.add("blood_glucose_mgdl")

    # Blueprint 8.3: GCS is "Conditional on ACVPU < A OR HEAD INJURY".  A sprained
    # ankle is an injury and is not a head injury - demanding a GCS for it would
    # make the control case THIN and prove the system is too noisy (scenario S-01).
    head_injury = bool(patient.value("major_mechanism")) or         bool(patient.value("head_injury"))
    if altered or head_injury:
        fields.add("gcs")

    # --- 2. envelope not-applicable ------------------------------------------
    if paediatric:
        env = kb.envelope("paediatric")
        for fname, spec in (env.get("not_applicable_fields") or {}).items():
            below = spec.get("applies_when_age_days_below")
            if below is not None and patient.age_days is not None                     and patient.age_days < below:
                fields.discard(fname)

    if not paediatric:
        fields.discard("work_of_breathing")
        fields.discard("capillary_refill_seconds")

    return sorted(fields)
