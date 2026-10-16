"""Template for the Local Repair Module (to be implemented by the team).

ADAPTIX loads the module named by ADAPTIX_LOCAL_REPAIR_MODULE (a Python import path, e.g.
`local_repair.engine`) and calls `propose(context)` when an experiment selects the "local" engine.
ADAPTIX never falls back to another engine and never invents output when this module is missing or fails.

context (only names, types and schema text - never row values or credentials):
    {"tool": str, "drift_summary": str,
     "unresolved_fields": [{"field": "body:client_id", "type": "str", "description": "..."}],
     "candidate_arguments": [{"name": "customer_id", "type": "str"}],
     "database_error": {"sqlstate": "42703", "kind": "undefined_column"}}

Return a dict matching adaptix.models.LLMRepairPlan (validated strictly; extra keys are rejected):
    {"diagnosis": str, "evidence": [str], "affected_fields": [str],
     "mappings": [{"target_field": "body:client_id", "source_arg": "customer_id", "transform": "none",
                   "evidence": "<text copied verbatim from a field description>"}],
     "response_mappings": [], "compatibility_risks": [str], "confidence": 0.0-1.0, "verification_steps": [str]}

The proposal is untrusted: ADAPTIX validates it independently (verbatim evidence, types, no invented or
dropped values), tests it in the sandbox schema and verifies the business outcome before keeping it.
"""


def propose(context: dict) -> dict:
    raise NotImplementedError("the Local Repair Module has not been implemented yet")
