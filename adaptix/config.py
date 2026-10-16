"""Central configuration. Every value comes from an environment variable (or the hosting platform's
secret store for secrets) with a documented default. Secrets are never stored on this object."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from functools import lru_cache


def env(name: str, default: str | None = None) -> str | None:
    """Environment first, then Streamlit secrets (Streamlit Cloud), else default."""
    if os.environ.get(name) not in (None, ""):
        return os.environ[name]
    try:
        import streamlit as st
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return default


def _int(name, default):
    return int(env(name, str(default)))


def _float(name, default):
    return float(env(name, str(default)))


@dataclass(frozen=True)
class Settings:
    app_env: str = "development"
    db_path: str = "data/adaptix.db"             # production (real APIs) records
    sim_db_path: str = "data/adaptix-sim.db"     # Simulation Lab records: a separate database file
    test_db_path: str = "data/adaptix-test.db"   # REAL_API_TEST (controlled test service) records
    # LLM
    llm_provider: str = "gemini"                 # gemini | mock | none
    gemini_model: str = "gemini-3.5-flash-lite"  # Google's recommended fast/cost-efficient model (docs, 2026-10)
    llm_timeout_s: float = 20.0
    llm_max_retries: int = 2
    llm_min_confidence: float = 0.8
    # repair engine
    max_repair_attempts: int = 2
    max_transport_retries: int = 2
    request_timeout_s: float = 10.0
    total_time_budget_s: float = 30.0
    backoff_base_s: float = 0.2
    backoff_max_s: float = 4.0
    auto_min_similarity: float = 75.0
    # API service
    admin_token: str = field(default="", repr=False)
    cors_origins: tuple[str, ...] = ()
    max_body_bytes: int = 256_000
    api_url: str | None = None                   # set to use a remote ADAPTIX API from the dashboard
    retention_days: int = 30
    # real API egress (SSRF protection) and monitoring
    allowed_hosts: tuple[str, ...] = ()          # exact hosts or "*.example.com"; empty = no real egress
    allow_http: bool = False                     # plain http only if explicitly enabled
    max_redirects: int = 3
    max_schema_bytes: int = 5_000_000
    host_min_interval_s: float = 1.0             # politeness: at most one request per host per interval
    monitor_enabled: bool = True
    monitor_interval_s: int = 3600               # default schema-check interval per real tool
    monitor_min_interval_s: int = 300            # floor for any configured interval


def _hosts(raw: str | None) -> tuple[str, ...]:
    return tuple(h.strip().lower().rstrip(".") for h in (raw or "").split(",") if h.strip())


def _bool(name, default: bool) -> bool:
    return (env(name, "true" if default else "false") or "").strip().lower() in ("1", "true", "yes", "on")


@lru_cache
def get_settings() -> Settings:
    admin = env("ADAPTIX_ADMIN_TOKEN") or ("dev-" + secrets.token_urlsafe(24))  # random per process if unset
    return Settings(
        app_env=env("APP_ENV", "development"),
        db_path=env("ADAPTIX_DB_PATH", "data/adaptix.db"),
        sim_db_path=env("ADAPTIX_SIM_DB_PATH", "data/adaptix-sim.db"),
        test_db_path=env("ADAPTIX_TEST_DB_PATH", "data/adaptix-test.db"),
        llm_provider=(env("LLM_PROVIDER", "gemini") or "gemini").lower(),
        gemini_model=env("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        llm_timeout_s=_float("LLM_TIMEOUT_SECONDS", 20.0),
        llm_max_retries=_int("LLM_MAX_RETRIES", 2),
        llm_min_confidence=_float("LLM_MIN_CONFIDENCE", 0.8),
        max_repair_attempts=_int("ADAPTIX_MAX_REPAIR_ATTEMPTS", 2),
        max_transport_retries=_int("ADAPTIX_MAX_RETRIES", 2),
        request_timeout_s=_float("ADAPTIX_REQUEST_TIMEOUT_SECONDS", 10.0),
        total_time_budget_s=_float("ADAPTIX_TIME_BUDGET_SECONDS", 30.0),
        backoff_base_s=_float("ADAPTIX_BACKOFF_BASE_SECONDS", 0.2),
        backoff_max_s=_float("ADAPTIX_BACKOFF_MAX_SECONDS", 4.0),
        auto_min_similarity=_float("ADAPTIX_AUTO_MIN_SIMILARITY", 75.0),
        admin_token=admin,
        cors_origins=tuple(o.strip() for o in (env("ADAPTIX_CORS_ORIGINS", "") or "").split(",") if o.strip()),
        max_body_bytes=_int("ADAPTIX_MAX_BODY_BYTES", 256_000),
        api_url=env("ADAPTIX_API_URL"),
        retention_days=_int("ADAPTIX_RETENTION_DAYS", 30),
        allowed_hosts=_hosts(env("ADAPTIX_ALLOWED_HOSTS")),
        allow_http=_bool("ADAPTIX_ALLOW_HTTP", False),
        max_redirects=_int("ADAPTIX_MAX_REDIRECTS", 3),
        max_schema_bytes=_int("ADAPTIX_MAX_SCHEMA_BYTES", 5_000_000),
        host_min_interval_s=_float("ADAPTIX_HOST_MIN_INTERVAL_SECONDS", 1.0),
        monitor_enabled=_bool("ADAPTIX_MONITOR_ENABLED", True),
        monitor_interval_s=max(_int("ADAPTIX_MONITOR_INTERVAL_SECONDS", 3600), 300),
    )
