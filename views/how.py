import streamlit as st

from views.common import flow, footer, page_header


def render():
    page_header("Overview · How it works", "From a 422 to a safe fix",
                "The agent never changes. Only the tool definitions in the registry are patched.")
    flow([("Agent", ""), ("Tool registry", "blue"), ("API", ""), ("422 error", "red"), ("Diagnose", "blue"),
          ("Validate", "blue"), ("Policy gate", "blue"), ("Canary patch + log", "blue"), ("Retry once", "green")])
    st.graphviz_chart("""
    digraph {
      rankdir=LR; bgcolor="transparent"; nodesep=0.35; ranksep=0.45;
      node [shape=box, style="rounded,filled", fillcolor="#ffffff", color="#BFD5F2", fontname="Helvetica", fontsize=13, margin="0.18,0.10"];
      edge [color="#7FA6D8", fontname="Helvetica", fontsize=11];
      agent [label="LLM agent"]; reg [label="Tool registry\\n(versioned)", fillcolor="#E6F0FC"]; api [label="API"];
      parse [label="Error parser\\n+ /openapi.json"]; t1 [label="Tier 1: rules"]; t2 [label="Tier 2: LLM\\n(only if unsure)"];
      val [label="Validator\\nschema + provenance", fillcolor="#E6F0FC"];
      gate [label="Policy gate\\nconfidence, entity, unit", fillcolor="#E6F0FC"];
      hold [label="Held for a human", fillcolor="#FFF6E0"];
      ok [label="Canary patch + log\\nretry once", fillcolor="#E7F6EC"]; stop [label="Stop and report", fillcolor="#FDECEC"];
      agent -> reg -> api; api -> parse [label="422"]; parse -> t1; t1 -> t2 [label="unsure"];
      t1 -> val; t2 -> val; val -> gate [label="valid"]; val -> stop [label="invalid"];
      gate -> ok [label="safe"]; gate -> hold [label="ambiguous"]; hold -> ok [label="approved"]; ok -> reg;
    }""", use_container_width=True)
    c1, c2 = st.columns(2, gap="large")
    with c1:
        st.markdown(
            "#### The pipeline\n"
            "1. The agent calls a tool through the registry, never the API directly.\n"
            "2. On a 422, the error is parsed and the new schema read from `/openapi.json` "
            "(falls back to the error locations).\n"
            "3. **Tier 1 rules** match renamed or moved fields by name similarity and plan lossless type conversions. "
            "If unsure, **Tier 2** asks an LLM which existing argument fits.\n"
            "4. The **validator** checks the repaired request against the new schema and that every value comes from "
            "the agent's data or approved config.\n"
            "5. The **policy gate** sends medium-confidence matches, different entity names, unit changes and LLM "
            "suggestions to a human. Safe repairs become a **canary** version used only by their creator, shared "
            "after 3 successes, rolled back if they fail.")
    with c2:
        st.markdown(
            "#### Safety rules\n"
            "- Never invent a value: only move, rename or convert what the agent already has\n"
            "- Validate before every retry\n"
            "- At most 2 repair attempts, then a clean stop\n"
            "- Headers only from approved configuration\n"
            "- The LLM can choose among existing values, never supply one\n\n"
            "#### Tech stack\n"
            "Python · FastAPI · Pydantic · jsonschema · RapidFuzz · Anthropic SDK (optional) · Streamlit")
    footer()
