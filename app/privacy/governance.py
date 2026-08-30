"""
Privacy / DPDP architecture - Blueprint 15.3, 6.6.

Jurisdiction: India, DPDP Act 2023 + DPDP Rules 2025.

THE HONESTY STATEMENT (Blueprint 15.5), which the UI displays verbatim:
"The prototype is DPDP-SHAPED, NOT DPDP-COMPLIANT.  Compliance is an organisational
fact established by a data protection officer, a DPIA, an independent audit,
contracts with processors, a notice, and a lawful basis for each purpose."

Implemented here: purpose-bound processing, role-based field-level minimisation,
access logging, pseudonymous identifiers, retention timers, the erasure/export
paths, the breach-response pathway, the DPIA stub, and the Fourth Schedule
paediatric position.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set


class Role(str, Enum):
    TRIAGE_NURSE = "triage_nurse"
    CHARGE_NURSE = "charge_nurse"
    PHYSICIAN = "physician"
    AUDITOR = "auditor"
    DPO = "data_protection_officer"


class Purpose(str, Enum):
    """Every read declares a purpose.  Non-care purposes FAIL CLOSED."""
    CARE = "care"
    SAFETY_INVESTIGATION = "safety_investigation"
    REGULATORY_AUDIT = "regulatory_audit"
    # Below this line: blocked by default and logged (Blueprint complexity 26).
    BILLING = "billing"
    RESEARCH = "research"
    STAFF_PERFORMANCE = "staff_performance"
    INSURANCE = "insurance"
    MODEL_TRAINING = "model_training"
    MARKETING = "marketing"


CARE_PURPOSES = {Purpose.CARE}
GOVERNANCE_PURPOSES = {Purpose.SAFETY_INVESTIGATION, Purpose.REGULATORY_AUDIT}

# Blueprint complexity 26: "The realistic threats are not hackers but ordinary
# INSTITUTIONAL DRIFT - using triage data for staff performance ranking, for
# insurance decisions, for model training without a basis, or for research without
# consent."  Three explicit refusals, enforced in code rather than in policy.
BLOCKED_PURPOSES = {
    Purpose.BILLING, Purpose.RESEARCH, Purpose.STAFF_PERFORMANCE,
    Purpose.INSURANCE, Purpose.MODEL_TRAINING, Purpose.MARKETING,
}

# Field-level minimisation per role.  "A triage nurse does not see billing
# identifiers."  Blueprint 6.6.
CLINICAL_FIELDS = {
    "patient_ref", "chair", "age_days", "sex", "arrival_mode", "arrival_timestamp_min",
    "stated_chief_complaint", "complaint_concepts", "observations", "acted_level",
    "prediction_set", "uncertainty_class", "dominant_reason", "secondary_reasons",
    "ttl_minutes", "fired_rules", "envelope_id", "action_verb", "routing_suggestion",
    "pathway_clocks", "open_tasks", "communication_barrier", "pregnancy_status",
}
IDENTITY_FIELDS = {"display_name", "record_id", "identity_confidence", "matched_fields"}
HISTORY_FIELDS = {"known_conditions", "baseline_systolic_bp", "prior_ed_visits_90d",
                  "rate_control_medication", "frailty_indicator"}
AUDIT_FIELDS = {"audit_records", "hash", "prev_hash", "input_snapshot", "versions",
                "clinician_decision", "override"}
GOVERNANCE_FIELDS = {"access_log", "retention", "dpia", "breach_log"}

ROLE_FIELD_SCOPE: Dict[Role, Set[str]] = {
    Role.TRIAGE_NURSE: CLINICAL_FIELDS | HISTORY_FIELDS,
    Role.CHARGE_NURSE: CLINICAL_FIELDS | HISTORY_FIELDS | {"queue_state", "deficit", "mode"},
    Role.PHYSICIAN: CLINICAL_FIELDS | HISTORY_FIELDS | IDENTITY_FIELDS,
    # The auditor sees the RECORD, not the live clinical picture.
    Role.AUDITOR: AUDIT_FIELDS | {"patient_ref", "acted_level", "uncertainty_class",
                                  "envelope_id", "ttl_minutes", "fired_rules"},
    Role.DPO: GOVERNANCE_FIELDS | {"patient_ref", "retention_days_remaining"},
}

ROLE_PURPOSES: Dict[Role, Set[Purpose]] = {
    Role.TRIAGE_NURSE: {Purpose.CARE},
    Role.CHARGE_NURSE: {Purpose.CARE},
    Role.PHYSICIAN: {Purpose.CARE},
    Role.AUDITOR: {Purpose.SAFETY_INVESTIGATION, Purpose.REGULATORY_AUDIT},
    Role.DPO: {Purpose.REGULATORY_AUDIT},
}


@dataclass
class AccessDecision:
    granted: bool
    role: Role
    purpose: Purpose
    visible_fields: List[str] = field(default_factory=list)
    denied_fields: List[str] = field(default_factory=list)
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "granted": self.granted, "role": self.role.value,
            "purpose": self.purpose.value,
            "visible_fields": sorted(self.visible_fields),
            "denied_fields": sorted(self.denied_fields),
            "reason": self.reason,
        }


def check_access(role: Optional[Role], purpose: Optional[Purpose],
                 requested_fields: Optional[Set[str]] = None) -> AccessDecision:
    """Purpose-bound processing.  Blueprint 6.6 fallback: "FAIL CLOSED: unknown role
    or purpose grants the LEAST-PRIVILEGED view and logs the failure." """
    if role is None or purpose is None:
        return AccessDecision(
            granted=False, role=Role.TRIAGE_NURSE,
            purpose=purpose or Purpose.CARE,
            visible_fields=[], denied_fields=sorted(requested_fields or []),
            reason="Role or purpose could not be established - failing closed to the "
                   "least-privileged view and logging the failure.",
        )

    if purpose in BLOCKED_PURPOSES:
        return AccessDecision(
            granted=False, role=role, purpose=purpose,
            denied_fields=sorted(requested_fields or ROLE_FIELD_SCOPE.get(role, set())),
            reason=(f"Purpose '{purpose.value}' is a SECONDARY USE and is blocked by "
                    f"default. No secondary use, no training on operational data, and "
                    f"no learning from overrides - three explicit refusals "
                    f"(Blueprint 6.6). This attempt has been logged."),
        )

    if purpose not in ROLE_PURPOSES.get(role, set()):
        return AccessDecision(
            granted=False, role=role, purpose=purpose,
            denied_fields=sorted(requested_fields or set()),
            reason=(f"Role '{role.value}' is not authorised for purpose "
                    f"'{purpose.value}'. Cross-purpose access blocked and logged."),
        )

    scope = ROLE_FIELD_SCOPE.get(role, set())
    requested = requested_fields if requested_fields is not None else scope
    visible = sorted(requested & scope)
    denied = sorted(requested - scope)
    return AccessDecision(granted=True, role=role, purpose=purpose,
                          visible_fields=visible, denied_fields=denied,
                          reason=None if not denied else
                          "Field-level minimisation: some requested fields are "
                          "outside this role's scope.")


def project_patient(payload: Dict[str, Any], role: Role) -> Dict[str, Any]:
    """Return only the fields this role may see.  The board itself shows a
    PSEUDONYMOUS identifier; full identity only where clinically required."""
    scope = ROLE_FIELD_SCOPE.get(role, set())
    out = {k: v for k, v in payload.items() if k in scope}
    if "display_name" not in scope:
        out.pop("display_name", None)
    return out


# ---------------------------------------------------------------------------
# Retention - DPDP Rule 6 [S7]
# ---------------------------------------------------------------------------

@dataclass
class RetentionPolicy:
    record_class: str
    retention_days: int
    basis: str
    may_be_lowered: bool = False

    def days_remaining(self, age_days: float) -> float:
        return max(0.0, self.retention_days - age_days)


RETENTION_POLICIES = [
    RetentionPolicy("audit_log", 365,
                    "DPDP Rule 6: logs and personal data retained for one year "
                    "unless another law requires otherwise [S7]. Configurable FLOOR - "
                    "may only be RAISED."),
    RetentionPolicy("access_log", 365, "DPDP Rule 6 [S7]."),
    RetentionPolicy("clinical_record", 1095,
                    "Clinical-record retention obligations may EXCEED the DPDP floor; "
                    "reconciled in production."),
    RetentionPolicy("operational_telemetry", 90,
                    "Minimisation: telemetry that serves no named audience is "
                    "surveillance (Blueprint 15.1)."),
]


# ---------------------------------------------------------------------------
# Breach pathway - DPDP Rule 7 [S7]
# ---------------------------------------------------------------------------

BREACH_RUNBOOK = {
    "trigger": "On becoming aware of a personal data breach.",
    "immediate": [
        "Intimate every affected Data Principal without delay, describing the "
        "breach, its likely consequences, and the mitigation measures taken.",
        "Intimate the Data Protection Board without delay.",
    ],
    "within_72_hours": [
        "Submit a DETAILED REPORT to the Board: broad facts, circumstances and "
        "reasons; mitigation measures; findings on the person who caused it; "
        "remedial measures; and a copy of the intimations sent to Data Principals.",
    ],
    "forensic_artefact": (
        "The separate hash-chained ACCESS LOG is the artefact that makes scope "
        "determination possible inside the 72-hour clock."
    ),
    "source": "DPDP Rules 2025, Rule 7 [S7]",
    "prototype_status": "PATHWAY DOCUMENTED AND STUBBED. Not exercised - a breach "
                        "runbook is an organisational process, not a code path.",
}


# ---------------------------------------------------------------------------
# Fourth Schedule - the paediatric emergency position
# ---------------------------------------------------------------------------

PAEDIATRIC_CONSENT_POSITION = {
    "rule": "DPDP Rules 2025, Rule 10 and Fourth Schedule Part A [S8]",
    "general_requirement": (
        "Verifiable parental consent is required before processing a child's "
        "personal data."
    ),
    "exemption_relied_on": (
        "Part A of the Fourth Schedule EXEMPTS clinical establishments, mental-health "
        "establishments, healthcare professionals and allied healthcare professionals, "
        "where processing is RESTRICTED TO PROVIDING HEALTH SERVICES to the child and "
        "LIMITED TO WHAT IS NECESSARY to protect her health."
    ),
    "our_position": (
        "A paediatric emergency cannot wait for verifiable parental consent, and "
        "Indian law does not require it to. We rely on the exemption, we document its "
        "SCOPE LIMITATION, and we do NOT invent a consent flow that would delay care."
    ),
    "scope_limitation": (
        "The exemption covers processing NECESSARY TO PROTECT THE CHILD'S HEALTH. It "
        "does not cover secondary use, research, or retention beyond the care purpose - "
        "all of which remain blocked by purpose binding."
    ),
}


# ---------------------------------------------------------------------------
# DPIA stub - Rule 13 [S8]
# ---------------------------------------------------------------------------

DPIA_STUB = {
    "status": "STUB. A DPIA is an organisational artefact requiring a named DPO, "
              "stakeholder consultation and independent audit. This is the SHAPE of "
              "one, not a completed one.",
    "obligation": "DPDP Rules 2025 Rule 13(1)-(2): annual DPIA and independent audit "
                  "for a Significant Data Fiduciary, with a report of significant "
                  "observations to the Board [S8].",
    "processing_purpose": "Emergency-department triage decision support: scheduling "
                          "clinical attention and maintaining a reassessment watchlist.",
    "lawful_basis_assumed": "Provision of health services in an emergency.",
    "data_categories": [
        "Demographics (age, sex) - age is clinically necessary as an ENVELOPE SELECTOR",
        "Chief complaint (free text, original language preserved)",
        "Triage vital signs and structured observations",
        "Record-derived baseline where an identity match exists",
    ],
    "explicitly_not_collected": [
        "Laboratory results", "Imaging", "Clinical notes", "Free-text history",
        "Billing identifiers", "Biometrics", "Location traces",
    ],
    "minimisation_argument": (
        "The arrival logic genuinely needs no labs, imaging, notes or free-text "
        "history. The smallest viable data footprint is simultaneously the best "
        "clinical design and the smallest breach surface (Blueprint 6.6)."
    ),
    "algorithmic_due_diligence": {
        "obligation": "Rule 13(3): due diligence to verify that algorithmic software "
                      "is not likely to pose a risk to the rights of Data Principals [S8].",
        "artefact": "The standing matched-pair fairness audit (evaluation/fairness). "
                    "An algorithmic-fairness audit is not a nice-to-have under Indian "
                    "law; it is an enumerated obligation.",
        "measures": [
            "The ranking function reads NO protected attribute (constraint C6).",
            "Age enters ONLY as an envelope selector - clinical necessity, not profiling.",
            "Sex enters ONLY to gate the conditional pregnancy field.",
            "The model does NOT learn from clinician overrides, because human triage "
            "judgement carries documented demographic disparity [S2].",
            "Matched-pair divergence is measured and must be zero.",
        ],
        "limitation": "Population bias is NOT measurable on synthetic data and is "
                      "reported as NOT MEASURED, never as passed.",
    },
    "residual_risks": [
        "Automation bias in the clinician user (measured by the automation-bias probe).",
        "Distribution shift between calibration and deployment populations "
        "(monitored by conformal set-width drift).",
        "Institutional drift toward secondary use (resisted by purpose binding).",
    ],
    "localisation": "Rule 13(4): deployment architecture keeps processing in-country "
                    "by default. No external inference call exists in the decision path.",
}


HONESTY_STATEMENT = (
    "DPDP-SHAPED, NOT DPDP-COMPLIANT. This prototype implements controls that map "
    "onto specific rules - Rule 6 safeguards and retention, Rule 7's breach path, "
    "Rule 13's fairness audit, purpose-bound access, minimisation - but compliance is "
    "an ORGANISATIONAL FACT established by a data protection officer, a data "
    "protection impact assessment, an independent audit, contracts with processors, a "
    "notice, and a lawful basis for each purpose. Authentication and logging do not "
    "constitute compliance."
)

SYNTHETIC_PROVENANCE_BANNER = (
    "100% SYNTHETIC DATA. No real patient record has ever entered this system. "
    "Disclosed on the interface, consistent with CDSCO's expectation that AI-enabled "
    "medical device software discloses whether datasets are real-world or synthetic [S19]."
)

THREAT_MODEL = [
    {"rank": 1, "threat": "Ordinary institutional drift toward secondary use - staff "
                          "performance ranking, insurance, unconsented research",
     "mitigation": "Purpose binding enforced in code; three explicit refusals.",
     "addressed_by_prototype": True},
    {"rank": 2, "threat": "Over-broad internal access",
     "mitigation": "Least privilege, role-scoped field-level views, access logging.",
     "addressed_by_prototype": True},
    {"rank": 3, "threat": "Identity leakage through logs and exports",
     "mitigation": "Pseudonymous identifiers; leakage scan in the test suite.",
     "addressed_by_prototype": True},
    {"rank": 4, "threat": "Tampering with the record after an adverse event",
     "mitigation": "Hash-chained append-only audit; verification reports the first "
                   "broken index.",
     "addressed_by_prototype": True},
    {"rank": 5, "threat": "External compromise",
     "mitigation": "A production security programme, not a feature.",
     "addressed_by_prototype": False,
     "note": "The prototype makes NO CLAIM to resist this. Saying which threats we do "
             "not address is part of the design."},
]


def export_patient_data(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The DPDP export path.  Returns the data subject's own record."""
    return {
        "export_type": "data_principal_export",
        "basis": "DPDP Act 2023 - right to access information about personal data.",
        "note": "Synthetic prototype export. Field set equals what is held.",
        "data": payload,
    }


def erase_patient_data(store: Dict[str, Any], patient_ref: str) -> Dict[str, Any]:
    """The erasure path.  Erases the CLINICAL record; the AUDIT CHAIN is retained
    because erasing a chained record would break tamper-evidence and because Rule 6
    requires one-year log retention [S7].  Saying this out loud is the point."""
    existed = patient_ref in store
    store.pop(patient_ref, None)
    return {
        "erased": existed,
        "patient_ref": patient_ref,
        "clinical_record_erased": existed,
        "audit_chain_retained": True,
        "reason": (
            "The append-only audit chain is RETAINED. Erasing a chained record would "
            "break tamper-evidence, and DPDP Rule 6 requires one-year retention of "
            "logs [S7]. In production the reconciliation is: erase the identity "
            "mapping, retain the pseudonymous audit entry."
        ),
    }
