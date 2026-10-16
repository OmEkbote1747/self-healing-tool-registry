"""Smoke test: every page of the Streamlit app renders without an exception."""
import pytest
from streamlit.testing.v1 import AppTest

PAGES = ["home", "how", "demo", "bench", "stress", "registry"]


@pytest.mark.parametrize("page", PAGES)
def test_page_renders(page):
    at = AppTest.from_string(f'''
import runpy, streamlit as st
st.session_state["_force_page"] = "{page}"
runpy.run_path("app.py", run_name="__main__")
''', default_timeout=60)
    at.run()
    assert not at.exception, at.exception


def test_demo_hold_then_approve():
    at = AppTest.from_string('''
import runpy, streamlit as st
st.session_state["_force_page"] = "demo"
runpy.run_path("app.py", run_name="__main__")
''', default_timeout=60)
    at.run()
    next(b for b in at.button if b.label.startswith("Change the API")).click().run()
    assert not at.exception
    assert any(m.value == "Needs approval" for m in at.metric)
    next(b for b in at.button if b.label.startswith("Approve and retry")).click().run()
    assert not at.exception
    assert any("Approved. Retried with tool v2: success" in s.value for s in at.success)
