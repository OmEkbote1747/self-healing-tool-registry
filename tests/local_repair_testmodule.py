"""TEST-ONLY Local Repair Module used by tests/test_livedb.py to exercise the adapter. Not shipped as an engine.

It maps a field only when the catalog states which column it was (renamed_from text in the description)."""
import re


def propose(context: dict) -> dict:
    mappings = []
    names = {a["name"] for a in context.get("candidate_arguments", [])}
    for f in context.get("unresolved_fields", []):
        m = re.search(r"\(catalog: column attnum \d+ was named (\w+)\)", f.get("description") or "")
        src = m.group(1) if m and m.group(1) in names else None
        mappings.append({"target_field": f["field"], "source_arg": src, "transform": "none",
                         "evidence": m.group(0) if src else ""})
    return {"diagnosis": "test module", "evidence": [], "affected_fields": [], "mappings": mappings,
            "response_mappings": [], "compatibility_risks": [], "confidence": 0.95, "verification_steps": []}


def propose_garbage(context: dict) -> dict:
    return {"diagnosis": "x", "unexpected": "field", "confidence": 7}
