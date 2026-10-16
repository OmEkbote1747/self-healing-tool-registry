"""Tier 2: an LLM proposes a field mapping when the rules are not sure.

The LLM only ever proposes which EXISTING argument fills which NEW field. It
never supplies values. Whatever it proposes still goes through the validator.
Enabled only when Anthropic credentials are available (ANTHROPIC_API_KEY).
"""
from __future__ import annotations

import json
import os

MODEL = os.environ.get("SELFHEAL_LLM_MODEL", "claude-opus-5-5")

_SCHEMA = {
    "type": "object",
    "properties": {
        "mappings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "source": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "reason": {"type": "string"},
                },
                "required": ["target", "source", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["mappings"],
    "additionalProperties": False,
}


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def build_prompt(tool: str, unfilled: dict[str, str], orphans: dict[str, str], errors: list[dict]) -> str:
    """Only field names, types and error locations go to the model - never values or headers."""
    safe_errors = [{"type": e.get("type"), "loc": e.get("loc"), "msg": e.get("msg")} for e in errors
                   if not (e.get("loc") or [""])[0] == "header"]
    return (
        f"An API used by the tool '{tool}' changed its request schema and rejected our call.\n"
        f"Validation errors from the API (values removed):\n{json.dumps(safe_errors, indent=1)}\n\n"
        f"New required fields with no value yet (path: type):\n{json.dumps(unfilled, indent=1)}\n\n"
        f"Arguments the agent already holds that no longer match any field (name: type):\n"
        f"{json.dumps(orphans, indent=1)}\n\n"
        "For each new field, pick the ONE argument that carries the same meaning, or null if none "
        "clearly does. Never invent values and never reuse one argument for two fields. "
        "If two arguments are equally plausible for a field, answer null for it."
    )


def propose_mapping(tool: str, unfilled: dict[str, str], orphans: dict[str, str],
                    errors: list[dict]) -> tuple[dict[str, str | None], int, str]:
    """Return ({new_field_path: source_arg_or_None}, tokens_used, note)."""
    import anthropic

    prompt = build_prompt(tool, unfilled, orphans, errors)
    client = anthropic.Anthropic()
    try:
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIConnectionError:
        return {}, 0, "LLM unreachable (network)"
    except anthropic.RateLimitError:
        return {}, 0, "LLM rate-limited"
    except anthropic.APIStatusError as e:
        return {}, 0, f"LLM error {e.status_code}"
    tokens = (resp.usage.input_tokens or 0) + (resp.usage.output_tokens or 0)
    if resp.stop_reason == "refusal":
        return {}, tokens, "LLM declined"
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    data = json.loads(text)
    return {m["target"]: m["source"] for m in data.get("mappings", [])}, tokens, f"proposed by {MODEL}"
