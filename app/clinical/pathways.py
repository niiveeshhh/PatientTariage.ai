"""
Complaint concept mapping and candidate risk pathways.

Blueprint complexity 1: "The system never RESOLVES ambiguity; it REPRESENTS it.
Complaint maps to a SET of candidate risk pathways, each with its own red-flag rule
set; ANY pathway firing escalates."

Blueprint 9.7: the DETERMINISTIC synonym-and-keyword mapper is the DEFAULT PATH and
the PERMANENT FALLBACK.  There is no LLM in this module and none in the decision
path.  The optional gated LLM suggestion path lives in app/clinical/llm_optional.py,
is feature-flagged OFF, and is non-authoritative by construction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from app.core.knowledge import Knowledge
from app.core.models import PathwayClock, Patient


# ---------------------------------------------------------------------------
# Closed clinical vocabulary.  Blueprint complexity 7: "Reason strings are drawn
# from a closed, clinically-reviewed vocabulary - never free-form generation."
# ---------------------------------------------------------------------------

CONCEPT_SYNONYMS: Dict[str, List[str]] = {
    "chest_pain": [
        "chest pain", "chest discomfort", "chest tightness", "chest pressure",
        "crushing chest", "central chest", "chest heaviness", "angina",
    ],
    "indigestion_with_diaphoresis": [
        "indigestion", "heartburn", "reflux", "acid", "antacid",
    ],
    "breathlessness": [
        "short of breath", "breathless", "sob", "cannot breathe", "difficulty breathing",
        "wheeze", "asthma", "gasping", "dyspnoea", "dyspnea",
    ],
    "focal_weakness": [
        "weakness one side", "arm weakness", "leg weakness", "face droop",
        "facial droop", "hemiparesis", "cannot move arm", "cannot move leg",
        "right-arm weakness", "left-arm weakness", "right arm weakness",
        "left arm weakness",
    ],
    "slurred_speech": [
        "slurred speech", "cannot speak", "speech difficulty", "dysarthria",
        "word finding", "aphasia",
    ],
    "sudden_visual_loss": ["vision loss", "lost vision", "sudden blindness", "visual loss"],
    "headache": ["headache", "migraine", "head pain", "thunderclap"],
    "abdominal_pain": [
        "abdominal pain", "stomach pain", "belly pain", "tummy pain", "abdo pain",
        "guarding", "epigastric",
    ],
    "back_pain": ["back pain", "interscapular", "between the shoulder blades", "lumbar pain"],
    "fever_with_systemic_features": [
        "fever", "febrile", "high temperature", "hot", "rigors", "chills",
    ],
    "confusion": ["confused", "confusion", "disoriented", "not making sense", "delirium"],
    "altered_consciousness": [
        "unresponsive", "unconscious", "collapsed", "found down", "not waking",
        "drowsy", "found unresponsive",
    ],
    "generalised_weakness": [
        "weak", "not eating", "off legs", "off her legs", "off his legs", "tired",
        "lethargic", "not himself", "not herself", "unwell", "just not right",
        "generally unwell", "feeding less", "feeding a bit less",
    ],
    "dizziness": ["dizzy", "dizziness", "lightheaded", "faint", "syncope", "blackout"],
    "injury": [
        "fall", "fell", "injury", "hurt", "trauma", "fracture", "deformity",
        "sprain", "twisted", "ankle", "wrist", "collision", "accident",
    ],
    "bleeding": ["bleeding", "blood", "haemorrhage", "hemorrhage", "vomiting blood"],
    "vomiting": ["vomit", "vomiting", "throwing up", "nausea"],
    "diarrhoea": ["diarrhoea", "diarrhea", "loose stools", "gastroenteritis"],
    "palpitations": ["palpitations", "racing heart", "heart racing", "fluttering"],
    "anxiety_panic": ["panic", "panic attack", "anxiety", "anxious", "hyperventilating"],
    "rash": ["rash", "spots", "hives", "urticaria"],
    "urinary": ["urine", "urinary", "catheter", "burning when passing", "uti"],
    "photophobia": ["photophobia", "light hurts", "neck stiffness"],
    "obstetric": ["pregnant", "labour", "labor", "contractions", "bleeding in pregnancy"],
    "poisoning_overdose": ["overdose", "poisoning", "took tablets", "ingested"],
    "no_complaint_stated": [],
}

# Ambiguity: complaints that map to several very different risk pathways at once.
# Blueprint complexity 1: "'weakness' maps to everything."
AMBIGUOUS_CONCEPTS: Set[str] = {
    "generalised_weakness",
    "dizziness",
    "confusion",
    "indigestion_with_diaphoresis",
    "abdominal_pain",
    "back_pain",
    "anxiety_panic",
    "chest_pain",
}

# Each concept opens one or more CANDIDATE RISK PATHWAYS.  Any pathway firing
# escalates; the system never picks one and discards the rest.
CONCEPT_TO_PATHWAYS: Dict[str, List[str]] = {
    "chest_pain": ["TCP-STEMI", "PW-AORTIC", "PW-PE", "PW-BENIGN-MSK"],
    "indigestion_with_diaphoresis": ["TCP-STEMI", "PW-GI"],
    "breathlessness": ["PW-RESP-FAILURE", "PW-PE", "TCP-SEPSIS", "PW-ASTHMA"],
    "focal_weakness": ["TCP-STROKE"],
    "slurred_speech": ["TCP-STROKE"],
    "sudden_visual_loss": ["TCP-STROKE"],
    "headache": ["PW-SAH", "PW-MENINGITIS", "PW-BENIGN-HEADACHE"],
    "photophobia": ["PW-MENINGITIS", "PW-SAH", "PW-BENIGN-HEADACHE"],
    "abdominal_pain": ["PW-SURGICAL-ABDOMEN", "PW-AORTIC", "TCP-SEPSIS", "PW-GI"],
    "back_pain": ["PW-AORTIC", "PW-CAUDA-EQUINA", "PW-BENIGN-MSK"],
    "fever_with_systemic_features": ["TCP-SEPSIS", "PW-MENINGITIS", "PW-BENIGN-VIRAL"],
    "confusion": ["TCP-SEPSIS", "PW-METABOLIC", "TCP-STROKE", "PW-DELIRIUM"],
    "altered_consciousness": ["PW-METABOLIC", "TCP-STROKE", "TCP-SEPSIS", "PW-POISONING"],
    "generalised_weakness": ["TCP-SEPSIS", "PW-METABOLIC", "PW-CARDIAC-OTHER", "PW-DECONDITIONING"],
    "dizziness": ["PW-ARRHYTHMIA", "TCP-STROKE", "PW-METABOLIC", "PW-BENIGN-VESTIBULAR"],
    "injury": ["PW-TRAUMA", "PW-BENIGN-MSK"],
    "bleeding": ["PW-HAEMORRHAGE"],
    "vomiting": ["PW-GI", "PW-METABOLIC", "TCP-SEPSIS"],
    "diarrhoea": ["PW-GI", "TCP-SEPSIS"],
    "palpitations": ["PW-ARRHYTHMIA", "PW-ANXIETY"],
    "anxiety_panic": ["PW-ANXIETY", "PW-ARRHYTHMIA", "PW-RESP-FAILURE"],
    "rash": ["PW-MENINGITIS", "PW-ALLERGY", "PW-BENIGN-DERM"],
    "urinary": ["TCP-SEPSIS", "PW-URINARY"],
    "obstetric": ["PW-OBSTETRIC"],
    "poisoning_overdose": ["PW-POISONING", "PW-METABOLIC"],
    "no_complaint_stated": [],
}

# Pathways whose presence, on its own, floors the acted level regardless of vitals.
# Blueprint 9.6: "Severe pain, stable vitals ... complaint-driven pathways for
# time-critical causes fire INDEPENDENTLY of vitals."
HIGH_RISK_PATHWAYS: Dict[str, int] = {
    "TCP-STROKE": 2,
    "TCP-STEMI": 2,
    "TCP-SEPSIS": 2,
    "PW-AORTIC": 2,
    "PW-SAH": 2,
    "PW-MENINGITIS": 2,
    "PW-CAUDA-EQUINA": 3,
    "PW-HAEMORRHAGE": 2,
    "PW-RESP-FAILURE": 2,
    "PW-SURGICAL-ABDOMEN": 3,
    "PW-ARRHYTHMIA": 3,
    "PW-METABOLIC": 3,
    "PW-POISONING": 3,
    "PW-OBSTETRIC": 3,
}

BENIGN_PATHWAYS = {
    "PW-BENIGN-MSK", "PW-BENIGN-HEADACHE", "PW-BENIGN-VIRAL",
    "PW-BENIGN-VESTIBULAR", "PW-BENIGN-DERM", "PW-DECONDITIONING",
    "PW-ANXIETY", "PW-GI", "PW-ALLERGY", "PW-URINARY", "PW-DELIRIUM",
    "PW-TRAUMA", "PW-ASTHMA", "PW-PE", "PW-CARDIAC-OTHER",
}


@dataclass
class ComplaintMapping:
    concepts: List[str] = field(default_factory=list)
    pathways: List[str] = field(default_factory=list)
    ambiguous: bool = False
    unmapped: bool = False
    matched_phrases: Dict[str, str] = field(default_factory=dict)
    mapper: str = "deterministic_keyword_v1"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "concepts": sorted(self.concepts),
            "pathways": sorted(self.pathways),
            "ambiguous": self.ambiguous,
            "unmapped": self.unmapped,
            "matched_phrases": dict(sorted(self.matched_phrases.items())),
            "mapper": self.mapper,
        }


def _normalise(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def map_complaint(text: str) -> ComplaintMapping:
    """Deterministic synonym-and-keyword mapper.  Same input -> same output, every
    time, which is what makes the audit record reproducible (Blueprint 9.7)."""
    mapping = ComplaintMapping()
    norm = _normalise(text)

    if not norm:
        mapping.concepts = ["no_complaint_stated"]
        mapping.unmapped = True
        return mapping

    for concept, phrases in CONCEPT_SYNONYMS.items():
        for phrase in phrases:
            if phrase and phrase in norm:
                if concept not in mapping.concepts:
                    mapping.concepts.append(concept)
                    mapping.matched_phrases[concept] = phrase
                break

    if not mapping.concepts:
        # Blueprint complexity 1 fallback: "If the mapper cannot resolve a complaint
        # to any pathway, the complaint is treated as UNMAPPED -> uncertainty class
        # THIN -> clock floor, and the nurse is asked one question."
        mapping.unmapped = True
        mapping.concepts = []
        return mapping

    pathways: List[str] = []
    for c in mapping.concepts:
        for p in CONCEPT_TO_PATHWAYS.get(c, []):
            if p not in pathways:
                pathways.append(p)
    mapping.pathways = pathways

    # Ambiguity is a FIRST-CLASS FIELD on the recommendation object, not a derived
    # display value (Blueprint complexity 1).
    distinct_high_risk = {p for p in pathways if p in HIGH_RISK_PATHWAYS}
    mapping.ambiguous = (
        any(c in AMBIGUOUS_CONCEPTS for c in mapping.concepts)
        or (len(distinct_high_risk) >= 2)
    )
    mapping.concepts.sort()
    mapping.pathways.sort()
    return mapping


def pathway_floor(pathways: List[str]) -> Optional[int]:
    """The most acute floor demanded by any active candidate pathway.  ANY pathway
    firing escalates (Blueprint complexity 1)."""
    floors = [HIGH_RISK_PATHWAYS[p] for p in pathways if p in HIGH_RISK_PATHWAYS]
    return min(floors) if floors else None


# ---------------------------------------------------------------------------
# Time-critical pathway clocks - Blueprint section 5 item 6
# ---------------------------------------------------------------------------

def open_pathway_clocks(patient: Patient, mapping: ComplaintMapping, kb: Knowledge,
                        now_min: float, profile: Dict[str, Any]) -> List[PathwayClock]:
    """Time-critical pathways are a SEPARATE OBJECT with their own countdown,
    displayed independently of the TTL and OUTSIDE the optimiser's authority.

    The clock is the patient's, not the hospital's: it is flagged with its window
    REGARDLESS of local capability (Blueprint complexity 11).
    """
    clocks: List[PathwayClock] = []
    capabilities = _capability_set(profile)

    for spec in kb.red_flags.get("time_critical_pathways", []):
        pid = spec["pathway_id"]
        if pid not in mapping.pathways:
            continue
        if any(existing.pathway_id == pid for existing in patient.pathway_clocks):
            continue

        onset = patient.value("stated_onset_time_min")
        origin_known = onset is not None
        if spec.get("clock_runs_from") == "arrival_timestamp":
            origin = patient.arrival_timestamp_min
            origin_known = True
        else:
            origin = onset if origin_known else patient.arrival_timestamp_min

        required = list(spec.get("required_capabilities", []))
        available = all(c in capabilities for c in required)

        clocks.append(PathwayClock(
            pathway_id=pid,
            name=spec["name"],
            opened_at_min=now_min,
            window_minutes=float(spec["window_minutes"]),
            clock_origin_min=float(origin),
            origin_is_known=origin_known,
            required_capabilities=required,
            capability_available=available,
            transfer_consideration=not available,
            source=spec.get("source", ""),
        ))
    return clocks


def _capability_set(profile: Dict[str, Any]) -> Set[str]:
    """Blueprint complexity 11 fallback: 'Unknown capability -> treat as
    unavailable and surface the transfer flag. Assume less, not more.'"""
    inv = profile.get("capability_inventory", {})
    caps: Set[str] = set()
    imaging = inv.get("imaging", {})
    for modality, availability in imaging.items():
        if availability and availability != "none":
            caps.add(modality)
    for spec in inv.get("specialties_24x7", []):
        caps.add(spec)
    lab = inv.get("lab_turnaround_minutes_routine")
    if lab is not None and lab <= 120:
        caps.add("labs")
    if "cardiology" in caps:
        caps.add("cath_lab")
    # Runtime resource outages (constraint C3 demo: "CT offline") remove capability.
    for offline in profile.get("_offline_resources", []):
        caps.discard(offline)
        if offline == "cardiology":
            caps.discard("cath_lab")
    return caps


def routing_suggestion(acted_level: int, mapping: ComplaintMapping,
                       profile: Dict[str, Any], envelope_id: str,
                       obstetric: bool) -> Dict[str, Optional[str]]:
    """Blueprint complexity 11: routing recommendations are FILTERED against the
    capability inventory BEFORE DISPLAY; an unavailable pathway becomes a
    TRANSFER-CONSIDERATION FLAG, not a silent omission.

    Blueprint 9.3: routing is RECOMMEND-ONLY.  The system proposes a destination
    that EXISTS and a human sends the patient there.
    """
    destinations = list(profile.get("routing_destinations", []))
    caps = _capability_set(profile)

    def first_available(*candidates: str) -> Optional[str]:
        for c in candidates:
            if c in destinations:
                return c
        return None

    suggestion: Optional[str] = None
    blocked: Optional[str] = None
    transfer: Optional[str] = None

    if acted_level <= 2:
        suggestion = first_available("resus", "resus_bay", "majors", "general_area")
    elif obstetric:
        suggestion = first_available("obstetric", "majors", "general_area")
        if suggestion is None or "obstetrics" not in caps:
            blocked = "no obstetric cover on site"
            transfer = "No obstetric cover on site - consider transfer decision now"
            suggestion = first_available("majors", "general_area", "resus_bay")
    elif envelope_id == "paediatric":
        suggestion = first_available("paediatrics", "majors", "general_area")
        if "paediatrics" not in caps:
            blocked = "no paediatric cover on site"
            transfer = "No paediatrician on site - consider transfer decision now"
            suggestion = first_available("majors", "general_area", "resus_bay")
    elif acted_level == 3:
        suggestion = first_available("majors", "general_area")
    else:
        suggestion = first_available("fast_track", "minors", "general_area")

    # Time-critical pathway capability gate, regardless of level.
    if "TCP-STROKE" in mapping.pathways and "ct" not in caps:
        transfer = "No CT on site - consider transfer decision now"
        blocked = blocked or "stroke pathway needs CT, unavailable here"
    if "TCP-STEMI" in mapping.pathways and "cath_lab" not in caps:
        transfer = transfer or "No cath lab on site - consider transfer decision now"

    return {
        "suggestion": suggestion,
        "blocked_reason": blocked,
        "transfer_consideration": transfer,
    }
