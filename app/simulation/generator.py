"""
Seeded arrival generator - Blueprint 17.1/17.2.

Distributions are taken from Blueprint 7.2 and 17.2, not invented.  The entire
cohort is generated from a SINGLE SEED, so the demo run, the metric run and the
baseline comparisons all use the same arrival stream and differences between
policies are attributable to the policy rather than the sample.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.core.models import (
    AgeSource, ArrivalMode, CommunicationBarrier, IdentityLink, MatchState,
    MissingReason, Observation, Patient, PregnancyStatus, Provenance, Quality, absent,
)
from app.simulation.trajectories import LatentTrajectory, make_trajectory

Y = 365

# Blueprint 7.2 / 17.2 - every figure below is cited there.
AGE_BANDS = [("paediatric", 0.20), ("adult", 0.59), ("geriatric", 0.21)]
PAED_SUBBANDS = [((0, 27), 0.04), ((28, 364), 0.14), ((365, 3 * Y), 0.24),
                 ((3 * Y, 5 * Y), 0.16), ((5 * Y, 12 * Y), 0.26), ((12 * Y, 18 * Y), 0.16)]
ACUITY_MIX = [(1, 0.01), (2, 0.11), (3, 0.33), (4, 0.34), (5, 0.21)]
P_PRIOR_RECORD = 0.50
P_AMBIGUOUS_MATCH = 0.15
P_COMORBIDITY_ADULT = 0.38
P_COMORBIDITY_GERIATRIC = 0.78
P_AMBIGUOUS_PRESENTATION = 0.18
P_MISSING_CRITICAL = 0.30
P_CONTRADICTORY = 0.07
P_COMM_LIMITATION = 0.12
P_RED_FLAG_AT_ARRIVAL = 0.06
P_DETERIORATION = 0.06
P_PREGNANCY_F_15_49 = 0.04

COMPLAINTS_BY_LEVEL: Dict[int, List[str]] = {
    1: ["found unresponsive", "severe breathing difficulty", "crushing central chest pain"],
    2: ["chest pain", "short of breath", "right arm weakness and slurred speech",
        "severe abdominal pain", "fever and confusion"],
    3: ["abdominal pain", "headache", "vomiting and diarrhoea", "dizziness",
        "fever", "back pain", "palpitations"],
    4: ["ankle injury after a fall", "sore throat", "rash", "wrist injury",
        "urine burning when passing"],
    5: ["sore knee", "medication request", "dressing change", "mild rash"],
}

PAED_COMPLAINTS: Dict[int, List[str]] = {
    1: ["not waking, floppy"], 2: ["breathing difficulty, nasal flaring", "fever and lethargic"],
    3: ["fever", "vomiting and diarrhoea", "not himself"],
    4: ["rash", "sore throat", "minor fall"], 5: ["mild rash"],
}


def _pick(rng: random.Random, weighted: List[Tuple[Any, float]]) -> Any:
    r = rng.random()
    acc = 0.0
    for value, w in weighted:
        acc += w
        if r <= acc:
            return value
    return weighted[-1][0]


def _diurnal_rate(hour: float, profile: Dict[str, Any]) -> float:
    """Blueprint 7.1: peak factor 1.8x over a late-morning and an evening peak;
    off-peak 0.58x overnight.  "A flat rate would make the queue behave
    unrealistically smoothly and would flatter our results." """
    v = profile["volume"]
    mean = float(v["mean_arrival_rate_per_hour"])
    h = hour % 24.0
    shape = (1.0
             + 0.55 * math.exp(-((h - 11.0) ** 2) / 8.0)
             + 0.60 * math.exp(-((h - 19.0) ** 2) / 8.0)
             - 0.42 * math.exp(-((h - 4.0) ** 2) / 12.0))
    return max(0.15 * mean, mean * shape)


@dataclass
class GeneratedPatient:
    patient: Patient
    trajectory: LatentTrajectory
    apparent_level: int


@dataclass
class Cohort:
    patients: List[GeneratedPatient] = field(default_factory=list)
    seed: int = 0
    profile_id: str = ""
    horizon_min: float = 0.0

    def summary(self) -> Dict[str, Any]:
        n = len(self.patients)
        if n == 0:
            return {"n": 0}
        det = sum(1 for g in self.patients if g.trajectory.deteriorates)
        paed = sum(1 for g in self.patients
                   if g.patient.age_days is not None and g.patient.age_days < 16 * Y)
        ger = sum(1 for g in self.patients
                  if g.patient.age_days is not None and g.patient.age_days >= 65 * Y)
        zero = sum(1 for g in self.patients
                   if g.patient.identity.match_state == MatchState.UNMATCHED)
        prov = sum(1 for g in self.patients
                   if g.patient.identity.match_state == MatchState.PROVISIONAL)
        comm = sum(1 for g in self.patients if g.patient.communication_barrier)
        return {
            "n": n, "seed": self.seed, "profile_id": self.profile_id,
            "horizon_min": self.horizon_min,
            "deteriorating": det, "deteriorating_fraction": round(det / n, 3),
            "paediatric": paed, "geriatric": ger,
            "zero_history": zero, "zero_history_fraction": round(zero / n, 3),
            "provisional_match": prov,
            "communication_limited": comm,
        }


def generate_cohort(profile: Dict[str, Any], seed: int, horizon_min: float = 1440.0,
                    load_multiplier: float = 1.0, start_hour: float = 0.0,
                    missing_rate: Optional[float] = None,
                    deterioration_rate: Optional[float] = None,
                    ambiguity_rate: Optional[float] = None,
                    ref_prefix: str = "G") -> Cohort:
    """Generate a reproducible arrival stream.  Same seed -> byte-identical cohort."""
    rng = random.Random(seed)
    cohort = Cohort(seed=seed, profile_id=profile.get("profile_id", ""),
                    horizon_min=horizon_min)
    p_missing = P_MISSING_CRITICAL if missing_rate is None else missing_rate
    p_det = P_DETERIORATION if deterioration_rate is None else deterioration_rate
    p_amb = P_AMBIGUOUS_PRESENTATION if ambiguity_rate is None else ambiguity_rate

    t = 0.0
    idx = 0
    while t < horizon_min:
        hour = start_hour + t / 60.0
        rate = _diurnal_rate(hour, profile) * load_multiplier
        gap = rng.expovariate(max(rate, 0.01) / 60.0)      # Poisson arrivals
        t += gap
        if t >= horizon_min:
            break
        idx += 1
        cohort.patients.append(
            _make_patient(rng, profile, t, f"{ref_prefix}-{idx:04d}",
                          p_missing, p_det, p_amb, horizon_min))
    return cohort


def _make_patient(rng, profile, arrival_min, ref, p_missing, p_det, p_amb,
                  horizon_min) -> GeneratedPatient:
    band = _pick(rng, AGE_BANDS)
    if band == "paediatric":
        (lo, hi) = _pick(rng, PAED_SUBBANDS)
        age_days = rng.randint(int(lo), int(hi))
    elif band == "adult":
        age_days = rng.randint(16 * Y, 64 * Y)
    else:
        age_days = rng.randint(65 * Y, 95 * Y)

    level = _pick(rng, ACUITY_MIX)
    sex = rng.choice(["M", "F"])
    paediatric = age_days < 16 * Y

    pool = PAED_COMPLAINTS.get(level, PAED_COMPLAINTS[3]) if paediatric \
        else COMPLAINTS_BY_LEVEL[level]
    complaint = rng.choice(pool)
    if rng.random() < p_amb:
        complaint = rng.choice(["weak, not eating", "just not right", "dizziness",
                                "not himself", "generally unwell", "indigestion"])

    # --- identity (Blueprint 7.2: 50% have a record; 15% of those ambiguous) ---
    identity = IdentityLink()
    record_id = None
    if rng.random() < P_PRIOR_RECORD:
        record_id = f"R-{rng.randint(10000, 99999)}"
        if rng.random() < P_AMBIGUOUS_MATCH:
            identity = IdentityLink(match_state=MatchState.PROVISIONAL,
                                    identity_confidence=rng.uniform(0.52, 0.88),
                                    matched_fields=["name", "age"],
                                    candidate_record_ids=[record_id])
        else:
            identity = IdentityLink(match_state=MatchState.MATCHED,
                                    identity_confidence=rng.uniform(0.91, 0.99),
                                    matched_fields=["name", "dob", "phone"],
                                    candidate_record_ids=[record_id])

    ambulance_p = float(profile.get("patient_mix", {}).get("ambulance_arrival_fraction", 0.1))
    arrival_mode = ArrivalMode.AMBULANCE if rng.random() < ambulance_p else ArrivalMode.WALK_IN

    pregnancy = PregnancyStatus.NOT_APPLICABLE
    if sex == "F" and 12 * Y <= age_days <= 55 * Y:
        r = rng.random()
        pregnancy = (PregnancyStatus.YES if r < P_PREGNANCY_F_15_49
                     else PregnancyStatus.UNKNOWN if r < P_PREGNANCY_F_15_49 + 0.05
                     else PregnancyStatus.NO)

    comm = rng.random() < P_COMM_LIMITATION
    comm_kind = CommunicationBarrier.NONE
    if comm:
        comm_kind = rng.choice([CommunicationBarrier.LANGUAGE, CommunicationBarrier.COGNITION,
                                CommunicationBarrier.CONSCIOUSNESS,
                                CommunicationBarrier.PRE_VERBAL if paediatric
                                else CommunicationBarrier.PHYSICAL])

    geriatric = age_days >= 65 * Y
    p_comorbid = P_COMORBIDITY_GERIATRIC if geriatric else P_COMORBIDITY_ADULT
    conditions: List[str] = []
    baseline_bp = None
    rate_control = None
    if not paediatric and rng.random() < p_comorbid:
        conditions = rng.sample(["hypertension", "diabetes", "copd", "asthma",
                                 "atrial_fibrillation", "ckd"], k=rng.randint(1, 2))
        if identity.match_state == MatchState.MATCHED:
            baseline_bp = rng.randint(120, 165)
            rate_control = rng.random() < 0.35

    patient = Patient(
        patient_ref=ref, arrival_timestamp_min=arrival_min,
        chair=f"{rng.randint(1, 40):02d}",
        age_days=age_days, age_source=AgeSource.DOCUMENT if not comm else AgeSource.ESTIMATED,
        age_estimated=comm, sex=sex, arrival_mode=arrival_mode,
        record_id=record_id, identity=identity,
        stated_chief_complaint=complaint,
        communication_barrier=comm, communication_barrier_kind=comm_kind,
        self_report_channel_available=not comm,
        pregnancy_status=pregnancy,
        known_conditions=conditions,
        baseline_systolic_bp=baseline_bp,
        rate_control_medication=rate_control,
        frailty_indicator=(geriatric and rng.random() < 0.3) or None,
        synthetic=True,
    )

    red_flag = rng.random() < P_RED_FLAG_AT_ARRIVAL or level == 1
    _emit_vitals(rng, patient, age_days, level, paediatric, arrival_min,
                 red_flag, p_missing)

    if rng.random() < P_CONTRADICTORY:
        _inject_contradiction(rng, patient, arrival_min)

    deteriorates = rng.random() < p_det
    traj = make_trajectory(rng, deteriorates, paediatric, level,
                           wait_horizon_min=min(180.0, horizon_min))
    patient._latent = traj.to_dict()
    return GeneratedPatient(patient=patient, trajectory=traj, apparent_level=level)


def _band_thresholds(age_days: int) -> Tuple[int, int]:
    for (hi, hr, rr) in ((27, 190, 60), (364, 180, 55), (3 * Y, 140, 40),
                         (5 * Y, 120, 35), (12 * Y, 120, 30), (18 * Y, 100, 20)):
        if age_days <= hi:
            return hr, rr
    return 100, 20


def _emit_vitals(rng, patient, age_days, level, paediatric, t, red_flag, p_missing):
    """Emit vitals consistent with the APPARENT acuity level.

    Blueprint 7.2: red flag present at arrival = 6%, "approximately the L1+L2 share
    less those who present WITHOUT an immediate hard trigger".  So a hard trigger is
    DELIBERATE rather than incidental: level 1 always carries one, level 2 about half
    the time, levels 3-5 essentially never.  Letting derangement leak out of the
    severity curve would make the board 20% red and prove only that the generator is
    miscalibrated.
    """
    hr_thr, rr_thr = _band_thresholds(age_days)
    sev = {1: 0.92, 2: 0.58, 3: 0.34, 4: 0.16, 5: 0.06}[level]
    hard_trigger = (level == 1) or (level == 2 and rng.random() < 0.5) or red_flag

    def jitter(base, spread):
        return base + rng.gauss(0, spread)

    hr = int(max(35, jitter(hr_thr * (0.60 + 0.33 * sev), hr_thr * 0.06)))
    rr = int(max(6, jitter(rr_thr * (0.58 + 0.34 * sev), rr_thr * 0.08)))
    spo2 = int(max(80, min(100, jitter(99 - 5.0 * sev, 1.2))))
    sbp = int(max(80, jitter(128 - 18 * sev, 11)))
    dbp = int(max(38, sbp * 0.62 + rng.gauss(0, 5)))
    temp = round(jitter(36.9 + 1.5 * sev * (1 if rng.random() < 0.55 else -0.2), 0.45), 1)
    pain = int(max(0, min(10, jitter(2 + 7 * sev, 2))))
    acvpu = "A"

    if hard_trigger:
        # Push exactly ONE parameter decisively past its threshold, so the fired
        # rule is nameable and the case is defensible.
        which = rng.choice(["spo2", "rr", "hr", "bp", "acvpu", "temp"])
        if which == "spo2":
            spo2 = rng.randint(84, 91)
        elif which == "rr":
            rr = int(rr_thr * rng.uniform(1.15, 1.6)) if paediatric else rng.randint(25, 34)
        elif which == "hr":
            hr = int(hr_thr * rng.uniform(1.1, 1.45)) if paediatric else rng.randint(131, 165)
        elif which == "bp":
            sbp = rng.randint(72, 90)
            dbp = int(sbp * 0.6)
        elif which == "acvpu":
            acvpu = rng.choice(["C", "V", "P"])
        else:
            temp = round(rng.uniform(39.1, 40.6) if paediatric
                         else rng.uniform(38.6, 40.4), 1)

    vals: Dict[str, Any] = {
        "heart_rate": hr, "respiratory_rate": rr, "spo2": spo2,
        "systolic_bp": sbp, "diastolic_bp": dbp, "temperature_c": temp,
        "consciousness_acvpu": acvpu, "pain_score": pain,
        "ambulatory": sev < 0.7,
    }
    if paediatric:
        vals["work_of_breathing"] = ("severe" if hard_trigger and sev > 0.85
                                     else "moderate" if hard_trigger and sev > 0.5
                                     else "mild" if sev > 0.35 else "normal")
        vals["capillary_refill_seconds"] = round(
            (2.8 + rng.random()) if hard_trigger and sev > 0.5 else 1.4 + 0.8 * sev, 1)
        vals["behavioural_state"] = "lethargic" if hard_trigger and sev > 0.6 else "settled"
        vals["carer_concern"] = rng.random() < (0.08 + 0.35 * sev)
        if age_days < 4 * Y:
            vals.pop("systolic_bp", None)
            vals.pop("diastolic_bp", None)
            patient.set_observation(
                absent("systolic_bp", MissingReason.NOT_APPLICABLE, t))

    prov_field = {"consciousness_acvpu": Provenance.OBS, "ambulatory": Provenance.OBS,
                  "work_of_breathing": Provenance.OBS, "behavioural_state": Provenance.OBS,
                  "capillary_refill_seconds": Provenance.OBS,
                  "pain_score": Provenance.PT, "carer_concern": Provenance.ATT}

    critical = ["spo2", "systolic_bp", "heart_rate", "respiratory_rate",
                "consciousness_acvpu"]
    drop: Optional[str] = None
    if rng.random() < p_missing:
        drop = rng.choice([c for c in critical if c in vals])

    for field_name, value in vals.items():
        if field_name == drop:
            patient.set_observation(absent(
                field_name,
                rng.choice([MissingReason.NOT_YET_TAKEN, MissingReason.DEVICE_FAILED,
                            MissingReason.REFUSED, MissingReason.NOT_OBTAINABLE]),
                t))
            continue
        prov = prov_field.get(field_name, Provenance.DEV)
        age = rng.choice([0.0, 0.0, 0.0, 0.0, rng.uniform(5, 30)])
        patient.set_observation(Observation(
            field_name=field_name, value=value, provenance=prov,
            timestamp_min=t - age, quality=Quality.CLEAN,
            source_confidence=0.95 if prov in (Provenance.DEV, Provenance.OBS) else 0.6))

    if rng.random() < 0.03:
        bad = rng.choice([("systolic_bp", 0), ("heart_rate", 300), ("spo2", 12),
                          ("temperature_c", 45.0)])
        patient.set_observation(Observation(
            field_name=bad[0], value=bad[1], provenance=Provenance.DEV,
            timestamp_min=t, quality=Quality.CLEAN, source_confidence=0.9))


def _inject_contradiction(rng, patient, t):
    kind = rng.choice(["self_report", "device", "arrival_mode"])
    if kind == "self_report":
        patient.set_observation(Observation("pain_score", rng.randint(0, 3),
                                            Provenance.PT, t, Quality.CLEAN, 0.5))
        patient.set_observation(Observation("diaphoresis", True, Provenance.OBS, t,
                                            Quality.CLEAN, 0.9))
        patient.set_observation(Observation("observed_distress", True, Provenance.OBS,
                                            t, Quality.CLEAN, 0.9))
    elif kind == "device":
        patient.set_observation(Observation("spo2", 99, Provenance.DEV, t,
                                            Quality.CLEAN, 0.95))
        patient.set_observation(Observation("work_of_breathing", "severe",
                                            Provenance.OBS, t, Quality.CLEAN, 0.9))
    else:
        patient.arrival_mode = ArrivalMode.AMBULANCE
        for f, v in (("heart_rate", 76), ("respiratory_rate", 16), ("spo2", 99),
                     ("systolic_bp", 124), ("temperature_c", 36.8)):
            patient.set_observation(Observation(f, v, Provenance.DEV, t,
                                                Quality.CLEAN, 0.95))
