"""Deterministic repair planning (Tier 1). Produces a RepairPlan; never applies anything itself.

A required field of the new schema is mapped to a caller argument only with evidence:
  1. documentation: the new field's x-renamed-from names the old field, or its description names the argument
  2. a confident name match (similarity >= threshold, clear margin, no unit word change, same entity)
Anything else is left in `unresolved` (the LLM may propose a mapping; the validator decides; otherwise stop).
"""
from __future__ import annotations

import re

from .credentials import CredentialRegistry
from .diff import DriftReport
from .models import Mapping, RepairPlan, ResponseMapping, ToolSpec
from .names import entity_mismatch, similarity, tokens, unit_change
from .schema import FieldSpec, Operation
from .tooldef import TYPE_TO_TRANSFORM, ToolDef, py_type


def leaf(loc: str) -> str:
    return loc.split(":", 1)[1]


def transform_for(value, f: FieldSpec) -> str:
    if f.enum is not None:
        return "none"
    want = f.type
    if want == "date":
        return "iso_date"
    if want in ("list", "object") or py_type(value) in f.types:
        return "none"
    return TYPE_TO_TRANSFORM.get(want, "none")


def documented(f: FieldSpec, arg: str, old_loc: str | None) -> str | None:
    """Verbatim evidence from the schema that `arg` (or its old field) became this field."""
    names = {arg, leaf(old_loc).split(".")[-1] if old_loc else arg}
    if f.renamed_from and f.renamed_from in names:
        return f"x-renamed-from: {f.renamed_from}"
    for n in names:
        if f.description and re.search(rf"(?<![A-Za-z0-9_]){re.escape(n)}(?![A-Za-z0-9_])", f.description):
            return f.description
    return None


def plan_request(td: ToolDef, spec: ToolSpec, new: Operation, args: dict, creds: CredentialRegistry,
                 auto_min: float = 75.0) -> RepairPlan:
    current = {loc: (s, t) for loc, (s, t) in td.request_map.items() if s in args}
    kept = {loc: v for loc, v in current.items() if loc in new.fields}
    unfilled = [loc for loc, f in new.fields.items() if f.required and loc not in kept]
    optional = [loc for loc, f in new.fields.items() if not f.required and loc not in kept]
    orphan_locs = {s: loc for loc, (s, _) in current.items() if loc not in new.fields}
    orphans = list(orphan_locs)
    mappings: list[Mapping] = []
    evidence, risks, unresolved = [], [], []
    used: set[str] = set()
    conf = 1.0

    for loc, (s, _) in kept.items():
        mappings.append(Mapping(target_field=loc, source_arg=s, transform=transform_for(args[s], new.fields[loc]),
                                evidence="unchanged field"))
    for loc in unfilled:
        f = new.fields[loc]
        docs = [(s, documented(f, s, orphan_locs[s])) for s in orphans if s not in used]
        docs = [(s, q) for s, q in docs if q]
        if len(docs) == 1:
            s, quote = docs[0]
            if unit_change(s, leaf(loc)):
                risks.append(f"{s} -> {leaf(loc)}: documented rename but the unit changed; not mapped")
                unresolved.append(loc)
                continue
            mappings.append(Mapping(target_field=loc, source_arg=s, transform=transform_for(args[s], f), evidence=quote))
            evidence.append(f"{leaf(loc)} documented as replacing {s}: \"{quote[:120]}\"")
            used.add(s)
            continue
        scored = sorted(((similarity(leaf(loc), s), s) for s in orphans if s not in used), reverse=True)
        if scored:
            best, s = scored[0]
            margin = best - (scored[1][0] if len(scored) > 1 else 0)
            problems = []
            if best < auto_min:
                problems.append(f"weak name match (similarity {best:.0f})")
            if len(scored) > 1 and margin < 15:
                problems.append("two arguments match almost equally")
            if unit_change(s, leaf(loc)):
                problems.append(f"unit word changed ({', '.join(sorted(unit_change(s, leaf(loc))))})")
            if entity_mismatch(s, leaf(loc)):
                problems.append("names refer to different things")
            if not problems:
                mappings.append(Mapping(target_field=loc, source_arg=s, transform=transform_for(args[s], f),
                                        evidence=f"field names: {s} -> {leaf(loc)} (similarity {best:.0f})"))
                evidence.append(f"{s} -> {leaf(loc)} by name (similarity {best:.0f})")
                used.add(s)
                conf = min(conf, best / 100)
                continue
            risks.append(f"{s} -> {leaf(loc)}: " + "; ".join(problems))
        unresolved.append(loc)
    for loc in optional:
        exact = [s for s in orphans if s not in used and tokens(s) == tokens(leaf(loc).split(".")[-1])]
        if exact:
            mappings.append(Mapping(target_field=loc, source_arg=exact[0], transform=transform_for(args[exact[0]],
                                    new.fields[loc]), evidence="exact name"))
            used.add(exact[0])

    headers = {}
    scope = f"tool:{spec.name}"
    for h in new.headers:
        if h in td.header_map:
            headers[h] = td.header_map[h]
        elif spec.auth_header and h == spec.auth_header.lower() and spec.credential_group:
            headers[h] = spec.credential_group
        else:
            groups = creds.groups_for_header(h, scope)
            if len(groups) == 1:
                headers[h] = groups[0]
                evidence.append(f"header {h} from configured credential group '{groups[0]}'")
            else:
                unresolved.append(f"header:{h}")
                risks.append(f"no single configured credential group for header {h}")

    return RepairPlan(source="rules", diagnosis="request schema changed", evidence=evidence,
                      affected_fields=unfilled + [f"header:{h}" for h in new.headers if h not in td.header_map],
                      mappings=mappings, compatibility_risks=risks, confidence=conf,
                      verification_steps=["status output is a success value", "response echoes the sent values"],
                      header_sources=headers, unresolved=unresolved,
                      response_mappings=plan_response(td, new).response_mappings)


def plan_response(td: ToolDef, new: Operation) -> RepairPlan:
    """Map logical outputs (status, echo) whose response field disappeared to a renamed field."""
    out, unresolved = [], []
    available = {leaf(k): f for k, f in new.response_fields.items()}
    for output, path in td.response_map.items():
        if path in available or not available:
            continue
        cands = [p for p, f in available.items() if f.renamed_from == path.split(".")[-1]]
        quote = f"x-renamed-from: {path.split('.')[-1]}" if cands else ""
        if not cands:
            scored = sorted(((similarity(p, path), p) for p in available if p not in td.response_map.values()),
                            reverse=True)
            if scored and scored[0][0] >= 75 and not entity_mismatch(path, scored[0][1]):
                cands, quote = [scored[0][1]], f"field names: {path} -> {scored[0][1]}"
        if len(cands) == 1:
            out.append(ResponseMapping(output=output, response_path=cands[0], evidence=quote))
        else:
            unresolved.append(f"response:{path}")
    return RepairPlan(source="rules", diagnosis="response schema changed", response_mappings=out,
                      confidence=1.0, unresolved=unresolved)
