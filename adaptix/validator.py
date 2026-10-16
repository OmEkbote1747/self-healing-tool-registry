"""Independent validation of a repair plan, whatever produced it (rules, LLM or reuse).

The plan is untrusted input. It is accepted only if every check passes; the result is a candidate
ToolDef whose request can be rebuilt exclusively from the caller's own arguments and configured credentials.
"""
from __future__ import annotations

import copy

from jsonschema import Draft202012Validator, FormatChecker

from .credentials import CredentialRegistry
from .models import RepairPlan, ToolSpec, ValidationResult
from .names import entity_mismatch, similarity, unit_change
from .planner import leaf
from .schema import Operation
from .tooldef import TRANSFORMS, ToolDef, apply_transform, get_path


def _check(res: ValidationResult, name: str, ok: bool, detail: str = ""):
    res.checks.append({"name": name, "ok": ok, "detail": detail})
    if not ok:
        res.rejected_reasons.append(f"{name}: {detail}" if detail else name)


def _leaves(d):
    if isinstance(d, dict):
        for v in d.values():
            yield from _leaves(v)
    else:
        yield d


def validate(plan: RepairPlan, td: ToolDef, spec: ToolSpec, new: Operation, args: dict,
             creds: CredentialRegistry, auto_min: float = 75.0, llm_min_confidence: float = 0.8,
             response_json: dict | None = None) -> tuple[ValidationResult, ToolDef | None]:
    res = ValidationResult(accepted=False)
    evidence_text = new.evidence_text()

    _check(res, "method unchanged", new.method == td.method, f"{td.method} -> {new.method}")
    _check(res, "JSON request body", not new.content_types or "application/json" in new.content_types,
           ", ".join(new.content_types))
    _check(res, "no unresolved required fields", not plan.unresolved, ", ".join(plan.unresolved))
    if plan.source == "llm":
        _check(res, "model confidence", plan.confidence >= llm_min_confidence,
               f"{plan.confidence:.2f} < {llm_min_confidence:.2f}" if plan.confidence < llm_min_confidence else f"{plan.confidence:.2f}")

    request_map: dict[str, list] = {}
    used: set[str] = set()
    for m in plan.mappings:
        tag = f"{m.source_arg} -> {m.target_field}"
        if m.source_arg is None:
            continue
        if m.target_field not in new.fields:
            _check(res, "target field exists in schema", False, tag)
            continue
        if m.source_arg not in args:
            _check(res, "source is a caller argument", False, tag)
            continue
        if m.source_arg in used or m.target_field in request_map:
            _check(res, "one-to-one mapping", False, tag)
            continue
        if m.transform not in TRANSFORMS:
            _check(res, "allowlisted transform", False, f"{tag}: {m.transform}")
            continue
        carried_over = (td.request_map.get(m.target_field) or [None])[0] == m.source_arg   # validated before
        renamed = leaf(m.target_field).split(".")[-1] != m.source_arg and not carried_over
        if renamed:
            units = unit_change(m.source_arg, leaf(m.target_field))
            if units:
                _check(res, "no unit change", False, f"{tag}: {', '.join(sorted(units))}")
                continue
            quote = (m.evidence or "").strip()
            name_based = quote.startswith("field names:")      # must then pass the strict name check
            if name_based:
                ok = (similarity(leaf(m.target_field), m.source_arg) >= auto_min
                      and not entity_mismatch(m.source_arg, leaf(m.target_field)))
                _check(res, "confident name match", ok, tag)
                if not ok:
                    continue
            else:
                grounded = bool(quote) and quote in evidence_text
                _check(res, "evidence quoted verbatim from the authoritative schema", grounded,
                       f"{tag}: {quote[:80]!r}")
                if not grounded:
                    continue
                if entity_mismatch(m.source_arg, leaf(m.target_field)):
                    names_arg = m.source_arg in quote
                    _check(res, "evidence names the original argument", names_arg, tag)
                    if not names_arg:
                        continue
        f = new.fields[m.target_field]
        try:
            value = apply_transform(args[m.source_arg], m.transform)
        except ValueError as e:
            _check(res, "lossless conversion", False, f"{tag}: {e}")
            continue
        errs = [e.message for e in Draft202012Validator(f.schema, format_checker=FormatChecker()).iter_errors(value)]
        if f.enum is not None and str(value) not in [str(x) for x in f.enum]:
            errs.append(f"{value!r} is not one of {f.enum}")
        _check(res, "value allowed by the new field", not errs, f"{tag}: {'; '.join(errs)[:160]}")
        if errs:
            continue
        request_map[m.target_field] = [m.source_arg, m.transform]
        used.add(m.source_arg)

    missing = [loc for loc, f in new.fields.items() if f.required and loc not in request_map]
    _check(res, "every required field mapped", not missing, ", ".join(missing))
    # a value the caller supplied and the old version sent must not silently disappear: if the API removed
    # that field, the caller's intent can no longer be fulfilled as asked
    sent_before = {s for s, _ in td.request_map.values() if s in args and args[s] is not None}
    dropped = sorted(sent_before - used)
    _check(res, "no caller-supplied value silently dropped", not dropped,
           ", ".join(dropped) + (" would be discarded" if dropped else ""))

    header_map = {}
    scope = f"tool:{spec.name}"
    for h in new.headers:
        g = plan.header_sources.get(h)
        ok = bool(g) and creds.has_group(g) and any(c["group"] == g and ("*" in c["scope"] or scope in c["scope"])
                                                    for c in creds.status())
        _check(res, "header from a configured, permitted credential", ok, f"{h} -> {g or 'none'}")
        if ok:
            header_map[h] = g

    response_map = dict(td.response_map)
    for rm in plan.response_mappings:
        ok = f"response:{rm.response_path}" in new.response_fields
        if ok and rm.evidence and not rm.evidence.startswith("field names:"):
            ok = rm.evidence in evidence_text
        if ok and response_json is not None:
            try:
                get_path(response_json, rm.response_path)
            except KeyError:
                ok = False
        _check(res, "response mapping grounded", ok, f"{rm.output} -> {rm.response_path}")
        if ok:
            response_map[rm.output] = rm.response_path

    candidate = ToolDef(name=td.name, version=td.version + 1, method=td.method, path=td.path,
                        request_map=request_map, header_map=header_map, response_map=response_map,
                        schema_signature=new.signature(),
                        patch=_patch(td, request_map, header_map, response_map))
    req = candidate.build(args)
    if new.body_schema is not None:
        errs = [e.message for e in Draft202012Validator(new.body_schema, format_checker=FormatChecker())
                .iter_errors(req["body"] or {})]
        _check(res, "rebuilt request matches the new schema", not errs, "; ".join(errs)[:200])
    allowed = {repr(v) for v in args.values()}
    for src, t in request_map.values():
        allowed.add(repr(apply_transform(args[src], t)))
    sent = list(_leaves(req["body"] or {})) + list(req["query"].values()) + list(req["path_params"].values())
    invented = [v for v in sent if repr(v) not in allowed and not isinstance(v, (dict, list))]
    _check(res, "every value comes from the caller's input", not invented, str(invented)[:120])

    res.accepted = all(c["ok"] for c in res.checks)
    return res, (candidate if res.accepted else None)


def validate_response(plan: RepairPlan, td: ToolDef, new: Operation, response_json: dict
                      ) -> tuple[ValidationResult, ToolDef | None]:
    """Response-schema drift: only the response mapping changes; the request is untouched."""
    res = ValidationResult(accepted=False)
    _check(res, "no unresolved outputs", not plan.unresolved, ", ".join(plan.unresolved))
    _check(res, "at least one output remapped", bool(plan.response_mappings))
    evidence_text = new.evidence_text()
    response_map = dict(td.response_map)
    for rm in plan.response_mappings:
        ok = f"response:{rm.response_path}" in new.response_fields
        _check(res, "output exists in the response schema", ok, rm.response_path)
        if rm.evidence and not rm.evidence.startswith("field names:"):
            ok = ok and rm.evidence in evidence_text
            _check(res, "evidence quoted verbatim from the authoritative schema", rm.evidence in evidence_text,
                   rm.evidence[:80])
        try:
            get_path(response_json, rm.response_path)
            present = True
        except KeyError:
            present = False
        _check(res, "output present in the actual response", present, rm.response_path)
        if ok and present:
            response_map[rm.output] = rm.response_path
    res.accepted = all(c["ok"] for c in res.checks)
    if not res.accepted:
        return res, None
    cand = copy.deepcopy(td)
    cand.response_map, cand.schema_signature = response_map, new.signature()
    cand.patch = _patch(td, td.request_map, td.header_map, response_map)
    return res, cand


def _patch(old: ToolDef, request_map, header_map, response_map) -> list[dict]:
    """Reversible description of what changed (old values kept so the change can be undone)."""
    ops = []
    for loc, v in request_map.items():
        prev = next((l for l, (s, _) in old.request_map.items() if s == v[0]), None)
        if prev != loc or old.request_map.get(loc) != v:
            ops.append({"op": "map", "argument": v[0], "from": prev, "to": loc, "transform": v[1]})
    for loc, (s, _) in old.request_map.items():
        if s not in [v[0] for v in request_map.values()]:
            ops.append({"op": "unmap", "argument": s, "from": loc})
    for h, g in header_map.items():
        if old.header_map.get(h) != g:
            ops.append({"op": "header", "header": h, "credential_group": g})
    for o, p in response_map.items():
        if old.response_map.get(o) != p:
            ops.append({"op": "response", "output": o, "from": old.response_map.get(o), "to": p})
    return copy.deepcopy(ops)
