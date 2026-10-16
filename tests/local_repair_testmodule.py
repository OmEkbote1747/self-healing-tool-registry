"""TEST-ONLY Local Repair Module used by tests/test_livedb.py to exercise the adapter. NOT a shipped engine.

It proposes a mapping only from facts ADAPTIX put into the context (catalog renames, type changes, baseline mapping
lines) and quotes them verbatim as evidence. It never invents values."""
import re

NEED = {"float": "to_float", "int": "to_int", "str": "to_str"}


def propose(context: dict) -> dict:
    cands = {a["name"] for a in context.get("candidate_arguments", [])}
    mappings = []
    for f in context.get("unresolved_fields", []):
        desc, field = f.get("description") or "", f["field"]
        table = field.split(":", 1)[1].split(".", 1)[0]
        src, transform, quote = None, "none", ""
        m = re.search(r"\(catalog: (\w+) column attnum \d+ was named (\w+)\)", desc)
        if m and f"{m.group(1)}.{m.group(2)}" in cands:
            src, quote = f"{m.group(1)}.{m.group(2)}", m.group(0)
        m = re.search(r"\(baseline mapping: ([\w.]+) -> [\w.]+\)", desc)
        if m and m.group(1) in cands:
            src, quote = m.group(1), m.group(0)
        m = re.search(r"\(type changed; the workflow needs (\w+)\)", desc)
        if m and field.split(":", 1)[1] in cands:
            src, transform, quote = field.split(":", 1)[1], NEED.get(m.group(1), "none"), m.group(0)
        if src and src.split(".")[0] != table:
            src = None
        mappings.append({"target_field": field, "source_arg": src, "transform": transform, "evidence": quote})
    return {"diagnosis": "test module", "evidence": [], "affected_fields": [], "mappings": mappings,
            "response_mappings": [], "compatibility_risks": [], "confidence": 0.95, "verification_steps": []}


def propose_garbage(context: dict) -> dict:
    return {"diagnosis": "x", "unexpected": "field", "confidence": 7}
