"""
Build the 32-scenario library - Blueprint 17.3.

"32 hand-authored scenarios, 17 of them adversarial (53%).  The brief asks for
30-40% adversarial; we are deliberately above it, because a library that only
demonstrates success demonstrates nothing."

Each scenario carries an EXPECTED-BEHAVIOUR ENVELOPE - a RANGE of acceptable
outputs, not a single label - written BEFORE the system output is evaluated
(Blueprint 18.1 tier G2).  "Honest about clinical reality: a 7-year-old with those
numbers is defensibly a 3 or a 4."

Run:  python scripts/build_scenarios.py
Emits: data/scenarios/scenarios.json
"""

from __future__ import annotations

import io
import json
import os

Y = 365  # days per year, for readability below


def S(sid, adversarial, title, demographics, complaint, observations,
      history=None, expected=None, complexity=None, wow=None, latent=None,
      arrival_offset_min=0.0, notes="", injections=None, paired_arrival=None):
    return {
        "scenario_id": sid,
        "adversarial": adversarial,
        "title": title,
        "arrival_offset_min": arrival_offset_min,
        "demographics": demographics,
        "stated_chief_complaint": complaint,
        "observations": observations,
        "history": history or {},
        "latent_trajectory": latent,
        "injections": injections or [],
        "paired_arrival": paired_arrival,
        "expected_behaviour_envelope": expected or {},
        "blueprint_complexity": complexity or [],
        "wow_moment": wow,
        "notes": notes,
    }


SCENARIOS = [

    S("S-01", False, "28 M, ankle injury after fall - the control case",
      {"age_days": 28 * Y, "age_source": "document", "sex": "M",
       "arrival_mode": "walk_in"},
      "ankle injury after a fall, walking",
      {"heart_rate": 82, "systolic_bp": 124, "diastolic_bp": 78,
       "respiratory_rate": 16, "spo2": 99, "temperature_c": 36.8,
       "consciousness_acvpu": "A", "pain_score": 6, "ambulatory": True},
      history={"identity": "UNMATCHED"},
      expected={"acted_level_range": [3, 4], "uncertainty_class_in": ["CLEAR"],
                "required_fired_rules": [], "forbidden_fired_rules": ["RF-A01", "RF-A07"],
                "ttl_max_minutes": 60, "queue_class_in": ["N"],
                "prediction_set_max_width": 3},
      complexity=[],
      notes="The control case. If this generates any alert, the system is too noisy. "
            "Blueprint 17.3 S-01."),

    S("S-02", False, "45 F, known migraine with photophobia",
      {"age_days": 45 * Y, "age_source": "document", "sex": "F",
       "arrival_mode": "walk_in", "pregnancy_status": "no"},
      "migraine headache with photophobia",
      {"heart_rate": 88, "systolic_bp": 130, "diastolic_bp": 82,
       "respiratory_rate": 18, "spo2": 98, "temperature_c": 36.9,
       "consciousness_acvpu": "A", "pain_score": 8, "ambulatory": True},
      history={"identity": "MATCHED", "identity_confidence": 0.97,
               "known_conditions": ["migraine"], "prior_ed_visits_90d": 2},
      expected={"acted_level_range": [2, 4], "uncertainty_class_in": ["CLEAR", "THIN", "CONFLICTED"],
                "ttl_max_minutes": 60, "queue_class_in": ["N"],
                "assert_matched_record_does_not_lower_below_pain_floor": True},
      complexity=[6, 12],
      notes="Tests that a matched record with a benign prior pattern does not lower "
            "the acted level below the pain-driven floor. Blueprint 17.3 S-02."),

    S("S-03", False, "58 M, crushing central chest pain, ambulance, diaphoretic",
      {"age_days": 58 * Y, "age_source": "document", "sex": "M",
       "arrival_mode": "ambulance"},
      "crushing central chest pain, onset 35 minutes ago",
      {"heart_rate": 112, "systolic_bp": 96, "diastolic_bp": 58,
       "respiratory_rate": 24, "spo2": 93, "temperature_c": 36.5,
       "consciousness_acvpu": "A", "pain_score": 9, "diaphoresis": True,
       "stated_onset_time_min": -35, "ambulatory": False},
      history={"identity": "MATCHED", "identity_confidence": 0.96,
               "known_conditions": ["hypertension", "smoker"]},
      expected={"acted_level_range": [1, 2], "uncertainty_class_in": ["CLEAR", "THIN", "CONFLICTED"],
                "required_queue_class": "R", "ttl_max_minutes": 15,
                "required_pathway_clocks": ["TCP-STEMI"],
                "required_fired_rules_any": ["RF-A04", "RF-A05", "RF-A09",
                                             "RF-A12", "RF-A13"]},
      complexity=[6],
      notes="Hard rule fires immediately. Time-critical cardiac pathway clock opens "
            "alongside the TTL. Blueprint 17.3 S-03."),

    S("S-04", False, "3 y, febrile and lethargic, carried in",
      {"age_days": 3 * Y, "age_source": "document", "sex": "M",
       "arrival_mode": "walk_in"},
      "fever and lethargic, carried in",
      {"heart_rate": 178, "respiratory_rate": 48, "spo2": 96,
       "temperature_c": 39.6, "consciousness_acvpu": "V",
       "capillary_refill_seconds": 3.0, "work_of_breathing": "mild",
       "behavioural_state": "lethargic",
       "systolic_bp": {"absent": "not_applicable"},
       "blood_glucose_mgdl": {"absent": "not_yet_taken"}},
      history={"identity": "UNMATCHED"},
      expected={"acted_level_range": [1, 2], "uncertainty_class_in": ["THIN", "CONFLICTED", "BLIND"],
                "required_queue_class": "R", "ttl_max_minutes": 15,
                "required_envelope": "paediatric",
                "required_fired_rules_any": ["RF-P01", "RF-P02", "RF-A02", "RF-P09"],
                "assert_bp_absence_is_not_applicable": True},
      complexity=[3, 4, 5],
      notes="Paediatric 1-3 y band crossed on BOTH HR>140 and RR>40 [S5]; ACVPU below A. "
            "BP absence correctly marked not applicable with CRT substituting. S-04."),

    S("S-05", False, "22 F, asthma exacerbation, speaking in single words",
      {"age_days": 22 * Y, "age_source": "document", "sex": "F",
       "arrival_mode": "walk_in", "pregnancy_status": "no"},
      "asthma, short of breath, speaking in single words",
      {"heart_rate": 124, "systolic_bp": 118, "diastolic_bp": 74,
       "respiratory_rate": 32, "spo2": 89, "temperature_c": 37.0,
       "consciousness_acvpu": "A", "work_of_breathing": "severe", "ambulatory": True},
      history={"identity": "MATCHED", "identity_confidence": 0.95,
               "known_conditions": ["asthma", "prior_icu_admission"]},
      expected={"acted_level_range": [1, 2], "uncertainty_class_in": ["CLEAR", "THIN", "CONFLICTED"],
                "required_queue_class": "R", "ttl_max_minutes": 15,
                "required_fired_rules_any": ["RF-A04", "RF-P07"],
                "assert_prior_icu_cannot_lower": True},
      complexity=[6],
      notes="SpO2 below the adult threshold [S5] fires the hard rule. Prior ICU "
            "admission raises risk and cannot lower it. S-05."),

    S("S-06", True, "18-day neonate, 'feeding a bit less', parents calm, looks well",
      {"age_days": 18, "age_source": "document", "sex": "M",
       "arrival_mode": "walk_in"},
      "feeding a bit less than usual",
      {"heart_rate": 176, "respiratory_rate": 52, "spo2": 97,
       "temperature_c": 38.2, "consciousness_acvpu": "A",
       "work_of_breathing": "normal", "capillary_refill_seconds": 2.0,
       "behavioural_state": "settled", "carer_concern": False,
       "systolic_bp": {"absent": "not_applicable"},
       "blood_glucose_mgdl": {"absent": "not_yet_taken"}},
      history={"identity": "UNMATCHED"},
      expected={"acted_level_max": 2, "uncertainty_class_in": ["THIN", "CONFLICTED", "BLIND"],
                "required_fired_rules": ["RF-P04"], "required_envelope": "paediatric",
                "ttl_max_minutes": 15,
                "assert_benign_appearance_cannot_suppress_age_rule": True},
      complexity=[3, 4],
      notes="THE APPEARANCE IS THE TRAP. Fever >38 C in an infant under 28 days is at "
            "minimum a level-2 situation REGARDLESS of how well the child looks [S5]. "
            "Asserts a benign presentation cannot suppress an age-specific rule. S-06."),

    S("S-07", True, "3 y, 'not himself' per mother, playing intermittently",
      {"age_days": 3 * Y, "age_source": "document", "sex": "M",
       "arrival_mode": "walk_in"},
      "not himself, mother concerned",
      {"heart_rate": 168, "respiratory_rate": 34, "spo2": 97,
       "temperature_c": 39.2, "consciousness_acvpu": "A",
       "capillary_refill_seconds": 2.0, "work_of_breathing": "normal",
       "carer_concern": True, "behavioural_state": "settled",
       "systolic_bp": {"absent": "not_applicable"}},
      history={"identity": "UNMATCHED"},
      expected={"acted_level_range": [2, 3], "uncertainty_class_in": ["THIN", "CONFLICTED", "BLIND"],
                "required_fired_rules": ["RF-P01", "RF-P10"],
                "required_envelope": "paediatric",
                "assert_adult_envelope_would_be_benign": True,
                "ttl_max_minutes": 30},
      complexity=[3, 4],
      wow="WOW-2 age-band flip",
      notes="THE AGE-BAND FLIP DEMO. Under an adult envelope this scores near zero. "
            "Under the 1-3 y band HR>140 fires [S5], the fever rule applies, and the "
            "carer-concern trigger escalates regardless of the rest [S11]. S-07."),

    S("S-08", True, "7 y, abdominal pain 2 days, walking, guarding on movement",
      {"age_days": 7 * Y, "age_source": "document", "sex": "F",
       "arrival_mode": "walk_in"},
      "abdominal pain for two days, guarding on movement",
      {"heart_rate": 118, "systolic_bp": 102, "diastolic_bp": 64,
       "respiratory_rate": 24, "spo2": 99, "temperature_c": 37.4,
       "consciousness_acvpu": "A", "pain_score": 4, "guarding": True,
       "capillary_refill_seconds": 2.0, "work_of_breathing": "normal",
       "ambulatory": True},
      history={"identity": "UNMATCHED"},
      expected={"acted_level_range": [2, 3], "prediction_set_min_width": 2,
                "uncertainty_class_in": ["THIN", "CONFLICTED"],
                "required_envelope": "paediatric",
                "escalation_premium_min": 0.0,
                "assert_near_threshold_widens_rather_than_resolves_benign": True},
      complexity=[1, 3],
      notes="Sits JUST BELOW the 5-12 y HR threshold of 120 [S5]. Tests that a "
            "near-threshold paediatric case WIDENS THE SET rather than resolving to "
            "the benign level. S-08."),

    S("S-09", False, "14 M, sports injury, obvious forearm deformity",
      {"age_days": 14 * Y, "age_source": "document", "sex": "M",
       "arrival_mode": "walk_in"},
      "sports injury, forearm deformity after a fall",
      {"heart_rate": 104, "systolic_bp": 126, "diastolic_bp": 70,
       "respiratory_rate": 18, "spo2": 99, "temperature_c": 36.9,
       "consciousness_acvpu": "A", "pain_score": 9,
       "capillary_refill_seconds": 2.0, "work_of_breathing": "normal",
       "ambulatory": True},
      history={"identity": "UNMATCHED"},
      expected={"acted_level_range": [2, 4], "uncertainty_class_in": ["CLEAR", "THIN"],
                "forbidden_contradiction_detectors": ["D3_self_report_vs_observation"],
                "required_envelope": "paediatric"},
      complexity=[1, 12],
      notes="HR just above the 12-18 y threshold of 100 [S5] but COHERENT with severe "
            "pain. Tests that the coherence detector does NOT fire on an EXPLAINED "
            "abnormality. A false contradiction flag here is a DEFECT. S-09."),

    S("S-10", False, "6 mo, bronchiolitis, moderate work of breathing, nasal flaring",
      {"age_days": 180, "age_source": "document", "sex": "F",
       "arrival_mode": "walk_in"},
      "breathing difficulty, nasal flaring",
      {"heart_rate": 168, "respiratory_rate": 62, "spo2": 91,
       "temperature_c": 37.8, "consciousness_acvpu": "A",
       "work_of_breathing": "moderate", "capillary_refill_seconds": 2.0,
       "behavioural_state": "settled",
       "systolic_bp": {"absent": "not_applicable"}},
      history={"identity": "UNMATCHED"},
      expected={"acted_level_range": [1, 2], "required_queue_class": "R",
                "required_fired_rules_any": ["RF-P02", "RF-P03", "RF-P08"],
                "required_envelope": "paediatric", "ttl_max_minutes": 15},
      complexity=[3, 4],
      notes="1-12 mo RR threshold of 55 crossed [S5] plus low saturation. Work of "
            "breathing scored as a first-class parameter per national PEWS [S11]. S-10."),

    S("S-11", True, "71 M, 'weak, not eating, 2 days', walked in with his son",
      {"age_days": 71 * Y, "age_source": "document", "sex": "M",
       "arrival_mode": "walk_in"},
      "weak, not eating, two days",
      {"heart_rate": 78, "systolic_bp": 104, "diastolic_bp": 60,
       "respiratory_rate": 20, "spo2": 95, "temperature_c": 36.4,
       "consciousness_acvpu": "A", "pain_score": 0, "ambulatory": True,
       "reported_new_confusion": {"value": True, "provenance": "Att"},
       "patient_denies_confusion": {"value": True, "provenance": "Pt"}},
      history={"identity": "MATCHED", "identity_confidence": 0.97,
               "baseline_systolic_bp": 150, "baseline_bp_age_days": 120,
               "rate_control_medication": True, "baseline_oriented": True},
      expected={"acted_level_range": [2, 2], "prediction_set_equals": [2, 3, 4],
                "uncertainty_class_in": ["CONFLICTED"],
                "required_fired_rules_any": ["RF-G01", "RF-G02"],
                "required_envelope": "geriatric",
                "point_estimate_min": 3, "escalation_premium_min": 1.0,
                "ttl_max_minutes": 15},
      complexity=[2, 3, 4, 6],
      wow="WOW-1 conformal escalation",
      notes="THE OPENING DEMO CASE. Geriatric envelope removes the reassurance of the "
            "normal temperature and normal HR (immunosenescence; rate-control masking) "
            "and computes relative hypotension against baseline [S6]. Self-report vs "
            "collateral contradiction fires. Card reads 'acting on worst of 3 plausible "
            "levels'. S-11."),

    S("S-12", False, "82 F, mechanical fall at home, no LOC, hip pain, lives alone",
      {"age_days": 82 * Y, "age_source": "document", "sex": "F",
       "arrival_mode": "ambulance"},
      "fall at home, hip pain",
      {"heart_rate": 88, "systolic_bp": 138, "diastolic_bp": 78,
       "respiratory_rate": 18, "spo2": 96, "temperature_c": 36.7,
       "consciousness_acvpu": "A", "pain_score": 7, "ambulatory": False},
      history={"identity": "UNMATCHED"},
      expected={"acted_level_range": [2, 3], "uncertainty_class_in": ["THIN", "CONFLICTED"],
                "required_envelope": "geriatric",
                "assert_no_baseline_raises_uncertainty_not_reassurance": True},
      complexity=[3, 4, 6],
      notes="No baseline means relative-hypotension logic CANNOT RUN, which raises "
            "uncertainty rather than defaulting to reassurance. Frailty modifier "
            "escalates only. S-12."),

    S("S-13", True, "68 F, 'indigestion', took an antacid at home, no chest pain",
      {"age_days": 68 * Y, "age_source": "document", "sex": "F",
       "arrival_mode": "walk_in", "pregnancy_status": "not_applicable"},
      "indigestion, took an antacid at home",
      {"heart_rate": 96, "systolic_bp": 142, "diastolic_bp": 88,
       "respiratory_rate": 20, "spo2": 96, "temperature_c": 36.6,
       "consciousness_acvpu": "A", "pain_score": 3, "diaphoresis": True,
       "ambulatory": True},
      history={"identity": "MATCHED", "identity_confidence": 0.94,
               "known_conditions": ["diabetes"]},
      expected={"acted_level_range": [2, 3], "prediction_set_min_width": 2,
                "uncertainty_class_in": ["CONFLICTED"],
                "required_contradiction_detectors": ["D3_self_report_vs_observation"],
                "required_envelope": "geriatric",
                "assert_reassuring_complaint_cannot_suppress_observed_sign": True},
      complexity=[2, 3],
      notes="Myocardial infarction can present with NO CHEST PAIN AT ALL, particularly "
            "in women and older adults [S6]. Self-report vs observation detector fires "
            "on 'feels fine' against diaphoresis. S-13."),

    S("S-14", True, "79 M, long-term catheter, 'confused since this morning'",
      {"age_days": 79 * Y, "age_source": "document", "sex": "M",
       "arrival_mode": "walk_in"},
      "confused since this morning, urinary catheter",
      {"heart_rate": 92, "systolic_bp": 118, "diastolic_bp": 70,
       "respiratory_rate": 22, "spo2": 96, "temperature_c": 36.2,
       "consciousness_acvpu": "C", "ambulatory": True},
      history={"identity": "MATCHED", "identity_confidence": 0.98,
               "baseline_oriented": True},
      expected={"acted_level_range": [1, 2], "uncertainty_class_in": ["CLEAR", "THIN", "CONFLICTED"],
                "required_fired_rules_any": ["RF-A02", "RF-G02"],
                "required_envelope": "geriatric", "ttl_max_minutes": 15,
                "assert_normal_temperature_never_reassures_in_geriatric": True},
      complexity=[3, 4],
      notes="Sepsis from a urinary source often presents as ALTERED MENTAL STATUS in "
            "older adults, and fever is frequently ABSENT [S6]. Asserts that a normal "
            "temperature NEVER reassures in the geriatric envelope - the single most "
            "important geriatric rule in the system. S-14."),

    S("S-15", False, "34 F, first ED visit, no ID document, chest discomfort",
      {"age_days": 34 * Y, "age_source": "stated", "sex": "F",
       "arrival_mode": "walk_in", "pregnancy_status": "no"},
      "chest discomfort",
      {"heart_rate": 92, "systolic_bp": 118, "diastolic_bp": 76,
       "respiratory_rate": 18, "spo2": 98, "temperature_c": 36.9,
       "consciousness_acvpu": "A", "pain_score": 4, "ambulatory": True},
      history={"identity": "UNMATCHED"},
      expected={"acted_level_range": [2, 3], "uncertainty_class_in": ["CLEAR", "THIN", "CONFLICTED"],
                "queue_class_in": ["N"], "ttl_max_minutes": 30,
                "assert_zero_history_yields_valid_recommendation": True},
      complexity=[5, 6],
      notes="The demo's NORMAL REFERENCE CASE and the required zero-history "
            "demonstration. A valid, fully-featured recommendation with NO RECORD AT "
            "ALL - zero-history is a SUPPORTED PATH, not a degraded one. Deteriorates "
            "later as S-27. S-15."),

    S("S-16", False, "Adult M, age unknown, found unresponsive, brought by police",
      {"age_days": None, "age_source": "unknown", "sex": "M",
       "arrival_mode": "police"},
      "found unresponsive on the street",
      {"heart_rate": 108, "systolic_bp": 108, "diastolic_bp": 64,
       "respiratory_rate": 22, "spo2": 94, "temperature_c": 36.1,
       "consciousness_acvpu": "P", "blood_glucose_mgdl": 48},
      history={"identity": "UNMATCHED", "communication_barrier": True,
               "communication_barrier_kind": "consciousness",
               "self_report_channel_available": False},
      expected={"acted_level_range": [1, 1], "uncertainty_class_in": ["BLIND"],
                "required_fired_rules": ["RF-A01", "RF-A03", "RF-X02"],
                "required_envelope": "unknown_age", "ttl_max_minutes": 10,
                "assert_blind_and_hard_rule_compose": True},
      complexity=[3, 5, 6],
      notes="Hypoglycaemia red flag fires - a fully reversible cause of altered "
            "consciousness and one of the highest-value rules in the set. Unknown age "
            "applies the UNION of adjacent envelopes. Tests that BLIND and a hard rule "
            "COMPOSE CORRECTLY. S-16."),

    S("S-17", True, "Adult F, no shared language, no attendant, SpO2 probe failed",
      {"age_days": 41 * Y, "age_source": "estimated", "sex": "F",
       "arrival_mode": "walk_in", "pregnancy_status": "unknown"},
      "gestures toward her abdomen",
      {"heart_rate": 96, "systolic_bp": 124, "diastolic_bp": 78,
       "respiratory_rate": 20, "temperature_c": 37.1, "consciousness_acvpu": "A",
       "spo2": {"absent": "device_failed"}},
      history={"identity": "UNMATCHED", "communication_barrier": True,
               "communication_barrier_kind": "language",
               "self_report_channel_available": False},
      expected={"acted_level_max": 3, "uncertainty_class_in": ["BLIND"],
                "required_queue_class": "B", "ttl_max_minutes": 10,
                "required_fired_rules_any": ["RF-X01"],
                "assert_self_report_scored_absent_not_negative": True,
                "assert_escalated_by_class_not_score": True},
      complexity=[1, 2, 5],
      wow="WOW-3 override + audit",
      notes="THE OVERRIDE DEMO CASE. Self-report scored as ABSENT, not negative. Hard "
            "10-minute floor. Class B, above all normal patients. Reason: 'cannot "
            "assess - no self-report, SpO2 never measured'. Nurse then de-escalates "
            "with a reason, and the audit row appears live. S-17."),

    S("S-18", True, "41 M, sudden severe interscapular back pain while lifting",
      {"age_days": 41 * Y, "age_source": "document", "sex": "M",
       "arrival_mode": "walk_in"},
      "sudden severe back pain between the shoulder blades while lifting",
      {"heart_rate": 88, "systolic_bp": 168, "diastolic_bp": 94,
       "respiratory_rate": 18, "spo2": 98, "temperature_c": 36.8,
       "consciousness_acvpu": "A", "pain_score": 9, "ambulatory": True},
      history={"identity": "UNMATCHED"},
      expected={"acted_level_range": [2, 2], "prediction_set_min_width": 2,
                "uncertainty_class_in": ["THIN", "CONFLICTED"],
                "assert_complaint_pathway_fires_independently_of_vitals": True},
      complexity=[1],
      notes="Severe pain with UNREMARKABLE VITALS - the pattern that catches teams who "
            "treat normal observations as EXCLUSION. Complaint pathway (PW-AORTIC) "
            "fires independently of physiology. S-18."),

    S("S-19", True, "55 F, dizziness on standing; record lists a beta-blocker she denies",
      {"age_days": 55 * Y, "age_source": "document", "sex": "F",
       "arrival_mode": "walk_in", "pregnancy_status": "no"},
      "dizziness on standing",
      {"heart_rate": 58, "systolic_bp": 108, "diastolic_bp": 62,
       "respiratory_rate": 16, "spo2": 97, "temperature_c": 36.7,
       "consciousness_acvpu": "A", "pain_score": 0, "ambulatory": True,
       "patient_denies_medication": True},
      history={"identity": "PROVISIONAL", "identity_confidence": 0.62,
               "matched_fields": ["name", "age"],
               "rate_control_medication": True},
      expected={"acted_level_range": [2, 3], "prediction_set_min_width": 2,
                "uncertainty_class_in": ["CONFLICTED"],
                "required_contradiction_detectors": ["D5_record_vs_present_state"],
                "assert_provisional_record_cannot_reassure": True},
      complexity=[6],
      notes="Record vs present-state contradiction fires. Under a PROVISIONAL match, "
            "record data may RAISE risk and may NEVER reassure (rule P3). Tests that "
            "the bradycardia is NOT EXPLAINED AWAY by a medication that may belong to "
            "someone else. S-19."),

    S("S-20", True, "30 M, 'panic attack, I get these' - documented anxiety history",
      {"age_days": 30 * Y, "age_source": "document", "sex": "M",
       "arrival_mode": "walk_in"},
      "panic attack, I get these",
      {"heart_rate": 128, "systolic_bp": 128, "diastolic_bp": 80,
       "respiratory_rate": 28, "spo2": 94, "temperature_c": 37.2,
       "consciousness_acvpu": "A", "pain_score": 2, "ambulatory": True},
      history={"identity": "MATCHED", "identity_confidence": 0.95,
               "known_conditions": ["anxiety"]},
      expected={"acted_level_range": [2, 2], "uncertainty_class_in": ["CLEAR", "THIN", "CONFLICTED"],
                "required_fired_rules_any": ["RF-A05", "RF-A09", "RF-A13"],
                "assert_psychiatric_history_cannot_lower_acted_level": True},
      complexity=[2],
      notes="THE ANCHORING TRAP. A documented psychiatric history is the most common "
            "route to attributing physical deterioration to anxiety. Physiology fires "
            "regardless; the psychiatric history is explicitly BARRED from lowering the "
            "acted level. Blueprint section 5 item 16. S-20."),

    S("S-21", True, "62 M, right-arm weakness and slurred speech, onset 40 min ago",
      {"age_days": 62 * Y, "age_source": "document", "sex": "M",
       "arrival_mode": "ambulance"},
      "right arm weakness and slurred speech, onset 40 minutes ago",
      {"heart_rate": 84, "systolic_bp": 178, "diastolic_bp": 96,
       "respiratory_rate": 16, "spo2": 98, "temperature_c": 36.8,
       "consciousness_acvpu": "A", "gcs": 15, "stated_onset_time_min": -40,
       "ambulatory": True},
      history={"identity": "MATCHED", "identity_confidence": 0.96,
               "known_conditions": ["atrial_fibrillation"]},
      expected={"acted_level_range": [1, 2], "uncertainty_class_in": ["CLEAR", "THIN", "CONFLICTED"],
                "required_pathway_clocks": ["TCP-STROKE"],
                "assert_news2_near_zero_but_level_acute": True,
                "assert_pathway_clock_outside_optimiser_authority": True},
      complexity=[6, 11],
      notes="THE CASE THAT PROVES WHY A PHYSIOLOGICAL SCORE ALONE IS INSUFFICIENT: "
            "NEWS2 here is close to zero [S1]. The complaint pathway fires the "
            "time-critical stroke clock against the thrombolysis window [S13], "
            "displayed separately from the TTL and OUTSIDE the optimiser's authority. "
            "S-21."),

    S("S-22", True, "Two patients, same common name, ages 46 and 49, 12 min apart",
      {"age_days": 46 * Y, "age_source": "stated", "sex": "M",
       "arrival_mode": "walk_in"},
      "abdominal pain",
      {"heart_rate": 94, "systolic_bp": 132, "diastolic_bp": 80,
       "respiratory_rate": 18, "spo2": 97, "temperature_c": 37.0,
       "consciousness_acvpu": "A", "pain_score": 5, "ambulatory": True},
      history={"identity": "PROVISIONAL", "identity_confidence": 0.58,
               "matched_fields": ["name"],
               "candidate_record_ids": ["R-DUP-A", "R-DUP-B"]},
      expected={"uncertainty_class_in": ["CONFLICTED", "THIN", "BLIND"],
                "required_contradiction_detectors": ["D5_record_vs_present_state"],
                "assert_no_automatic_merge": True,
                "assert_both_surfaced_for_confirmation": True,
                "assert_provisional_match_never_lowers_acted_acuity": True},
      complexity=[6],
      paired_arrival={
          "suffix": "b",
          "arrival_offset_min": 12.0,
          "demographics": {"age_days": 49 * Y, "age_source": "stated", "sex": "M",
                           "arrival_mode": "walk_in"},
          "stated_chief_complaint": "abdominal pain",
          "observations": {"heart_rate": 90, "systolic_bp": 128, "diastolic_bp": 78,
                           "respiratory_rate": 18, "spo2": 98, "temperature_c": 36.9,
                           "consciousness_acvpu": "A", "pain_score": 4,
                           "ambulatory": True},
          "history": {"identity": "PROVISIONAL", "identity_confidence": 0.57,
                      "matched_fields": ["name"],
                      "candidate_record_ids": ["R-DUP-A", "R-DUP-B"]},
          "note": "Same common name, adjacent age, arriving 12 minutes later. Both "
                  "map to the same record with medium confidence.",
      },
      notes="Tests the MOST UNDER-MODELLED FAILURE IN INDIAN REGISTRATION. Neither "
            "record is used for reassurance; both patients are surfaced for identity "
            "confirmation; NO AUTOMATIC MERGE. S-22 is ONE scenario containing TWO "
            "arrivals - the collision is the scenario. S-22."),

    S("S-23", True, "50 F, alert, ambulatory, chest wall pain - BP cuff fault reads 0/0",
      {"age_days": 50 * Y, "age_source": "document", "sex": "F",
       "arrival_mode": "walk_in", "pregnancy_status": "no"},
      "chest wall pain, worse on movement",
      {"heart_rate": 96, "systolic_bp": 0, "diastolic_bp": 0,
       "respiratory_rate": 18, "spo2": 97, "temperature_c": 36.8,
       "consciousness_acvpu": "A", "pain_score": 4, "ambulatory": True},
      history={"identity": "UNMATCHED"},
      expected={"acted_level_range": [2, 3], "uncertainty_class_in": ["THIN", "CONFLICTED"],
                "assert_impossible_value_quarantined_not_deleted": True,
                "assert_no_false_emergency_from_artefact": True,
                "required_open_task_kinds": ["re_measure"],
                "forbidden_fired_rules": ["RF-A07"]},
      complexity=[4],
      notes="IMPOSSIBLE VALUE QUARANTINED, not deleted and not believed. Field becomes "
            "'unreliable - re-measure', a task opens, the clock shortens. Asserts BOTH "
            "failure modes are avoided: no false emergency, no silent drop. S-23."),

    S("S-24", True, "63 M, severe work of breathing, tripod position, SpO2 reads 99%",
      {"age_days": 63 * Y, "age_source": "document", "sex": "M",
       "arrival_mode": "ambulance"},
      "severe breathing difficulty",
      {"heart_rate": 118, "systolic_bp": 134, "diastolic_bp": 82,
       "respiratory_rate": 30, "spo2": 99, "temperature_c": 37.0,
       "consciousness_acvpu": "A", "work_of_breathing": "severe",
       "ambulatory": False},
      history={"identity": "MATCHED", "identity_confidence": 0.96,
               "known_conditions": ["copd"]},
      expected={"acted_level_range": [1, 1], "prediction_set_min_width": 1,
                "uncertainty_class_in": ["CONFLICTED"],
                "required_contradiction_detectors": ["D4_device_vs_clinical"],
                "required_fired_rules_any": ["RF-P07", "RF-A05"],
                "assert_no_averaging_of_contradictory_values": True},
      complexity=[3, 4],
      notes="DEVICE vs CLINICAL contradiction: a 99% saturation is inconsistent with "
            "severe respiratory distress. NO AVERAGING, NO MOST-RECENT-WINS. Both "
            "values are shown and a human is asked to verify. S-24."),

    S("S-25", False, "38 F, level 4 at triage, all observations normal - 42 min stale",
      {"age_days": 38 * Y, "age_source": "document", "sex": "F",
       "arrival_mode": "walk_in", "pregnancy_status": "no"},
      "sore throat",
      {"heart_rate": {"value": 78, "age_min": 42}, "systolic_bp": {"value": 120, "age_min": 42},
       "diastolic_bp": {"value": 76, "age_min": 42},
       "respiratory_rate": {"value": 16, "age_min": 42},
       "spo2": {"value": 98, "age_min": 42},
       "temperature_c": {"value": 37.1, "age_min": 42},
       "consciousness_acvpu": {"value": "A", "age_min": 42},
       "pain_score": {"value": 3, "age_min": 42}},
      history={"identity": "UNMATCHED"},
      expected={"uncertainty_class_in": ["THIN", "CONFLICTED", "BLIND"],
                "assert_t5_silence_trigger_fires": True,
                "assert_stale_values_decay_toward_unknown_not_normal": True,
                "required_trigger_classes": ["T5"]},
      complexity=[5],
      notes="T5 SILENCE TRIGGER. Values past 2x half-life become ABSENT; the reason "
            "line reads 'no new data in 42 min'. ABSENCE OF ACTIVITY IS ITSELF THE "
            "EVENT. S-25."),

    S("S-26", True, "27 M, arrived by ambulance, now refuses assessment",
      {"age_days": 27 * Y, "age_source": "stated", "sex": "M",
       "arrival_mode": "ambulance"},
      "declines to say",
      {"heart_rate": {"absent": "refused"}, "systolic_bp": {"absent": "refused"},
       "respiratory_rate": {"absent": "refused"}, "spo2": {"absent": "refused"},
       "temperature_c": {"absent": "refused"},
       "consciousness_acvpu": {"value": "A", "provenance": "Obs"},
       "ambulatory": True},
      history={"identity": "UNMATCHED"},
      expected={"uncertainty_class_in": ["THIN", "CONFLICTED", "BLIND"],
                "required_contradiction_detectors": ["D2_arrival_mode_vs_acuity"],
                "assert_refusal_does_not_become_invisibility": True,
                "assert_missing_reason_is_refused": True},
      complexity=[2, 5],
      notes="TWO THINGS AT ONCE: missing-by-refusal is a distinct reason with its own "
            "handling, AND arrival mode vs acuity fires because someone with more "
            "information decided he needed an ambulance. Tests that REFUSAL DOES NOT "
            "BECOME INVISIBILITY. S-26."),

    S("S-27", False, "S-15 at t+18 min: HR 92->118, SpO2 98->93, RR 18->22",
      {"age_days": 34 * Y, "age_source": "stated", "sex": "F",
       "arrival_mode": "walk_in", "pregnancy_status": "no"},
      "chest discomfort",
      {"heart_rate": 92, "systolic_bp": 118, "diastolic_bp": 76,
       "respiratory_rate": 18, "spo2": 98, "temperature_c": 36.9,
       "consciousness_acvpu": "A", "pain_score": 4, "ambulatory": True},
      history={"identity": "UNMATCHED"},
      injections=[{"at_offset_min": 18.0,
                   "observations": {"heart_rate": 118, "spo2": 93, "respiratory_rate": 22},
                   "provenance": "Dev"}],
      expected={"acted_level_after_injection_range": [2, 3],
                "assert_acted_level_escalates_after_injection": True,
                "assert_ttl_shortens_after_injection": True,
                "assert_two_recommendation_versions": True,
                "required_trigger_classes": ["T2"],
                "assert_delta_reason_string_present": True},
      complexity=[20, 22],
      wow="WOW-4 deterioration and re-ranking",
      notes="THE RE-RANKING DEMO. No absolute adult threshold is crossed by HR alone; "
            "the DELTA fires T2. Position 9 -> 1 within one refresh cycle, clock 30 min "
            "-> 8 min, reason 'HR +26, SpO2 -4 in 18 min', two recommendation versions "
            "in the audit. S-27."),

    S("S-28", True, "5 y, gastroenteritis - HR drifts 128->148->168 with BP maintained",
      {"age_days": 5 * Y, "age_source": "document", "sex": "M",
       "arrival_mode": "walk_in"},
      "vomiting and diarrhoea",
      {"heart_rate": 128, "systolic_bp": 96, "diastolic_bp": 60,
       "respiratory_rate": 24, "spo2": 98, "temperature_c": 37.6,
       "consciousness_acvpu": "A", "capillary_refill_seconds": 2.0,
       "work_of_breathing": "normal", "behavioural_state": "settled"},
      history={"identity": "UNMATCHED"},
      injections=[
          {"at_offset_min": 12.0, "observations": {"heart_rate": 148}, "provenance": "Dev"},
          {"at_offset_min": 25.0, "observations": {"heart_rate": 168}, "provenance": "Dev"},
          {"at_offset_min": 38.0, "observations": {"heart_rate": 172, "systolic_bp": 72},
           "provenance": "Dev"},
      ],
      latent={"deteriorates": True, "event_at_offset_min": 38.0,
              "event": "decompensated_shock",
              "required_intervention": ["fluid_resuscitation", "resus_bay"],
              "true_acuity": 1},
      expected={"assert_escalates_before_bp_falls": True,
                "escalation_must_occur_before_offset_min": 38.0,
                "required_envelope": "paediatric",
                "required_trigger_classes": ["T2"]},
      complexity=[3, 4, 20],
      notes="PAEDIATRIC COMPENSATED SHOCK - children compensate and then crash. The "
            "system MUST surface this BEFORE the blood pressure falls, on TREND ALONE. "
            "If it only escalates at the BP drop, that is a DOCUMENTED FAILURE of the "
            "design and we would report it as one. S-28."),

    S("S-29", False, "70 M, level 4, waiting 55 min - relative reports new confusion",
      {"age_days": 70 * Y, "age_source": "document", "sex": "M",
       "arrival_mode": "walk_in"},
      "sore knee",
      {"heart_rate": 76, "systolic_bp": 136, "diastolic_bp": 80,
       "respiratory_rate": 16, "spo2": 97, "temperature_c": 36.8,
       "consciousness_acvpu": "A", "pain_score": 3, "ambulatory": True},
      history={"identity": "MATCHED", "identity_confidence": 0.97,
               "baseline_oriented": True},
      injections=[{"at_offset_min": 55.0,
                   "observations": {"relative_reports_change": "relative reports he has become confused",
                                    "reported_new_confusion": True},
                   "provenance": "Att"}],
      expected={"assert_acted_level_escalates_after_injection": True,
                "required_trigger_classes": ["T3"],
                "assert_t3_is_escalate_only": True,
                "required_envelope": "geriatric"},
      complexity=[20],
      notes="T3 OBSERVATION TRIGGER from a family member, ESCALATE-ONLY [S11]. Tests "
            "the free-sensor channel that is almost always present in an Indian ED and "
            "almost never instrumented. S-29."),

    S("S-30", False, "45 M, level 3, waits past the 30-minute protocol interval",
      {"age_days": 45 * Y, "age_source": "document", "sex": "M",
       "arrival_mode": "walk_in"},
      "abdominal pain",
      {"heart_rate": 88, "systolic_bp": 128, "diastolic_bp": 78,
       "respiratory_rate": 18, "spo2": 98, "temperature_c": 37.0,
       "consciousness_acvpu": "A", "pain_score": 5, "ambulatory": True},
      history={"identity": "UNMATCHED"},
      injections=[{"at_offset_min": 34.0, "observations": {}, "advance_only": True}],
      expected={"assert_enters_queue_class_E": True,
                "assert_class_E_above_all_normal": True,
                "required_trigger_classes": ["T1"],
                "assert_charge_nurse_escalation_after_grace": True},
      complexity=[21],
      notes="T1 EXPIRY. Class E, above every normal patient. Charge-nurse escalation "
            "after the grace interval. Tests that NOTHING CHANGING IS STILL AN EVENT - "
            "the founding failure of the product. S-30."),

    S("S-31", False, "Surge injection: 18 arrivals in 12 minutes, incl. 3x L2 and 1x L1",
      {"age_days": 52 * Y, "age_source": "document", "sex": "F",
       "arrival_mode": "ambulance", "pregnancy_status": "no"},
      "chest pain",
      {"heart_rate": 104, "systolic_bp": 122, "diastolic_bp": 74,
       "respiratory_rate": 22, "spo2": 95, "temperature_c": 36.9,
       "consciousness_acvpu": "A", "pain_score": 7, "ambulatory": True},
      history={"identity": "UNMATCHED"},
      expected={"assert_surge_mode_recommended": True,
                "assert_objective_switches_to_minimax": True,
                "assert_discretionary_work_suspended": True,
                "assert_deficit_board_appears": True,
                "assert_worklist_still_capped_at_k": True},
      complexity=[12, 27],
      wow="WOW-5 Deficit Board",
      notes="SURGE BEAT. Mode change recommended to the charge nurse; objective "
            "switches from expected to MINIMAX WORST-CASE HARM; discretionary work "
            "suspends; Deficit Board appears with a quantified staffing gap; worklist "
            "STAYS CAPPED AT K. S-31."),

    S("S-32", True, "CT scanner offline while the stroke case is in the queue (district)",
      {"age_days": 62 * Y, "age_source": "document", "sex": "M",
       "arrival_mode": "ambulance"},
      "right arm weakness and slurred speech, onset 40 minutes ago",
      {"heart_rate": 84, "systolic_bp": 178, "diastolic_bp": 96,
       "respiratory_rate": 16, "spo2": 98, "temperature_c": 36.8,
       "consciousness_acvpu": "A", "gcs": 15, "stated_onset_time_min": -40,
       "ambulatory": True},
      history={"identity": "MATCHED", "identity_confidence": 0.96,
               "known_conditions": ["atrial_fibrillation"]},
      expected={"acted_level_range": [1, 2],
                "required_transfer_consideration_contains": "CT",
                "assert_priority_does_not_fall_when_resource_missing": True,
                "assert_pathway_clock_still_displayed": True,
                "profile": "H-S"},
      complexity=[11, 27],
      notes="CAPABILITY FILTER suppresses the internal imaging pathway and raises 'no "
            "CT on site - consider transfer decision now'. PRIORITY DOES NOT FALL "
            "BECAUSE THE RESOURCE IS MISSING (constraint C3). Tests the most dangerous "
            "available optimisation and asserts it is IMPOSSIBLE. S-32."),
]


def main() -> None:
    adversarial = sum(1 for s in SCENARIOS if s["adversarial"])
    doc = {
        "library_version": "1.0.0",
        "source": "PatientTriage.ai Round 2 Master Blueprint, section 17.3",
        "count": len(SCENARIOS),
        "adversarial_count": adversarial,
        "adversarial_fraction": round(adversarial / len(SCENARIOS), 3),
        "ground_truth_tier": "G2 - scenario expected behaviour",
        "note": (
            "Each scenario carries an EXPECTED-BEHAVIOUR ENVELOPE - a RANGE of "
            "acceptable outputs, not a single label - authored BEFORE the system "
            "output was evaluated. 'Honest about clinical reality: a 7-year-old with "
            "those numbers is defensibly a 3 or a 4.' Blueprint 18.1."
        ),
        "seven_failure_modes_the_adversarial_half_tests": [
            "1. a benign appearance suppressing an age-specific rule (S-06, S-07)",
            "2. a reassuring self-report suppressing an observed sign (S-13, S-20)",
            "3. a normal temperature or heart rate reassuring in a geriatric patient (S-11, S-14)",
            "4. a physiological score missing a complaint-driven time-critical condition (S-21)",
            "5. silently resolving a contradiction (S-19, S-24)",
            "6. mishandling impossible or absent data by trusting or deleting it (S-23, S-26)",
            "7. letting resource scarcity lower a priority (S-32)",
        ],
        "scenarios": SCENARIOS,
    }
    out = os.path.join("data", "scenarios", "scenarios.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with io.open(out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"wrote {out}: {len(SCENARIOS)} scenarios, {adversarial} adversarial "
          f"({adversarial / len(SCENARIOS):.0%})")


if __name__ == "__main__":
    main()
