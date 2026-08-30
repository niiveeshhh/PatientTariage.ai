"""
L3 - Physiological risk read.  Blueprint 9.2.

"Quantifies HOW DERANGED, which rules cannot.  Produces the gradient the queue ranks
on.  SUPPRESSED ENTIRELY where the score is invalid (pregnancy), which is itself an
output."

Blueprint 9.1: NEWS2 is adopted "as the physiological read that MODULATES ATTENTION
- never as a triage category".  A stroke patient can score zero; that is why the
complaint pathways in L2 fire independently of this layer.

Blueprint 9.5 geriatric implementation decision: atypical-presentation modifiers are
implemented as REMOVALS OF REASSURANCE, never as numeric additions to the score,
because [S6] establishes the DIRECTION of these effects and not a magnitude.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.core.knowledge import Knowledge
from app.core.models import Patient
from app.clinical.layer1_envelope import EnvelopeSelection


@dataclass
class RiskRead:
    score: float = 0.0                     # normalised 0..1 derangement gradient
    suppressed: bool = False
    suppression_reason: Optional[str] = None
    news2_total: Optional[int] = None
    news2_max_single: Optional[int] = None
    news2_band: Optional[str] = None
    news2_components: Dict[str, int] = field(default_factory=dict)
    paediatric_flags: List[str] = field(default_factory=list)
    geriatric_modifiers_applied: List[str] = field(default_factory=list)
    reassurance_removed: List[str] = field(default_factory=list)
    components_missing: List[str] = field(default_factory=list)
    envelope_id: str = ""
    # Blueprint 9.5 table B: geriatric modifiers are REMOVALS OF REASSURANCE, not
    # numeric additions.  Mechanically a removal of reassurance means the benign
    # levels are no longer well supported, so the CONFORMAL SET WIDENS toward the
    # acute side.  This counter is the only thing the modifiers emit into the
    # decision path - they never touch the gradient.
    set_widening: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "suppressed": self.suppressed,
            "suppression_reason": self.suppression_reason,
            "news2_total": self.news2_total,
            "news2_max_single": self.news2_max_single,
            "news2_band": self.news2_band,
            "news2_components": dict(sorted(self.news2_components.items())),
            "paediatric_flags": sorted(self.paediatric_flags),
            "geriatric_modifiers_applied": sorted(self.geriatric_modifiers_applied),
            "reassurance_removed": sorted(self.reassurance_removed),
            "components_missing": sorted(self.components_missing),
            "envelope_id": self.envelope_id,
            "set_widening": self.set_widening,
        }

    def as_news2_facts(self) -> Optional[Dict[str, Any]]:
        if self.news2_total is None:
            return None
        return {"total": self.news2_total, "max_single": self.news2_max_single}


# ---------------------------------------------------------------------------
# NEWS2  [S1]
# ---------------------------------------------------------------------------

def _score_band(value: Any, bands: List[Dict[str, Any]],
                on_oxygen: Optional[bool] = None) -> Optional[int]:
    """Look a value up in a published band table.  Returns None when the value is
    absent - NEVER a zero.  Blueprint 6.1: never impute a missing decision-critical
    value to a normal one; a missing field must not score as 'normal'."""
    if value is None:
        return None
    for band in bands:
        if "value" in band:
            if band["value"] == value:
                return int(band["score"])
            continue
        lo = band.get("min")
        hi = band.get("max")
        if lo is not None and value < lo:
            continue
        if hi is not None and value > hi:
            continue
        if band.get("on_air") and on_oxygen:
            continue
        if band.get("on_oxygen") and not on_oxygen:
            continue
        return int(band["score"])
    return None


def compute_news2(patient: Patient, kb: Knowledge) -> Dict[str, Any]:
    """NEWS2 aggregate.  Reproduced against the published table [S1], including the
    single-parameter-score-of-3 trigger and the supplemental-oxygen weighting.

    Missing parameters are reported in components_missing and contribute NOTHING to
    the total - the aggregate is deliberately NOT completed by imputation.  U1 then
    carries the incompleteness into the uncertainty class, where it shortens the
    clock rather than silently lowering the score.
    """
    adult = kb.envelope("adult")
    params = adult["news2_scoring"]["parameters"]
    on_oxygen = bool(patient.value("on_oxygen"))
    scale2 = bool(patient.value("spo2_scale2"))

    components: Dict[str, int] = {}
    missing: List[str] = []

    def take(name: str, value: Any, table: List[Dict[str, Any]]) -> None:
        s = _score_band(value, table, on_oxygen=on_oxygen)
        if s is None:
            missing.append(name)
        else:
            components[name] = s

    take("respiratory_rate", patient.value("respiratory_rate"), params["respiratory_rate"])
    take("spo2", patient.value("spo2"),
         params["spo2_scale2"] if scale2 else params["spo2_scale1"])
    take("supplemental_oxygen", on_oxygen, params["supplemental_oxygen"])
    take("systolic_bp", patient.value("systolic_bp"), params["systolic_bp"])
    take("heart_rate", patient.value("heart_rate"), params["heart_rate"])
    take("consciousness_acvpu", patient.value("consciousness_acvpu"),
         params["consciousness_acvpu"])
    take("temperature", patient.value("temperature_c"), params["temperature"])

    total = sum(components.values())
    max_single = max(components.values()) if components else 0

    if max_single >= 3 and total < 5:
        band = "low_medium_single_param"
    elif total >= 7:
        band = "high"
    elif total >= 5:
        band = "medium"
    else:
        band = "low"

    return {
        "total": total,
        "max_single": max_single,
        "band": band,
        "components": components,
        "missing": missing,
        "scale": 2 if scale2 else 1,
    }


NEWS2_MAX_PLAUSIBLE = 20.0     # ASM: normalisation ceiling for the 0..1 gradient


# ---------------------------------------------------------------------------
# Paediatric read  [S5] + [S11]
# ---------------------------------------------------------------------------

def compute_paediatric_read(patient: Patient, selection: EnvelopeSelection,
                            kb: Knowledge) -> Dict[str, Any]:
    """Age-banded ESI v5 thresholds plus the national-PEWS scored parameter set.

    This is a GRADIENT, not a triage category: the hard thresholds themselves fire
    in L2 as rules RF-P01..RF-P10.  What this produces is 'how far past the band
    threshold', which is what the queue ranks on.
    """
    flags: List[str] = []
    points = 0.0
    missing: List[str] = []

    bands = selection.age_bands_considered or (
        [selection.age_band] if selection.age_band else [])
    band = None
    if bands:
        # For a dual-scored estimated age, use the TIGHTEST band - the more acute
        # reading, consistent with acting on the worse result.
        band = min((b for b in bands if b), key=lambda b: b["hr_high"], default=None)

    hr = patient.value("heart_rate")
    rr = patient.value("respiratory_rate")
    spo2 = patient.value("spo2")
    temp = patient.value("temperature_c")

    if band:
        if hr is None:
            missing.append("heart_rate")
        elif hr > band["hr_high"]:
            over = (hr - band["hr_high"]) / float(band["hr_high"])
            points += 3.0 + min(3.0, over * 10)
            flags.append(f"HR {hr} above {band['label']} threshold {band['hr_high']}")
        if rr is None:
            missing.append("respiratory_rate")
        elif rr > band["rr_high"]:
            over = (rr - band["rr_high"]) / float(band["rr_high"])
            points += 3.0 + min(3.0, over * 10)
            flags.append(f"RR {rr} above {band['label']} threshold {band['rr_high']}")

    if spo2 is None:
        missing.append("spo2")
    elif spo2 < 92:
        points += 3.0 + min(3.0, (92 - spo2) * 0.5)
        flags.append(f"SpO2 {spo2}% below 92%")
    elif spo2 < 95:
        points += 1.0

    if temp is None:
        missing.append("temperature_c")
    else:
        age_days = patient.age_days or 0
        if age_days <= 27 and temp > 38.0:
            points += 4.0
            flags.append("fever under 28 days old")
        elif age_days <= 91 and temp > 38.0:
            points += 3.0
            flags.append("fever, infant under 3 months")
        elif temp > 39.0 or temp < 36.0:
            points += 2.0
            flags.append("temperature outside child range")

    wob = patient.value("work_of_breathing")
    if wob is None:
        missing.append("work_of_breathing")
    elif wob == "severe":
        points += 4.0
        flags.append("severe work of breathing")
    elif wob == "moderate":
        points += 2.5
        flags.append("moderate work of breathing")
    elif wob == "mild":
        points += 1.0

    crt = patient.value("capillary_refill_seconds")
    if crt is None:
        missing.append("capillary_refill_seconds")
    elif crt >= 3.0:
        points += 3.0
        flags.append(f"capillary refill {crt} s")
    elif crt > 2.0:
        points += 1.0

    perf = patient.value("skin_perfusion")
    if perf in ("mottled", "cyanosed"):
        points += 3.0
        flags.append(f"perfusion {perf}")
    elif perf == "pale":
        points += 1.0

    beh = patient.value("behavioural_state")
    if beh in ("inconsolable", "lethargic", "unresponsive"):
        points += 2.5
        flags.append(f"behaviour {beh}")

    acvpu = patient.value("consciousness_acvpu")
    if acvpu is None:
        missing.append("consciousness_acvpu")
    elif acvpu in ("C", "V"):
        points += 3.0
    elif acvpu in ("P", "U"):
        points += 5.0

    if patient.value("carer_concern"):
        # Escalate-only.  Never subtracted, never used to reassure.
        points += 2.0
        flags.append("carer concerned")

    return {"points": points, "flags": flags, "missing": missing,
            "band": band["band_id"] if band else None}


PAEDIATRIC_MAX_PLAUSIBLE = 22.0   # ASM: normalisation ceiling


# ---------------------------------------------------------------------------
# The layer
# ---------------------------------------------------------------------------

def run_layer3(patient: Patient, selection: EnvelopeSelection,
               kb: Knowledge) -> RiskRead:
    """Select the envelope-appropriate read.  This function never returns a triage
    category - only a 0..1 derangement gradient plus its components."""
    read = RiskRead(envelope_id=selection.primary_id)

    # ------------------------------------------------------------------
    # Pregnancy: the aggregate score is SUPPRESSED, not adjusted.
    # "Refusing to score is the safest possible output here." (Blueprint 5 item 7)
    # ------------------------------------------------------------------
    if selection.aggregate_score_suppressed and selection.primary_id == "pregnancy":
        read.suppressed = True
        read.suppression_reason = (
            "NEWS2 is not valid in pregnancy - the physiological response to acute "
            "illness is modified [S1]. No validated obstetric early-warning score is "
            "substituted, because inventing one would be a fabrication."
        )
        # The gradient falls back to the hard-rule floor alone; L2 still runs in full.
        read.score = 0.0
        return read

    # ------------------------------------------------------------------
    # Unknown age: union of adjacent envelopes.  Take the WORST reading.
    # ------------------------------------------------------------------
    if selection.primary_id == "unknown_age":
        read.suppressed = True
        read.suppression_reason = (
            "Age unknown - no calibrated envelope applies. The union of adjacent "
            "envelopes' escalations is used and uncertainty is BLIND [ASM]."
        )
        paed = compute_paediatric_read(patient, selection, kb)
        news2 = compute_news2(patient, kb)
        read.news2_total = news2["total"]
        read.news2_max_single = news2["max_single"]
        read.news2_band = news2["band"]
        read.news2_components = news2["components"]
        read.components_missing = sorted(set(news2["missing"]) | set(paed["missing"]))
        read.paediatric_flags = paed["flags"]
        adult_norm = min(1.0, news2["total"] / NEWS2_MAX_PLAUSIBLE)
        paed_norm = min(1.0, paed["points"] / PAEDIATRIC_MAX_PLAUSIBLE)
        read.score = max(adult_norm, paed_norm)
        return read

    # ------------------------------------------------------------------
    # Paediatric
    # ------------------------------------------------------------------
    if selection.primary_id == "paediatric":
        paed = compute_paediatric_read(patient, selection, kb)
        read.paediatric_flags = paed["flags"]
        read.components_missing = paed["missing"]
        read.score = min(1.0, paed["points"] / PAEDIATRIC_MAX_PLAUSIBLE)
        # NEWS2 is NEVER computed here.  Asserted by a dedicated adversarial test.
        read.news2_total = None
        return read

    # ------------------------------------------------------------------
    # Adult / geriatric / adolescent dual
    # ------------------------------------------------------------------
    news2 = compute_news2(patient, kb)
    read.news2_total = news2["total"]
    read.news2_max_single = news2["max_single"]
    read.news2_band = news2["band"]
    read.news2_components = news2["components"]
    read.components_missing = list(news2["missing"])
    score = min(1.0, news2["total"] / NEWS2_MAX_PLAUSIBLE)

    if selection.dual_scored and "paediatric" in selection.envelopes:
        # Adolescent 16-18: score under BOTH, act on the worse [ASM].
        paed = compute_paediatric_read(patient, selection, kb)
        read.paediatric_flags = paed["flags"]
        read.components_missing = sorted(set(read.components_missing) | set(paed["missing"]))
        score = max(score, min(1.0, paed["points"] / PAEDIATRIC_MAX_PLAUSIBLE))

    if selection.primary_id == "geriatric":
        score = _apply_geriatric_modifiers(patient, kb, read, score)

    read.score = score
    return read


def _apply_geriatric_modifiers(patient: Patient, kb: Knowledge, read: RiskRead,
                               score: float) -> float:
    """Blueprint 9.5 table B, implemented literally:

        "The modifiers are implemented as ESCALATE-ONLY QUALIFIERS - absent fever
         REMOVES THE REASSURANCE that a normal temperature would otherwise provide;
         suspected rate control REMOVES THE REASSURANCE of a normal heart rate -
         RATHER THAN AS NUMERIC ADDITIONS TO THE SCORE.

         The literature establishes the DIRECTION of these effects, NOT A MAGNITUDE.
         Implementing them as removals of reassurance is faithful to what is
         actually known.  ASSIGNING THEM POINT VALUES WOULD BE INVENTING PRECISION."

    So this function RETURNS THE GRADIENT UNCHANGED.  What it does instead is record
    which reassurances are void and increment read.set_widening, which widens the
    conformal prediction set toward the acute side.  The result is the behaviour the
    blueprint's opening demo case requires: the point estimate still reads the raw
    numbers as benign, and the ACTED level is the most acute plausible one.

    Escalation itself, where it is warranted, is done by the DETERMINISTIC RULES
    RF-G01 (relative hypotension) and RF-G02 (new confusion) in layer 2 - not here.
    """
    env = kb.envelope("geriatric")
    mods = env["atypical_presentation_modifiers"]["modifiers"]
    by_id = {m["modifier_id"]: m for m in mods}

    temp = patient.value("temperature_c")
    hr = patient.value("heart_rate")

    # GER-MOD-AFEBRILE: a normal temperature NEVER reassures, at any freshness.
    if temp is not None and 36.1 <= temp <= 38.0:
        read.geriatric_modifiers_applied.append("GER-MOD-AFEBRILE")
        read.reassurance_removed.append(
            "normal temperature does not reassure - fever is often absent in acute "
            "infection in older adults [S6]"
        )
        read.set_widening += 1

    # GER-MOD-RATECONTROL: unknown is treated as possible (Blueprint 8.1).
    on_rate_control = patient.rate_control_medication
    if on_rate_control is not False and hr is not None and 51 <= hr <= 90:
        read.geriatric_modifiers_applied.append("GER-MOD-RATECONTROL")
        state = "on rate-control medication" if on_rate_control else "rate control unknown"
        read.reassurance_removed.append(
            f"normal heart rate does not reassure - {state}; beta-blockers and "
            f"calcium-channel blockers mask compensatory tachycardia [S6]"
        )
        read.set_widening += 1

    # GER-MOD-RELHYPO: the ESCALATION is rule RF-G01.  Here it only voids the
    # reassurance an apparently acceptable BP would otherwise carry.
    baseline = patient.baseline_systolic_bp
    sbp = patient.value("systolic_bp")
    if baseline and sbp:
        drop = (baseline - sbp) / float(baseline)
        threshold = float(by_id["GER-MOD-RELHYPO"]["relative_drop_fraction"])
        if drop >= threshold:
            read.geriatric_modifiers_applied.append("GER-MOD-RELHYPO")
            read.reassurance_removed.append(
                f"BP {int(round(drop * 100))}% below her baseline of {baseline} - "
                f"an apparently acceptable BP can represent acute decompensation [S6]"
            )
            read.set_widening += 1

    # GER-MOD-DELIRIUM: the ESCALATION is rule RF-G02.
    if patient.value("consciousness_acvpu") == "C" or patient.value("reported_new_confusion"):
        read.geriatric_modifiers_applied.append("GER-MOD-DELIRIUM")
        read.reassurance_removed.append(
            "new confusion is a presenting sign of sepsis in older adults, not a "
            "soft finding [S6]"
        )
        read.set_widening += 1

    # GER-MOD-FUNCTION: new functional loss.
    if patient.value("ambulatory") is False:
        read.geriatric_modifiers_applied.append("GER-MOD-FUNCTION")
        read.reassurance_removed.append(
            "new inability to walk is a major event regardless of vitals [S6]"
        )
        read.set_widening += 1

    # GER-MOD-FRAILTY: escalate-only, and only from a MATCHED record (rule P3).
    if patient.frailty_indicator and patient.identity.may_reassure:
        read.geriatric_modifiers_applied.append("GER-MOD-FRAILTY")
        read.reassurance_removed.append(
            "frailty recorded - identical vitals carry a different trajectory [S6]"
        )
        read.set_widening += 1

    # The gradient is returned UNTOUCHED.  This is the whole point.
    return score
