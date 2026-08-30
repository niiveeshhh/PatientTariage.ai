from app.core.engine import evaluate, make_context, commit_ttl
from app.simulation.scenarios import load_library, build_patient, check_envelope
from app.queue.living_queue import assign_hard_class

lib = load_library()
passed, failed = 0, []
for sc in lib["scenarios"]:
    if sc["scenario_id"] in ("S-27", "S-28", "S-29", "S-30", "S-31"):
        continue
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