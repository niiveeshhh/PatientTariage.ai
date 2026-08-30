from app.audit.chain import AuditChain

chain = AuditChain()
for i in range(5):
    chain.append({"event_type": "recommendation", "patient_ref": f"P{i}", "v": i})
print("clean chain valid:", chain.verify().valid)

chain.tamper_for_demo(2, "v", 999)
result = chain.verify()
print("after tampering record 2:", result.valid, "-> first broken index", result.first_broken_index)