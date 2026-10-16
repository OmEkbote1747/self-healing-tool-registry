"""Structured comparison of the registered schema with the newly discovered one."""
from __future__ import annotations

from pydantic import BaseModel

from .names import similarity
from .schema import Operation


class FieldChange(BaseModel):
    kind: str            # added | removed | renamed | type_changed | required_changed | enum_changed |
                         # format_changed | moved | header_added | header_removed
    location: str
    old: str | None = None
    new: str | None = None
    detail: str = ""


class DriftReport(BaseModel):
    old_signature: str
    new_signature: str
    changed: bool
    method_changed: str | None = None
    content_type_changed: list[str] | None = None
    request_changes: list[FieldChange] = []
    response_changes: list[FieldChange] = []
    compatibility: str = "compatible"     # compatible | repairable | breaking
    summary: str = ""

    def touched(self) -> set[str]:
        out = set()
        for c in self.request_changes + self.response_changes:
            out.add(c.location)
            if c.old:
                out.add(c.old)
        return out


def _leaf(k: str) -> str:
    return k.split(":", 1)[1]


def _compare(old: dict, new: dict, prefix: str) -> list[FieldChange]:
    changes: list[FieldChange] = []
    removed = [k for k in old if k not in new]
    added = [k for k in new if k not in old]
    paired: set[str] = set()
    # likely renames / moves: documented first, then by name similarity
    for a in added:
        f = new[a]
        doc = [r for r in removed if r not in paired and f.renamed_from and _leaf(r).split(".")[-1] == f.renamed_from]
        cands = doc or sorted((r for r in removed if r not in paired and old[r].type == f.type
                               and similarity(_leaf(a), _leaf(r)) >= 50),
                              key=lambda r: -similarity(_leaf(a), _leaf(r)))
        if cands:
            r = cands[0]
            paired.add(r)
            kind = "moved" if _leaf(a).split(".")[-1] == _leaf(r).split(".")[-1] else "renamed"
            why = "documented (x-renamed-from)" if doc else f"name similarity {similarity(_leaf(a), _leaf(r)):.0f}"
            changes.append(FieldChange(kind=kind, location=a, old=r, new=new[a].label(), detail=why))
    for r in removed:
        if r not in paired:
            changes.append(FieldChange(kind="removed", location=r, old=old[r].label()))
    for a in added:
        if not any(c.location == a for c in changes):
            changes.append(FieldChange(kind="added", location=a, new=new[a].label(),
                                       detail="required" if new[a].required else "optional"))
    for k in set(old) & set(new):
        o, n = old[k], new[k]
        if o.types != n.types and o.enum is None and n.enum is None:
            changes.append(FieldChange(kind="type_changed", location=k, old=o.type, new=n.type))
        if o.required != n.required:
            changes.append(FieldChange(kind="required_changed", location=k, old=str(o.required), new=str(n.required)))
        if o.enum != n.enum:
            changes.append(FieldChange(kind="enum_changed", location=k, old=str(o.enum), new=str(n.enum)))
        if o.fmt != n.fmt:
            changes.append(FieldChange(kind="format_changed", location=k, old=o.fmt, new=n.fmt))
    return changes


def diff(old: Operation, new: Operation | str | None) -> DriftReport:
    if isinstance(new, str):
        return DriftReport(old_signature=old.signature(), new_signature="?", changed=True, method_changed=new,
                           compatibility="breaking", summary=f"the API now expects {new} instead of {old.method}")
    req = _compare(old.fields, new.fields, "")
    req += [FieldChange(kind="header_added", location=f"header:{h}") for h in new.headers if h not in old.headers]
    req += [FieldChange(kind="header_removed", location=f"header:{h}") for h in old.headers if h not in new.headers]
    resp = _compare(old.response_fields, new.response_fields, "")
    ct = new.content_types if sorted(new.content_types) != sorted(old.content_types) else None
    breaking = bool(ct and "application/json" not in (ct or []))
    compat = "breaking" if breaking else ("repairable" if (req or resp) else "compatible")
    kinds = {}
    for c in req + resp:
        kinds[c.kind] = kinds.get(c.kind, 0) + 1
    summary = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(kinds.items())) or "no schema change"
    return DriftReport(old_signature=old.signature(), new_signature=new.signature(),
                       changed=old.signature() != new.signature(), content_type_changed=ct, request_changes=req,
                       response_changes=resp, compatibility=compat, summary=summary)


def sent_locations(req: dict) -> set[str]:
    """Location keys ("body:a.b", "query:x", "path:id") of the request that was actually built."""
    out: set[str] = set()

    def walk(d, prefix):
        for k, v in d.items():
            if isinstance(v, dict) and v:
                walk(v, f"{prefix}{k}.")
            else:
                out.add(f"body:{prefix}{k}")
    if isinstance(req.get("body"), dict):
        walk(req["body"], "")
    out |= {f"query:{k}" for k in (req.get("query") or {})}
    out |= {f"path:{k}" for k in (req.get("path_params") or {})}
    return out


def request_conflicts(report: DriftReport, sent: set[str], sent_headers: set[str]) -> list[str]:
    """How the request actually sent violates the NEW schema on locations the drift touched (evidence)."""
    out = []
    for c in report.request_changes:
        if c.kind == "removed" and c.location in sent:
            out.append(f"sent {c.location}, which the schema no longer accepts")
        elif c.kind in ("renamed", "moved") and c.old in sent:
            out.append(f"sent {c.old}, now {c.location}")
        elif c.kind == "added" and c.detail == "required" and c.location not in sent:
            out.append(f"did not send newly required {c.location}")
        elif c.kind == "type_changed" and c.location in sent:
            out.append(f"{c.location} must now be {c.new}")
        elif c.kind == "required_changed" and c.new == "True" and c.location not in sent:
            out.append(f"{c.location} is now required")
        elif c.kind == "header_added" and c.location.split(":", 1)[1] not in sent_headers:
            out.append(f"did not send newly required {c.location}")
    return out
