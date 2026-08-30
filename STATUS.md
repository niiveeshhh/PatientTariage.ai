# PatientTriage.ai — Build Status

**Stopped at user request part-way through implementation.** This file records
exactly what exists, what works, what is miscalibrated, and what was never built.
Nothing below is aspirational.

Team DataBrix · Problem Track 2 · Jurisdiction: India (DPDP Act 2023 + Rules 2025)

---

## How to run what exists

```bash
python scripts/build_scenarios.py
```

```bash
python -c "from app.core.engine import evaluate, make_context, commit_ttl; from app.simulation.scenarios import load_library, build_patient, check_envelope; from app.queue.living_queue import assign_hard_class; lib=load_library(); ok=0; [ok := ok + (lambda sc: (lambda c,p: (lambda r: check_envelope(sc,r[0],r[1],queue_class=assign_hard_class(p,r[0],c.now_min,any(x.floor_level<=2 for x in r[0].fired_rules),False),patient=p).passed)(evaluate(p,c)))(make_context(sc['expected_behaviour_envelope'].get('profile','H-L'),now_min=0.0), build_patient(sc,0.0)))(sc) for sc in lib['scenarios'] if sc['scenario_id'] not in ('S-27','S-28','S-29','S-30','S-31')]; print(ok)"
```

There is no test runner, no API and no UI yet, so there is no `run_demo` command.

---

## BUILT AND VERIFIED

| Component | File | Verification |
|---|---|---|
| Patient data model (§8) — provenance quintuples, missingness reasons, half-lives | `app/core/models.py` | imports; used by all 27 passing scenarios |
| Clinical knowledge loader + profile validator (§16.1) | `app/core/knowledge.py` | rejects any profile that loosens a floor; refuses to load an uncited rule |
| Monotonic simulation clock with skew detection (§5 item 17) | `app/core/clock.py` | imports |
| **L0** integrity + identity gate (§9.2) | `app/clinical/layer0_integrity.py` | S-23 asserts impossible BP is quarantined, not deleted |
| **L1** envelope selection (§9.5) | `app/clinical/layer1_envelope.py` | S-07 age-band flip, S-16 unknown age, pregnancy suppression all pass |
| **L2** deterministic red flags, 32 rules + 3 pathways | `app/clinical/layer2_redflags.py`, `rules/red_flags/hard_rules.json` | every rule carries a citation; load fails otherwise |
| **L3** NEWS2 / paediatric / geriatric read (§9.5) | `app/clinical/layer3_risk.py` | geriatric modifiers implemented as *removals of reassurance*, not point values |
| **L4** five uncertainty components → four classes (§10.1–10.3) | `app/uncertainty/` | S-11 reaches CONFLICTED via the collateral-vs-patient detector |
| **L4** split-conformal escalation (§10.4) | `app/uncertainty/conformal.py` | S-11: point estimate 4, set {2,3,4}, **acted 2**, Escalation Premium 2.0 |
| **L5** dynamic TTL with the I1 ratchet (§12.1) | `app/queue/ttl.py` | single guarded write path; lengthening requires actor + reason + durable audit |
| **L6** Living Queue, classes R>E>B>N, C1–C6, Deficit Board (§11) | `app/queue/living_queue.py` | builds; **K derivation is broken — see defects** |
| **L7** five reassessment triggers T1–T5 (§12.2) | `app/queue/triggers.py` | envelope-normalised deltas implemented |
| **L8** human decision, 9-reason override taxonomy, asymmetric friction (§14.4) | `app/core/department.py`, `app/audit/records.py` | de-escalation without a reason raises `OverrideRefused` |
| Hash-chained tamper-evident audit (§15.2) | `app/audit/chain.py` | **verified: clean chain passes; tampering record 2 is detected at index 2** |
| Degradation ladder L0–L4 (§13.2) | `app/safety/degradation.py` | imports; rung derivation from component health |
| Privacy / DPDP architecture (§15.3) | `app/privacy/governance.py` | purpose binding, 5 roles, retention, breach runbook, DPIA stub, Fourth Schedule position |
| SQLite append-only store | `app/store/db.py` | imports |
| Monotonic GBM in pure Python (§9.4) | `app/models/monotonic_gbm.py` | imports; `verify_monotonicity()` provided |
| Deterioration estimator with bounded authority (§9.3) | `app/models/deterioration.py` | returns `None` when disabled; system runs without it |
| FHIR R4 / ABDM adapter + bundle export (§16.4) | `app/adapters/fhir_r4.py` | round-trips; equivalence differed only by the TTL ratchet in a flawed *test harness*, not the adapter |
| Seeded generator with blueprint distributions (§17.2) | `app/simulation/generator.py` | **red-flag rate miscalibrated — see defects** |
| Latent trajectories, hidden from the engine (§18 G3/G4) | `app/simulation/trajectories.py` | 6 deterioration patterns incl. paediatric compensated shock |
| **32-scenario library, 17 adversarial (53%)** (§17.3) | `data/scenarios/scenarios.json` | **27/27 statically evaluable scenarios pass their expected-behaviour envelopes** |
| 3 hospital profiles + universal safety core (§16.1–16.2) | `rules/profiles/` | H-S carries the three-tier AIIMS vocabulary |

All 30 modules import cleanly.

---

## KNOWN DEFECTS

**1. `derive_k()` — FIXED.** Now returns K=3/2/1 for H-L/H-M/H-S, matching
Blueprint 16.2. The window is one re-look slot (`1 / relooks_per_staff_hour`).

**2. Generator red-flag rate — PARTIALLY FIXED.** Was ~20%, now **15.7%** against
the blueprint's 6% (`app/simulation/generator.py::_emit_vitals`). Hard triggers are
now deliberate (always at L1, ~50% at L2, never L3–L5), but residual derangement
still leaks from the severity curve into level-3 patients, mostly via RF-A02
(ACVPU below A) and the NEWS2 aggregate rules. Consequence: the Deficit Board fires
more readily than it should at 1× load, so any surge comparison run off this
generator would understate the contrast between normal and surge. **Not fixed.**

Neither defect affects the 27 passing scenarios, which use hand-authored vitals.

## NOT BUILT

- **FastAPI layer and the entire UI** — no board, 5-second card, basis view,
  override flow, audit view, governance tab, metrics tab, fairness tab, profile
  selector, surge/Deficit display or degradation banner exists as a *screen*. The
  underlying logic for all of them exists and is callable.
- **Test suites** — `tests/` is empty scaffolding. No unit, integration, scenario,
  adversarial, property-based, degradation or performance tests were written. The
  eight invariants I1–I8 are *enforced in code* but have **no executable tests**,
  so the blueprint's central evidence claim is unsupported.
- **Evaluation harness** — no baselines A–D, no five ablations, no metrics
  computation, no sensitivity sweeps, no fairness matched-pair report.
- **Demo scripts** — `scripts/run_demo`, `generate_data`, `run_tests`,
  `run_evaluation`, `verify_audit` do not exist. Only `build_scenarios.py` does.
- **Documentation** — no README, and `docs/` is empty: no architecture,
  clinical_logic, safety, privacy, validation, demo or traceability matrix.
- **Scenarios S-27 to S-31** are authored but not executed, because they need the
  injection/queue runner that would live in the demo script.
- `pyproject.toml`, `requirements.txt`, `LICENSE`, `.gitignore` — absent.

---

## HONESTY STATEMENTS (carried from the blueprint, unchanged)

- **DPDP-shaped, not DPDP-compliant.** Compliance is an organisational fact
  requiring a DPO, a DPIA, an independent audit, processor contracts, a notice and
  a lawful basis per purpose.
- **100% synthetic data.** No real patient record has entered this system.
- **No accuracy or AUC figure is reported**, and none should be: a model trained on
  trajectories we wrote, predicting those trajectories, measures our own
  consistency rather than medicine.
- **Not clinically validated. Not for clinical use.** Synthetic validation is a
  precondition for real validation, not a substitute for it.
- Every clinical threshold in `rules/` traces to a row in
  `rules/citations/sources.json` or is tagged `ASM` as our own assumption. The
  knowledge loader refuses to start if a rule loses its citation.
