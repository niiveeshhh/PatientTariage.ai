"""
The five contradiction detectors - Blueprint 10.2.

"The governing rule for all five: THE SYSTEM NEVER AUTO-RESOLVES. A contradiction is
surfaced as a named flag routed to a human, because it means one of the inputs is
wrong and the system cannot know which.  Averaging two contradictory inputs produces
a number that describes no patient."

No averaging.  No most-recent-wins.  No trust-hierarchy shortcut.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.core.models import ArrivalMode, Contradiction, Patient
from app.clinical.layer1_envelope import EnvelopeSelection
from app.clinical.layer3_risk import RiskRead
from app.clinical.pathways import HIGH_RISK_PATHWAYS, ComplaintMapping

# ASM thresholds.  Blueprint 10.2 names the detectors and their meaning; the exact
# cut-offs at which a discordance becomes "worth a human look" are ours.
BENIGN_RISK_CEILING = 0.25
LOW_PAIN_CEILING = 4
DISTRESS_HR_FLOOR = 100


def detect_all(patient: Patient, selection: EnvelopeSelection, risk: RiskRead,
               mapping: ComplaintMapping, now_min: float) -> List[Contradiction]:
    out: List[Contradiction] = []
    for fn in (_complaint_vs_physiology, _arrival_mode_vs_acuity,
               _self_report_vs_observation, _device_vs_device,
               _record_vs_present_state):
        c = fn(patient, selection, risk, mapping, now_min)
        if c is not None:
            out.append(c)
    return out


# ---------------------------------------------------------------------------

def _complaint_vs_physiology(patient, selection, risk, mapping, now_min
                             ) -> Optional[Contradiction]:
    """Fires when a high-risk complaint pathway is active while all envelope vitals
    are unremarkable, or the inverse.

    "Either the complaint is inflated or the physiology is compensated - both are
    worth a human look."
    """
    high_risk_active = [p for p in mapping.pathways if p in HIGH_RISK_PATHWAYS
                        and HIGH_RISK_PATHWAYS[p] <= 2]
    vitals_benign = risk.score <= BENIGN_RISK_CEILING and not risk.suppressed

    if high_risk_active and vitals_benign:
        label = _friendly_complaint(mapping)
        return Contradiction(
            detector_id="D1_complaint_vs_physiology",
            name="Complaint contradicts physiology",
            card_text=f"{label} reported; vitals unremarkable - verify",
            values={"pathways": sorted(high_risk_active),
                    "risk_score": round(risk.score, 3)},
        )

    # The inverse: markedly deranged physiology with a trivial or absent complaint.
    if risk.score >= 0.55 and not high_risk_active and not mapping.unmapped:
        return Contradiction(
            detector_id="D1_complaint_vs_physiology",
            name="Physiology contradicts complaint",
            card_text="vitals deranged; complaint minor - verify",
            values={"risk_score": round(risk.score, 3),
                    "concepts": sorted(mapping.concepts)},
        )
    return None


def _arrival_mode_vs_acuity(patient, selection, risk, mapping, now_min
                            ) -> Optional[Contradiction]:
    """Ambulance or inter-facility transfer arrival with a benign computed picture.

    "Someone with more information than we have decided this patient needed
    transport. That judgement is evidence."
    """
    if patient.arrival_mode not in (ArrivalMode.AMBULANCE, ArrivalMode.INTER_FACILITY):
        return None
    if risk.suppressed:
        return None
    if risk.score > BENIGN_RISK_CEILING:
        return None
    mode = ("arrived by ambulance" if patient.arrival_mode == ArrivalMode.AMBULANCE
            else "inter-facility transfer")
    return Contradiction(
        detector_id="D2_arrival_mode_vs_acuity",
        name="Arrival mode contradicts computed acuity",
        card_text=f"{mode}; picture benign - verify",
        values={"arrival_mode": patient.arrival_mode.value,
                "risk_score": round(risk.score, 3)},
    )


def _self_report_vs_observation(patient, selection, risk, mapping, now_min
                                ) -> Optional[Contradiction]:
    """Low pain or 'feels fine' against observed distress, guarding, diaphoresis or
    tachycardia.

    "The under-reporting case (complexity 2), and a common route to geriatric and
    cultural under-triage."
    """
    pain = patient.value("pain_score")
    observed_distress = bool(patient.value("observed_distress"))
    diaphoresis = bool(patient.value("diaphoresis"))
    guarding = bool(patient.value("guarding"))
    hr = patient.value("heart_rate")
    tachy = hr is not None and hr >= DISTRESS_HR_FLOOR

    # Collateral (attendant-stated) report contradicting the patient's own denial.
    # Blueprint scenario S-11: "Son reports new confusion; patient denies."  In
    # Indian practice the history often comes from an accompanying relative rather
    # than the patient, so this is a routine case, not an exotic one.
    collateral = patient.value("reported_new_confusion")
    denies = patient.value("patient_denies_confusion")
    if collateral and (denies or patient.value("consciousness_acvpu") == "A"):
        return Contradiction(
            detector_id="D3_self_report_vs_observation",
            name="Collateral report contradicts patient account",
            card_text="relative reports new confusion; patient denies - verify",
            values={"collateral_reports": "new confusion",
                    "patient_account": "denies",
                    "acvpu": patient.value("consciousness_acvpu")},
        )

    signs = []
    if observed_distress:
        signs.append("observed distress")
    if diaphoresis:
        signs.append("diaphoretic")
    if guarding:
        signs.append("guarding")
    if tachy:
        signs.append(f"HR {hr}")

    if not signs:
        return None

    # Blueprint scenario S-09 is the regression test for FALSE POSITIVES here: an
    # abnormality that is fully EXPLAINED by a stated severe pain must not fire.
    if pain is not None and pain >= 7 and len(signs) == 1 and tachy:
        return None

    low_self_report = (pain is not None and pain <= LOW_PAIN_CEILING) or \
                      bool(patient.value("reports_feels_fine"))
    if not low_self_report:
        return None

    pain_text = f"pain {int(pain)}/10" if pain is not None else "reports feeling fine"
    return Contradiction(
        detector_id="D3_self_report_vs_observation",
        name="Self-report contradicts observation",
        card_text=f"{pain_text}; {', '.join(signs)} - verify",
        values={"pain_score": pain, "signs": signs},
    )


def _device_vs_device(patient, selection, risk, mapping, now_min
                      ) -> Optional[Contradiction]:
    """Two measurements of the same or coupled parameters that cannot both be true.

    "One instrument is wrong and we do not know which.  Card names BOTH values.
    No averaging, no most-recent-wins."
    """
    spo2 = patient.value("spo2")
    wob = patient.value("work_of_breathing")
    if spo2 is not None and spo2 >= 97 and wob == "severe":
        return Contradiction(
            detector_id="D4_device_vs_clinical",
            name="Device contradicts clinical picture",
            card_text=f"SpO2 {spo2}% with severe work of breathing - verify both",
            values={"spo2": spo2, "work_of_breathing": wob},
        )

    sbp = patient.value("systolic_bp")
    perf = patient.value("skin_perfusion")
    if sbp is not None and sbp >= 120 and perf in ("mottled", "cyanosed"):
        return Contradiction(
            detector_id="D4_device_vs_clinical",
            name="Device contradicts clinical picture",
            card_text=f"BP {sbp} systolic with {perf} perfusion - verify both",
            values={"systolic_bp": sbp, "skin_perfusion": perf},
        )

    hr_dev = patient.value("heart_rate")
    hr_manual = patient.value("heart_rate_manual")
    if hr_dev is not None and hr_manual is not None and abs(hr_dev - hr_manual) >= 25:
        return Contradiction(
            detector_id="D4_device_vs_device",
            name="Device contradicts device",
            card_text=f"HR device {hr_dev}, manual {hr_manual} - verify both",
            values={"heart_rate": hr_dev, "heart_rate_manual": hr_manual},
        )
    return None


def _record_vs_present_state(patient, selection, risk, mapping, now_min
                             ) -> Optional[Contradiction]:
    """Record-derived history materially inconsistent with the observed patient.

    "The strongest available signal of a WRONG IDENTITY MATCH."
    """
    # A documented paediatric age against an adult presentation, or vice versa.
    documented_age = patient.value("record_documented_age_days")
    if documented_age is not None and patient.age_days is not None:
        if abs(documented_age - patient.age_days) > 5 * 365:
            return Contradiction(
                detector_id="D5_record_vs_present_state",
                name="Record may not match this patient",
                card_text="record age differs from observed age - confirm identity",
                values={"record_age_days": documented_age,
                        "observed_age_days": patient.age_days},
            )

    # A recorded medication contradicted by the patient's own account.
    if patient.rate_control_medication and patient.value("patient_denies_medication"):
        return Contradiction(
            detector_id="D5_record_vs_present_state",
            name="Record contradicts patient account",
            card_text="record lists rate-control drug; patient denies - confirm identity",
            values={"record_rate_control": True, "patient_denies": True},
        )

    # A PROVISIONAL match is ITSELF a record-vs-present-state contradiction: the
    # record and the person in front of you may not be the same person, and the
    # system cannot know which.  Blueprint complexity 6: "the dangerous case is not
    # NO record - it is a CONFIDENT WRONG record."  Surfacing it is the whole point.
    if patient.identity.match_state.value == "PROVISIONAL":
        n = len(patient.identity.candidate_record_ids)
        detail = (f"{n} candidate records" if n > 1
                  else "matched on "
                       + ("+".join(sorted(patient.identity.matched_fields)) or "weak fields"))
        return Contradiction(
            detector_id="D5_record_vs_present_state",
            name="Record may not match this patient",
            card_text=f"provisional match ({detail}) - confirm identity before using history",
            values={"match_state": "PROVISIONAL",
                    "candidate_record_count": n,
                    "matched_fields": sorted(patient.identity.matched_fields)},
        )
    return None


def _friendly_complaint(mapping: ComplaintMapping) -> str:
    if "chest_pain" in mapping.concepts:
        return "chest pain"
    if "focal_weakness" in mapping.concepts or "slurred_speech" in mapping.concepts:
        return "stroke symptoms"
    if "abdominal_pain" in mapping.concepts:
        return "abdominal pain"
    if "back_pain" in mapping.concepts:
        return "severe back pain"
    if "breathlessness" in mapping.concepts:
        return "breathlessness"
    if mapping.concepts:
        return mapping.concepts[0].replace("_", " ")
    return "complaint"
