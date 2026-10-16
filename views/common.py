"""Shared look-and-feel and helpers for every page."""
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "patches.jsonl"
DB = ROOT / "logs" / "registry.db"


def session_registry():
    """A durable registry scoped to this browser session (its own tenant), so visitors never see each other."""
    import uuid
    from selfheal.store import SqliteRegistry
    tenant = st.session_state.setdefault("tenant", "session-" + uuid.uuid4().hex[:8])
    return SqliteRegistry(DB, tenant=tenant)
APP_NAME = "SchemaHeal"
REPO_URL = "https://github.com/Paras131206/self-healing-tool-registry"

CSS = """
<style>
.block-container { padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1240px; }
h1, h2, h3 { letter-spacing: -0.02em; color: #0F1E33; }

/* page header */
.sh-eyebrow { color:#1565C0; font-weight:700; font-size:0.85rem; letter-spacing:.08em; text-transform:uppercase; margin-bottom:.2rem; }
.sh-title { font-size:2.3rem; font-weight:800; color:#0F1E33; line-height:1.15; margin:0 0 .4rem 0; }
.sh-sub { color:#4A5B72; font-size:1.1rem; margin-bottom:1.2rem; max-width: 860px; }

/* hero */
.sh-hero { background: linear-gradient(135deg, #F4F9FF 0%, #E3EFFD 100%); border:1px solid #D6E4F5;
           border-radius: 22px; padding: 3rem 3rem 2.4rem 3rem; margin-bottom: 1.2rem; }
.sh-badge { display:inline-block; background:#FFFFFF; border:1px solid #CFE0F6; color:#1565C0; font-weight:600;
            font-size:.85rem; padding:.3rem .8rem; border-radius:999px; margin-bottom:1rem; }
.sh-hero h1 { font-size: 3.1rem !important; font-weight: 800; line-height:1.08; margin: 0 0 1rem 0; color:#0F1E33 !important; padding:0 !important; }
.sh-hero h1 .accent { color:#1565C0 !important; }
.sh-hero p { font-size: 1.25rem; color:#3A4B62; max-width: 780px; margin:0; }

/* cards */
.sh-grid { display:grid; gap: 1rem; margin: .4rem 0 1.4rem 0; }
.sh-g2 { grid-template-columns: repeat(2, 1fr); } .sh-g3 { grid-template-columns: repeat(3, 1fr); }
.sh-g4 { grid-template-columns: repeat(4, 1fr); } .sh-g5 { grid-template-columns: repeat(5, 1fr); }
@media (max-width: 900px) { .sh-g3, .sh-g4, .sh-g5 { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 560px) { .sh-g2, .sh-g3, .sh-g4, .sh-g5 { grid-template-columns: 1fr; } }
.sh-card { background:#FFFFFF; border:1px solid #DCE8F6; border-radius:16px; padding:1.25rem 1.3rem;
           box-shadow: 0 1px 2px rgba(16,42,84,.04), 0 6px 18px rgba(16,42,84,.05); height:100%; }
.sh-card h4 { margin:.55rem 0 .35rem 0; font-size:1.12rem; font-weight:700; color:#0F1E33; }
.sh-card p { margin:0; color:#4A5B72; font-size:.98rem; line-height:1.45; }
.sh-icon { width:42px; height:42px; border-radius:12px; background:#E6F0FC; color:#1565C0; display:flex;
           align-items:center; justify-content:center; font-size:1.3rem; font-weight:800; }
.sh-kpi { text-align:left; }
.sh-kpi .label { color:#1565C0; font-weight:700; font-size:.8rem; letter-spacing:.06em; text-transform:uppercase; }
.sh-kpi .value { font-size:2.4rem; font-weight:800; color:#0F1E33; line-height:1.15; margin:.25rem 0; }
.sh-kpi .detail { color:#4A5B72; font-size:.95rem; }
.sh-kpi .target { color:#7A8AA0; font-size:.85rem; font-style:italic; margin-top:.2rem; }

/* pipeline */
.sh-flow { display:flex; flex-wrap:wrap; align-items:center; gap:.5rem; margin:.4rem 0 1.4rem 0; }
.sh-step { background:#FFFFFF; border:1px solid #CFE0F6; border-radius:12px; padding:.65rem 1rem; font-weight:600;
           color:#0F1E33; font-size:1rem; }
.sh-step.blue { background:#1565C0; color:#FFFFFF; border-color:#1565C0; }
.sh-step.red { background:#FDECEC; border-color:#F3C2C2; color:#9B1C1C; }
.sh-step.green { background:#E7F6EC; border-color:#BFE5CB; color:#14632F; }
.sh-arrow { color:#7FA6D8; font-weight:800; font-size:1.2rem; }

/* misc */
.sh-section { font-size:1.6rem; font-weight:800; color:#0F1E33; margin: 1.6rem 0 .2rem 0; }
.sh-section-sub { color:#4A5B72; font-size:1.02rem; margin-bottom:.8rem; }
.sh-note { background:#F4F9FF; border:1px solid #D6E4F5; border-radius:12px; padding:.9rem 1.1rem; color:#3A4B62; font-size:.95rem; }
.sh-footer { color:#7A8AA0; font-size:.88rem; border-top:1px solid #E1EBF7; margin-top:2.5rem; padding-top:1rem; }
.sh-pill { display:inline-block; padding:.15rem .6rem; border-radius:999px; font-size:.82rem; font-weight:700; }
.sh-pill.ok { background:#E7F6EC; color:#14632F; } .sh-pill.no { background:#FDECEC; color:#9B1C1C; }

/* bigger, clearer widgets for demos and screenshots */
[data-testid="stMetricValue"] { font-size: 2.1rem; font-weight: 800; color:#0F1E33; }
[data-testid="stMetricLabel"] p { font-size: .95rem; font-weight:600; color:#4A5B72; }
[data-testid="stExpander"] summary p { font-size: 1.02rem; font-weight: 600; }
[data-testid="stJson"] { font-size: 15px !important; }
[data-testid="stSidebar"] [data-testid="stSidebarNavLink"] span { font-size: 1rem; font-weight: 500; }
.stButton > button[kind="primary"] { box-shadow: 0 6px 16px rgba(21,101,192,.25); font-weight:700; padding:.6rem 1.3rem; }
</style>
"""


def inject_css():
    st.markdown(CSS, unsafe_allow_html=True)


def page_header(eyebrow: str, title: str, sub: str = ""):
    st.markdown(f'<div class="sh-eyebrow">{eyebrow}</div><div class="sh-title">{title}</div>'
                + (f'<div class="sh-sub">{sub}</div>' if sub else ""), unsafe_allow_html=True)


def cards(items, cols=3, icon_key="icon"):
    html = "".join(f'<div class="sh-card"><div class="sh-icon">{it.get(icon_key, "")}</div>'
                   f'<h4>{it["title"]}</h4><p>{it["text"]}</p></div>' for it in items)
    st.markdown(f'<div class="sh-grid sh-g{cols}">{html}</div>', unsafe_allow_html=True)


def kpis(items, cols=4):
    html = "".join(f'<div class="sh-card sh-kpi"><div class="label">{k["label"]}</div>'
                   f'<div class="value">{k["value"]}</div><div class="detail">{k["detail"]}</div>'
                   + (f'<div class="target">{k["target"]}</div>' if k.get("target") else "") + "</div>"
                   for k in items)
    st.markdown(f'<div class="sh-grid sh-g{cols}">{html}</div>', unsafe_allow_html=True)


def flow(steps):
    parts = []
    for i, s in enumerate(steps):
        label, cls = (s if isinstance(s, tuple) else (s, ""))
        parts.append(f'<div class="sh-step {cls}">{label}</div>')
        if i < len(steps) - 1:
            parts.append('<div class="sh-arrow">→</div>')
    st.markdown(f'<div class="sh-flow">{"".join(parts)}</div>', unsafe_allow_html=True)


def section(title, sub=""):
    st.markdown(f'<div class="sh-section">{title}</div>' + (f'<div class="sh-section-sub">{sub}</div>' if sub else ""),
                unsafe_allow_html=True)


def footer():
    st.markdown(f'<div class="sh-footer">{APP_NAME} · PS-01 Self-Healing Tool Registries Under Runtime API Schema Drift'
                f' · Team Runtime Terrors · Genesis 2.0, Track EVA 02 · <a href="{REPO_URL}">Source on GitHub</a></div>',
                unsafe_allow_html=True)


def use_llm() -> bool:
    return bool(st.session_state.get("use_llm", False))


def show_trace(trace):
    for i, step in enumerate(trace):
        icon = ":material/check_circle:" if step["ok"] else ":material/cancel:"
        repeat = step["step"].startswith("Attempt") and i > 0
        open_ = (not step["ok"] and not repeat) or "Validator" in step["step"] or "Proposed" in step["step"]
        with st.expander(f"{step['step']}", expanded=open_, icon=icon):
            d = step["detail"]
            if isinstance(d, str):
                st.write(d)
            else:
                st.json(d)


@st.cache_data(show_spinner="Running the 125-case benchmark…")
def cached_benchmark():
    from selfheal.bench import run_benchmark
    return run_benchmark(use_llm=False)


@st.cache_data(show_spinner="Running stress tests…")
def cached_stress():
    from selfheal.bench import run_benchmark
    return run_benchmark(use_llm=False, stress=True)


def headline_numbers():
    """Live numbers from the benchmark, mapped to the PS-01 metric names."""
    import statistics
    from selfheal.bench import rates
    rows = cached_benchmark()
    n = rates(rows)
    n["mttr_ms"] = statistics.median(r["recovery_ms"] for r in rows if r["recovery_ms"] is not None)
    n["tokens"] = sum(r["tokens"] for r in rows)
    return n
