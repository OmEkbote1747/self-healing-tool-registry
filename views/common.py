"""Shared dashboard plumbing: the API client, theme CSS and small, consistent UI components.

The dashboard never reads the database or calls the engine directly: every view goes through the ADAPTIX
API. By default the API runs in this same process (works on Streamlit Cloud); set ADAPTIX_API_URL and
ADAPTIX_ADMIN_TOKEN to point the dashboard at a separately deployed API instead.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent

STATUS = {   # one consistent vocabulary and colour per outcome
    "recovered": ("Recovered", "#047857", "#ECFDF5"), "ok": ("OK", "#1D4ED8", "#EFF6FF"),
    "stopped": ("Stopped safely", "#B45309", "#FFFBEB"), "rolled_back": ("Rolled back", "#6D28D9", "#F5F3FF"),
    "failed": ("Failed", "#B91C1C", "#FEF2F2"), "open": ("Open", "#475569", "#F1F5F9"),
    "healthy": ("Healthy", "#047857", "#ECFDF5"), "degraded": ("Degraded", "#B45309", "#FFFBEB"),
    # integration / monitoring statuses
    "VERIFIED": ("VERIFIED", "#047857", "#ECFDF5"), "CONFIGURED": ("CONFIGURED", "#1D4ED8", "#EFF6FF"),
    "FAILED": ("FAILED", "#B91C1C", "#FEF2F2"), "ACTION REQUIRED": ("ACTION REQUIRED", "#B45309", "#FFFBEB"),
    "NOT RUN": ("NOT RUN", "#475569", "#F1F5F9"), "UNSUPPORTED": ("UNSUPPORTED", "#6D28D9", "#F5F3FF"),
    "NOT REQUIRED": ("NOT REQUIRED", "#475569", "#F1F5F9"), "YES": ("YES", "#047857", "#ECFDF5"),
    "NO": ("NO", "#B45309", "#FFFBEB"),
    "verified": ("Verified", "#047857", "#ECFDF5"), "unsupported": ("Schema not published", "#6D28D9", "#F5F3FF"),
    "reachable": ("Reachable", "#047857", "#ECFDF5"), "unreachable": ("Unreachable", "#B91C1C", "#FEF2F2"),
    "not_checked": ("Not checked", "#475569", "#F1F5F9"), "not_run": ("Not run", "#475569", "#F1F5F9"),
    "drift": ("Schema drift", "#B45309", "#FFFBEB"), "no_drift": ("No drift", "#047857", "#ECFDF5"),
}
STAGE_ICON = {"request": "send", "classify": "category", "discover": "travel_explore", "diff": "difference",
              "plan": "edit_note", "validate": "verified", "apply": "inventory", "retry": "replay",
              "verify": "fact_check", "rollback": "undo", "outcome": "flag", "credential": "key"}

CSS = """
<style>
.block-container { padding-top: 1.6rem; max-width: 1280px; }
.ax-eyebrow { color:#1D4ED8; font-weight:700; font-size:.78rem; letter-spacing:.08em; text-transform:uppercase; }
.ax-title { font-size:1.75rem; font-weight:750; color:#0F172A; margin:.1rem 0 .2rem 0; }
.ax-sub { color:#475569; font-size:.98rem; margin-bottom:1rem; max-width: 920px; }
.ax-badge { display:inline-block; padding:.12rem .55rem; border-radius:999px; font-size:.78rem; font-weight:650;
            border:1px solid transparent; }
.ax-sim { background:#FEF3C7; color:#92400E; border-color:#FCD34D; }
.ax-card { background:#fff; border:1px solid #DCE3EE; border-radius:12px; padding:1rem 1.1rem; height:100%; }
.ax-kpi .l { color:#475569; font-size:.8rem; font-weight:600; text-transform:uppercase; letter-spacing:.04em; }
.ax-kpi .v { color:#0F172A; font-size:1.9rem; font-weight:750; line-height:1.2; margin:.2rem 0; }
.ax-kpi .d { color:#64748B; font-size:.85rem; }
.ax-grid { display:grid; gap:.8rem; grid-template-columns: repeat(var(--cols), minmax(0,1fr)); margin:.2rem 0 1rem 0; }
@media (max-width: 900px) { .ax-grid { grid-template-columns: repeat(2, minmax(0,1fr)); } }
.ax-step { border-left:3px solid #DCE3EE; padding:.15rem 0 .55rem .9rem; margin-left:.4rem; }
.ax-diff-add { background:#ECFDF5; } .ax-diff-del { background:#FEF2F2; } .ax-diff-chg { background:#FFFBEB; }
[data-testid="stSidebar"] .stCaption, [data-testid="stSidebar"] small { color:#93A4C3 !important; }
</style>
"""


class ApiError(RuntimeError):
    def __init__(self, status: int, detail):
        super().__init__(f"{status}: {detail}")
        self.status, self.detail = status, detail


@st.cache_resource(show_spinner=False)
def _client():
    from adaptix.config import get_settings
    s = get_settings()
    headers = {"Authorization": f"Bearer {s.admin_token}"}
    if s.api_url:
        return httpx.Client(base_url=s.api_url, headers=headers, timeout=120), s.api_url
    from fastapi.testclient import TestClient
    from adaptix.api import create_app
    return TestClient(create_app(), headers=headers), "in-process"


def api(method: str, path: str, **kw):
    client, _ = _client()
    r = client.request(method, path, **kw)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.json())
        except Exception:
            detail = r.text[:200]
        raise ApiError(r.status_code, detail)
    return r.json()


def api_mode() -> str:
    return _client()[1]


def safe_call(fn, *a, **kw):
    """Run an API call, show a readable error state instead of a traceback."""
    try:
        return fn(*a, **kw)
    except ApiError as e:
        d = e.detail.get("detail") if isinstance(e.detail, dict) else e.detail
        st.error(f"The ADAPTIX API returned {e.status}: {d}", icon=":material/error:")
    except httpx.HTTPError as e:
        st.error(f"Could not reach the ADAPTIX API: {type(e).__name__}", icon=":material/cloud_off:")
    return None


def inject_css():
    st.markdown(CSS, unsafe_allow_html=True)


def header(eyebrow: str, title: str, sub: str = ""):
    st.markdown(f'<div class="ax-eyebrow">{eyebrow}</div><div class="ax-title">{title}</div>'
                + (f'<div class="ax-sub">{sub}</div>' if sub else ""), unsafe_allow_html=True)


def badge(status: str) -> str:
    label, fg, bg = STATUS.get(status, (status.replace("_", " ").title(), "#334155", "#F1F5F9"))
    return f'<span class="ax-badge" style="color:{fg};background:{bg};border-color:{fg}33">{label}</span>'


SIM_BADGE = '<span class="ax-badge ax-sim">SIMULATED</span>'
REAL_BADGE = '<span class="ax-badge" style="background:#ECFDF5;color:#065F46;border-color:#6EE7B7">REAL API</span>'
BASELINE_BADGE = '<span class="ax-badge" style="background:#E2E8F0;color:#334155">BASELINE · repair off</span>'


def kpis(items: list[tuple], cols: int = 4):
    html = "".join(f'<div class="ax-card ax-kpi"><div class="l">{l}</div><div class="v">{v}</div>'
                   f'<div class="d">{d}</div></div>' for l, v, d in items)
    st.markdown(f'<div class="ax-grid" style="--cols:{cols}">{html}</div>', unsafe_allow_html=True)


def fmt_pct(v) -> str:
    return "—" if v is None else f"{v:.0f}%"


def fmt_ms(v) -> str:
    return "—" if v is None else (f"{v:.0f} ms" if v < 1000 else f"{v / 1000:.2f} s")


def go(page_key: str, **state):
    for k, v in state.items():
        st.session_state[k] = v
    st.switch_page(st.session_state["_pages"][page_key])


def empty(msg: str, action_label: str | None = None, page_key: str | None = None):
    st.info(msg, icon=":material/info:")
    if action_label and page_key and st.button(action_label, key=f"empty-{page_key}-{action_label}"):
        go(page_key)
