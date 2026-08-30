"""
Version stamping - Blueprint 15.1 and 6.11.

"Engine version, rule-set version, envelope version and model version are stamped
on every recommendation, so a past decision can be reproduced exactly."

Reproducibility rate must be 100% (Blueprint 22.3).  Replaying a stored input
snapshot with its stamped versions must yield a bit-identical result.
"""

ENGINE_VERSION = "1.0.0"
RULE_SET_VERSION = "1.4.0"          # rules/red_flags/hard_rules.json
UNIVERSAL_CORE_VERSION = "1.3.0"    # rules/profiles/universal_core.json
MODEL_VERSION = "0.9.0"             # monotonic GBM deterioration estimator
CALIBRATION_ID = "cal-synthetic-v1"
SCENARIO_LIBRARY_VERSION = "1.0.0"
AUDIT_SCHEMA_VERSION = "1.0.0"

PRODUCT_NAME = "PatientTriage.ai"
PRODUCT_TAGLINE = "The Living Queue"
TEAM = "Team DataBrix"
JURISDICTION = "India - DPDP Act 2023 + DPDP Rules 2025"

# Blueprint 15.5 / 18.3.  These strings are used verbatim in the UI and in reports.
HONESTY_STATEMENTS = {
    "regulatory": (
        "DPDP-shaped, not DPDP-compliant. Compliance is an organisational fact "
        "established by a data protection officer, a DPIA, an independent audit, "
        "processor contracts, a notice, and a lawful basis for each purpose."
    ),
    "clinical": (
        "Decision-support prototype. Not for clinical use. Not clinically validated. "
        "Synthetic validation is a precondition for the real work, not a substitute for it."
    ),
    "data": (
        "100% synthetic data. No real patient record has ever entered this system. "
        "Disclosed on the interface, consistent with CDSCO's expectation that AI-enabled "
        "medical device software discloses whether datasets are real-world or synthetic."
    ),
    "accuracy": (
        "No accuracy, AUC, sensitivity or specificity is reported as a clinical result. "
        "A model trained on trajectories we wrote, predicting those trajectories, measures "
        "our own consistency, not medicine."
    ),
}


def version_stamp() -> dict:
    """The exact versions of every component that contributed.  Blueprint 15.1:
    'Reproducibility requires knowing what was running, not what is running now.'"""
    return {
        "engine_version": ENGINE_VERSION,
        "rule_version": RULE_SET_VERSION,
        "universal_core_version": UNIVERSAL_CORE_VERSION,
        "model_version": MODEL_VERSION,
        "calibration_id": CALIBRATION_ID,
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
    }
