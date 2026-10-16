import streamlit as st

from views.common import (REPO_URL, cards, flow, footer, headline_numbers, kpis, section)


def render():
    st.markdown("""
<div class="sh-hero">
  <div class="sh-badge">PS-01 · Genesis 2.0 · Team Runtime Terrors</div>
  <h1>Keep AI agents running<br/>when the <b class="accent">APIs they use change.</b></h1>
  <p>SchemaHeal sits between an agent and its tools. When an API changes its schema, it works out what changed,
  checks the fix is safe, patches the tool and finishes the task, <b>without ever making up data</b>.</p>
</div>""", unsafe_allow_html=True)

    c1, c2, _ = st.columns([1, 1, 0.7])
    if c1.button("Launch the console  →", type="primary", use_container_width=True):
        st.switch_page(st.session_state["_pages"]["demo"])
    c2.link_button("View source on GitHub", REPO_URL, use_container_width=True)

    n = headline_numbers()
    section("Measured on the live prototype", "Same 125 synthetic cases for our agent and a standard retry-loop agent. "
            "Metric names follow the PS-01 problem statement.")
    kpis([
        {"label": "Zero-touch adaptation rate", "value": f"{n['zero_touch_rate']:.0f}%",
         "detail": f"{n['zero_touch']} / {n['heal_total']} healable fixed with no human",
         "target": f"target > 90% · {n['held']} more held for approval"},
        {"label": "Safe stop rate", "value": f"{n['safe_stop_rate']:.0f}%",
         "detail": f"{n['safe_stops']} / {n['stop_total']} unhealable stopped or held",
         "target": f"false repairs: {n['false_repairs']} · target 100%"},
        {"label": "MTTR", "value": f"{n['mttr_ms']:.1f} ms",
         "detail": "median, API break to recovery", "target": "target: < 4 s"},
        {"label": "Token overhead", "value": f"{n['tokens']}",
         "detail": "tokens for rule-based fixes", "target": "baseline: 5 failed calls per case"},
    ])

    section("The problem", "Production agents use fixed tool schemas. APIs keep changing underneath them.")
    flow([("API changes silently", ""), ("Agent gets a 422", "red"), ("Retries the same request", "red"),
          ("Guesses values, corrupts data", "red")])

    section("Why SchemaHeal is different", "Existing tools each cover one stage. We combine all five, at runtime.")
    cards([
        {"icon": "⚡", "title": "Runtime diagnosis", "text": "Reads the 422 and the new /openapi.json while the agent is running. No redeploy."},
        {"icon": "🔧", "title": "Automatic repair", "text": "Renames, moves and converts fields so the request matches the new schema."},
        {"icon": "✓", "title": "Validate before retry", "text": "Every repair is checked against the new schema before anything is sent."},
        {"icon": "⏱", "title": "Bounded + audited", "text": "At most two repair attempts, then a clean stop. Every step is logged."},
        {"icon": "♻", "title": "Reusable repairs", "text": "Fixes are versioned and shared, so the next agent never hits the same error."},
    ], cols=5)

    section("Key features")
    cards([
        {"icon": "1", "title": "Two-tier diagnosis", "text": "Rules handle renames, nesting and type changes at zero token cost. "
         "An LLM is asked only when the rules are unsure."},
        {"icon": "2", "title": "AI proposes, validator decides", "text": "The LLM may only pick which existing value fits a field. "
         "It can never supply a value of its own."},
        {"icon": "3", "title": "Never makes up data", "text": "If a required value or header is missing, SchemaHeal stops and "
         "reports exactly what is needed."},
        {"icon": "4", "title": "Approved config for secrets", "text": "New headers such as API keys are only taken from approved "
         "configuration, never guessed."},
        {"icon": "5", "title": "Versioned tool registry", "text": "Each repair creates tool v2, v3 and so on. Any version can be "
         "rolled back in one click."},
        {"icon": "6", "title": "Audit log + live dashboard", "text": "Append-only JSON Lines log and a side-by-side view of a "
         "normal agent versus ours."},
    ], cols=3)

    section("How it works")
    flow([("Agent calls a tool", ""), ("API returns 422", "red"), ("Diagnose: rules → LLM", "blue"),
          ("Validate", "blue"), ("Patch + log", "blue"), ("Retry once", "blue"), ("Task done", "green")])

    section("Where existing tools stop")
    st.dataframe(
        {"Existing approach": ["Contract testing (oasdiff, Pact)", "Retry with error feedback (LangChain)",
                               "Managed integrations (Composio)"],
         "Good at": ["catching breaks before deploy", "letting the LLM try again", "maintained connectors"],
         "What SchemaHeal adds": ["repair after deploy, at runtime", "a validated, bounded repair",
                                  "self-repair for any API + audit log"]},
        hide_index=True, use_container_width=True)

    st.markdown('<div class="sh-note"><b>Honest limits.</b> The 125 cases were written by our team and run against our '
                'own mock API. The baseline is a simple retry loop. In harder stress tests 6 of 10 pass, with 2 wrong '
                'repairs (similar names with different meanings, hidden unit changes), which we plan to route to the '
                'LLM or a human for approval.</div>', unsafe_allow_html=True)
    st.write("")
    if st.button("Open the live demo  →", type="primary"):
        st.switch_page(st.session_state["_pages"]["demo"])
    footer()
