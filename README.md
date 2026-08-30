# PatientTriage.ai — The Living Queue

**"We did not build an AI that triages patients. We built a queue that refuses to
let anyone be forgotten — and that tells you, out loud, when it cannot keep that
promise."**

Team DataBrix · Accenture Innovation Challenge 2026 · Problem Track 2 —
PatientTriage.ai · Round 2 · Jurisdiction: India (DPDP Act 2023 + DPDP Rules 2025)

> **Status: partial implementation, stopped mid-build.** This README describes
> exactly what exists and what does not. See [STATUS.md](STATUS.md) for the
> component-by-component build log. Do not take any claim below at face value
> without checking the corresponding row in STATUS.md — the two documents are kept
> in sync deliberately.

---

## 1. What this is

A clinical decision-support **engine** for emergency-department triage that treats
every triage decision as perishable rather than final. The core idea, carried
through from the team's Round 1 concept:

- Every triage decision expires — it carries a live clock (TTL), not a permanent
  score.
- Unknown is not stable — missing or stale data escalates attention, never
  reassures.
- Attention only ratchets upward — every de-escalation is an explicit, audited
  human act.
- **The AI owns the clock and the watchlist. The nurse owns the patient.**

The system does not diagnose, treat, prescribe, discharge, or autonomously route
or de-escalate anyone. Its only autonomous powers are scheduling attention
(reassessment timing, watchlist ordering) — its worst failure mode by
construction is a wasted glance, never a missed one.

This repository implements the **eight-layer engine** (L0–L7) and the
**accountability layer** (L8) described in the team's Round 2 Master Blueprint. It
does **not** yet implement the API, the UI, the test suites, or the evaluation
harness — see [§9 What is missing](#9-what-is-missing-honestly) below.

---

## 2. Why "the Living Queue" is different

Most triage products optimise a *score*. The evidence says the score is mostly not
where triage fails: the largest study of ESI (5.3M encounters, 21 EDs) found
overall mistriage of 32.2%, but *undertriage* was only 3.3% — the nurse's first
call is largely conservative. What kills patients is a decision **frozen** while a
crowded waiting room keeps moving: ED occupancy above roughly 90% has been shown
to independently raise 10-day mortality odds for patients already labelled
non-critical.

So this product's job is not "produce a better number at minute zero." Its job is
managing the *expiry* of a decision — detecting when a patient's picture has
changed, or gone stale, or become genuinely unknowable, and turning that into
scheduled human attention before harm accumulates.

---

## 3. Architecture: the eight engine layers

Each layer is a separate, independently callable, independently testable module —
never merged into one opaque function.

| Layer | Module | Responsibility |
|---|---|---|
| **L0** | `app/clinical/layer0_integrity.py` | Physiological plausibility gate + identity resolution (MATCHED / PROVISIONAL / UNMATCHED). Impossible values are **quarantined, never deleted**. |
| **L1** | `app/clinical/layer1_envelope.py` | Age-appropriate clinical envelope selection. Age selects the **rule set**, not a coefficient — paediatric / adult / geriatric / pregnancy / unknown-age, each with its own thresholds and NEWS2 validity rules. |
| **L2** | `app/clinical/layer2_redflags.py` + `rules/red_flags/hard_rules.json` | Deterministic, guideline-cited hard safety rules. **No learned component may veto this layer.** Every rule traces to a citation in `rules/citations/sources.json` or is tagged `ASM` (our own assumption) — the knowledge loader refuses to start otherwise. |
| **L3** | `app/clinical/layer3_risk.py` | Envelope-appropriate physiological read (NEWS2 for adults; age-banded ESI-style thresholds for children; NEWS2 + atypical-presentation modifiers for geriatric patients). Geriatric modifiers **remove reassurance**, they never add points — inventing a numeric magnitude the literature doesn't publish would be fabrication. |
| **L4** | `app/uncertainty/` | Five orthogonal uncertainty components → four named classes (CLEAR / THIN / CONFLICTED / BLIND) → **split-conformal prediction set** → act on the most acute plausible level. |
| **L5** | `app/queue/ttl.py` | Dynamic TTL = min(protocol floor, risk-derived interval, uncertainty-derived interval, load-compressed interval), enforced through a **single guarded write path** that cannot lengthen a clock without a named human actor and a durable audit record. |
| **L6** | `app/queue/living_queue.py` | The Living Queue itself: lexicographic hard classes (Red-flag > Expired > Blind > Normal), a harm-rate score *within* each class, six safety constraints (C1–C6), capacity-derived worklist size K, and the Deficit Board for when demand exceeds staffing. |
| **L7** | `app/queue/triggers.py` | Five reassessment trigger classes: Time, Event (incl. envelope-normalised trend deltas), Observation (carer/staff concern, escalate-only), Queue/load, and Silence (absence of new data while a task is open is itself an event). |
| **L8** | `app/core/department.py`, `app/audit/records.py` | The human decision boundary: asymmetric-friction overrides (escalate = one tap; de-escalate = mandatory reason category), the nine-category override taxonomy, and the hash-chained append-only audit trail written **before** any recommendation is displayed. |

Supporting subsystems, all present:

- **Uncertainty & conformal escalation** (`app/uncertainty/conformal.py`) — split-conformal calibration producing a coverage-guaranteed prediction set; the acted level is always the most acute member.
- **Hash-chained audit** (`app/audit/chain.py`) — append-only, tamper-evident; verification recomputes SHA-256 and reports the first broken index.
- **Degradation ladder** (`app/safety/degradation.py`) — L0 (full) → L1 (no model) → L2 (no history) → L3 (no engine) → L4 (dark/protocol-snapshot-only). Every rung shortens clocks; none lengthens them.
- **Monotonic gradient-boosted model** (`app/models/monotonic_gbm.py`, `app/models/deterioration.py`) — a small, dependency-free, pure-Python GBM with hard monotonic constraints, whose only authority is to shorten clocks. The system runs, unweakened at the safety-rule level, with this model disabled.
- **Privacy / DPDP governance** (`app/privacy/governance.py`) — purpose-bound access, five roles with field-level minimisation, retention policy, breach runbook, DPIA stub, and the Fourth Schedule paediatric-consent position, all mapped to specific DPDP Rules.
- **FHIR R4 / ABDM adapter** (`app/adapters/fhir_r4.py`) — ingests and emits synthetic ABDM-shaped FHIR R4 bundles; the same internal `Patient` object results whether built natively or from a bundle.
- **Seeded simulator** (`app/simulation/`) — arrival generator matched to the blueprint's distributions, latent deterioration trajectories hidden from the engine, and a 32-scenario hand-authored library (17 adversarial, 53%).

Everything above is **pure Python, zero I/O, deterministic given (patient, profile,
clock, versions)** — no framework, no network call, no external dependency in the
decision path.

---

## 4. What is verified right now

These are not claims — they are things you can rerun:

- **All 30 core modules import cleanly.**
- **27 of 27 statically-evaluable scenarios in the 32-scenario library pass their
  expected-behaviour envelopes** (envelopes were written before evaluation; the
  remaining 5 scenarios need a queue/injection runner that was not built — see
  §9).
- **Conformal escalation is real, not decorative.** The geriatric scenario S-11
  produces point estimate level 4, prediction set {2, 3, 4}, and an **acted level
  of 2** — the system acts on the worst plausible reading, not the most likely
  one, with a measured Escalation Premium of 2.0 levels.
- **Tamper-evidence is real.** A clean audit chain verifies; mutating one stored
  record's payload is detected at exactly that record's index by recomputing the
  hash chain — nothing about the detection is hard-coded.
- **The age-band flip is real.** The same 3-year-old scores near-zero risk under
  an adult-calibrated read and fires two rules (an age-banded heart-rate threshold
  and the carer-concern trigger) under the correct paediatric envelope.
- **K is genuinely capacity-derived**, not a constant: it resolves to 3 / 2 / 1
  across the tertiary / secondary / district hospital profiles, matching the
  blueprint's shipped profile table.

See [STATUS.md](STATUS.md) for the full component table, including two
diagnosed-but-only-partially-fixed calibration defects in the arrival generator.

---

## 5. Installation

```bash
python -m venv .venv
```

Windows:

```bash
.venv\Scripts\activate
```

macOS/Linux:

```bash
source .venv/bin/activate
```

No third-party dependencies are required to run the engine, the simulator, or the
scenario library — everything under `app/` is pure Python (3.9+). A
`requirements.txt` has not yet been written; nothing currently imported requires
one.

---

## 6. How to run what exists

Build the scenario library (regenerates `data/scenarios/scenarios.json` from
`scripts/build_scenarios.py`):

```bash
python scripts/build_scenarios.py
```

Run every statically-evaluable scenario against its expected-behaviour envelope
and print a pass/fail count. Save the block below as `scripts/check_scenarios.py`
and run `python scripts/check_scenarios.py`:

```python
from app.core.engine import evaluate, make_context, commit_ttl
from app.simulation.scenarios import load_library, build_patient, check_envelope
from app.queue.living_queue import assign_hard_class

lib = load_library()
passed, failed = 0, []
for sc in lib["scenarios"]:
    if sc["scenario_id"] in ("S-27", "S-28", "S-29", "S-30", "S-31"):
        continue  # need the injection/queue runner - not built yet
    ctx = make_context(sc["expected_behaviour_envelope"].get("profile", "H-L"), now_min=0.0)
    patient = build_patient(sc, 0.0)
    ctx.now_min = patient.arrival_timestamp_min + 5.0
    rec, trace = evaluate(patient, ctx)
    commit_ttl(patient, rec, ctx)
    red_flag = any(r.floor_level <= 2 for r in rec.fired_rules)
    qclass = assign_hard_class(patient, rec, ctx.now_min, red_flag, False)
    result = check_envelope(sc, rec, trace, queue_class=qclass, patient=patient)
    if result.passed:
        passed += 1
    else:
        failed.append(result.scenario_id)
print(f"PASS {passed} / FAIL {len(failed)}  {failed}")
```

Verify the audit chain's tamper-evidence. Save as `scripts/check_audit.py` and run
`python scripts/check_audit.py`:

```python
from app.audit.chain import AuditChain

chain = AuditChain()
for i in range(5):
    chain.append({"event_type": "recommendation", "patient_ref": f"P{i}", "v": i})
print("clean chain valid:", chain.verify().valid)

chain.tamper_for_demo(2, "v", 999)
result = chain.verify()
print("after tampering record 2:", result.valid, "-> first broken index", result.first_broken_index)
```

Generate a synthetic cohort and admit it into a department. Save as
`scripts/check_cohort.py` and run `python scripts/check_cohort.py`:

```python
from app.core.department import new_department
from app.core.knowledge import load_knowledge
from app.simulation.generator import generate_cohort

kb = load_knowledge()
dept = new_department("H-L")
cohort = generate_cohort(kb.profile("H-L"), seed=20260825, horizon_min=1440.0)
print(cohort.summary())

for g in cohort.patients:
    dept.clock.set_to(g.patient.arrival_timestamp_min)
    dept.admit(g.patient)
dept.rebuild_queue()

print("K =", dept.queue.k, "worklist =", [e.patient_ref for e in dept.queue.worklist])
print("audit records:", len(dept.audit.records), "chain valid:", dept.audit.verify().valid)
```

**There is no `scripts/run_demo`, no API server, and no UI.** The scripts above
are the only way to exercise the system today, and none of them ship in this
repository yet — copy the snippets in if you want to run them.

---

## 7. Clinical grounding

Nothing in `rules/` is a threshold that "sounded right." Every numeric clinical
value traces to a cited source in `rules/citations/sources.json` (NEWS2 [RCP],
ESI v5 [ENA], national paediatric early warning practice [RCPCH/NHS], CTAS
reassessment intervals, Surviving Sepsis Campaign, AHA stroke guidelines, the
AIIMS three-tier triage protocol, the largest published ESI mistriage study) or is
explicitly tagged `ASM` as the team's own stated assumption. The knowledge loader
(`app/core/knowledge.py`) **refuses to load** a red-flag rule, fever rule, or age
band that has lost its citation — this is enforced at import time, not by
convention.

---

## 8. Jurisdiction and honesty statements

**Jurisdiction: India — DPDP Act 2023 + DPDP Rules 2025.**

- **DPDP-shaped, not DPDP-compliant.** Compliance is an organisational fact
  requiring a named data protection officer, a completed DPIA, an independent
  audit, processor contracts, a notice, and a lawful basis for each processing
  purpose. Nothing in this codebase constitutes that.
- **100% synthetic data.** No real patient record has ever entered this system.
- **No accuracy, AUC, sensitivity or specificity figure is reported anywhere**,
  and none should be trusted if found: a model trained on trajectories this team
  wrote, and evaluated against those same trajectories, measures the team's own
  consistency — not medicine.
- **Not clinically validated. Not for clinical use.** Passing scenarios against
  hand-authored expected-behaviour envelopes is evidence of internal consistency
  with cited guidance, not a clinical outcome study.
- The eight safety invariants (I1–I8) described in the design (TTL never
  lengthens without a named human act; missing data can only escalate; ML cannot
  veto a red flag; resource scarcity cannot lower priority; etc.) are **enforced
  in code** — the TTL module physically has no automatic path to lengthen a
  clock — but they have **no property-based test suite proving it under
  adversarial generation**. Treat the invariants as implemented, not as proven.

---

## 9. What is missing, honestly

This is a partial implementation. In order of what would matter most to complete
next:

- **No API, no UI.** Every screen described in the design (live queue board,
  5-second patient card, basis/explainability view, override flow, audit viewer,
  governance/privacy tab, metrics dashboard, hospital-profile selector,
  degradation banner) has its underlying logic built and callable, but no HTTP
  layer or rendered interface exists.
- **No test suite.** `tests/` is empty. There are no unit tests, no integration
  tests, no property-based tests for the eight invariants, and no adversarial or
  degradation/chaos tests. This is the single largest gap between what the design
  claims and what is demonstrated.
- **No evaluation harness.** No baseline policy comparisons, no ablations, no
  computed metrics beyond what the scenario-envelope checks report inline, and no
  fairness matched-pair audit.
- **No demo tooling.** `scripts/run_demo`, `scripts/generate_data`,
  `scripts/run_tests`, `scripts/run_evaluation`, and `scripts/verify_audit` do not
  exist. Only `scripts/build_scenarios.py` does.
- **Five scenarios unexecuted.** S-27 through S-31 (deterioration-while-waiting,
  silence-trigger, and surge scenarios) are authored in the library but need a
  time-stepping injection/queue runner that was never built.
- **A calibration defect in the arrival generator is only partially fixed.** The
  synthetic cohort currently produces red flags at arrival at roughly 15.7%
  against a blueprint target of 6%; this does not affect the hand-authored
  scenario results but would distort any surge-vs-normal comparison run against
  the generated cohort. See STATUS.md for detail.
- **No packaging.** No `requirements.txt`, `pyproject.toml`, `LICENSE`, or
  `.gitignore`.

See [STATUS.md](STATUS.md) for the authoritative, component-by-component account
of what is built, what is verified, and what is not — it is updated alongside any
change to this repository and should be trusted over any summary in this file if
the two ever disagree.
