import os

import pytest

# Tests read configuration only from environment variables, never from a local .streamlit/secrets.toml
os.environ["ADAPTIX_IGNORE_STREAMLIT_SECRETS"] = "1"

from adaptix.config import Settings
from adaptix.credentials import CredentialRegistry
from adaptix.engine import Engine
from adaptix.llm import NoProvider
from adaptix.storage import Storage


@pytest.fixture
def make_engine(tmp_path):
    """SIMULATION-mode engine with a fresh database, no real network and no real sleeping."""
    def _make(llm=None, creds=None, sleeps=None, **settings):
        sl = sleeps if sleeps is not None else []
        return Engine(Storage(tmp_path / f"db{len(list(tmp_path.iterdir()))}.db", mode="simulation"),
                      creds or CredentialRegistry([]), llm or NoProvider(), Settings(**settings), sleep=sl.append)
    return _make


@pytest.fixture
def engine(make_engine):
    return make_engine()
