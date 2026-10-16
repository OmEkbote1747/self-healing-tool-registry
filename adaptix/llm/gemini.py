"""Real Gemini inference via Google's official google-genai SDK (verified against google-genai 2.31.0:
Client(api_key=..., http_options=HttpOptions(timeout=<ms>, retry_options=HttpRetryOptions(attempts=1))),
client.models.generate_content(model, contents, config=GenerateContentConfig(system_instruction,
response_mime_type="application/json", response_json_schema, temperature, max_output_tokens)),
response.text, response.usage_metadata.{prompt_token_count, candidates_token_count},
errors.ClientError / errors.ServerError with .code).

Keys come only from the credential registry (group "gemini", scope "llm"). A rejected key is never edited or
retried; failover to another configured key happens only for the categories the operator allowed.
"""
from __future__ import annotations

import random
import threading
import time

from pydantic import ValidationError

from ..credentials import CredentialError, CredentialRegistry
from ..models import LLMRepairPlan, now_iso
from ..security import scrub
from . import SYSTEM_INSTRUCTION, LLMResult, build_prompt

PERMANENT = {"credential_invalid", "credential_expired", "permission_denied", "model_not_found", "bad_request",
             "refused", "credential_unavailable", "not_permitted", "missing_configuration"}


def classify_gemini_error(exc) -> str:
    from google.genai import errors
    msg = str(getattr(exc, "message", "") or exc).lower()
    if isinstance(exc, errors.ClientError):
        code = getattr(exc, "code", None)
        if code == 429:
            return "rate_limited"
        if code in (401,) or "api key not valid" in msg or "api_key_invalid" in msg:
            return "credential_invalid"
        if "expired" in msg:
            return "credential_expired"
        if code == 403:
            return "permission_denied"
        if code == 404:
            return "model_not_found"
        return "bad_request"
    if isinstance(exc, errors.ServerError):
        return "provider_outage"
    name = type(exc).__name__.lower()
    if "timeout" in name or "timed out" in msg:
        return "timeout"
    if "connect" in name or "network" in msg:
        return "network"
    return "unknown"


class GeminiProvider:
    name, real = "gemini", True

    def __init__(self, creds: CredentialRegistry, model: str, timeout_s: float = 20.0, max_retries: int = 2,
                 client_factory=None, sleep=time.sleep):
        self.creds, self.model, self.timeout_s, self.max_retries = creds, model, timeout_s, max_retries
        self._factory = client_factory           # tests inject a fake SDK client; production uses the real one
        self._sleep = sleep
        self._lock = threading.Lock()
        self.last: dict = {}                    # safe metadata of the last real request
        self.last_success_at: str | None = None
        # One long-lived SDK client per credential. A client created inline and dropped is garbage-collected
        # mid-request, which closes its HTTP connection ("client has been closed").
        self._clients: dict[tuple[str, int], object] = {}

    def _client_for(self, alias: str, key: str):
        with self._lock:
            ck = (alias, hash(key))
            if ck not in self._clients:
                self._clients = {k: v for k, v in self._clients.items() if k[0] != alias}   # key was rotated
                self._clients[ck] = self._client(key)
            return self._clients[ck]

    def configured(self) -> bool:
        return any(c["configured"] and c["state"] != "missing" for c in self.creds.status() if c["group"] == "gemini")

    def _client(self, key: str):
        if self._factory:
            return self._factory(key)
        from google import genai
        from google.genai import types
        return genai.Client(api_key=key, http_options=types.HttpOptions(
            timeout=int(self.timeout_s * 1000), retry_options=types.HttpRetryOptions(attempts=1)))

    def _config(self, json_output: bool, max_tokens: int):
        from google.genai import types
        kw = dict(system_instruction=SYSTEM_INSTRUCTION, temperature=0.0, max_output_tokens=max_tokens)
        if json_output:
            kw.update(response_mime_type="application/json", response_json_schema=LLMRepairPlan.model_json_schema())
        return types.GenerateContentConfig(**kw)

    def _request(self, contents: str, json_output: bool, max_tokens: int) -> LLMResult:
        tried: set[str] = set()
        attempts, t_start = 0, time.perf_counter()
        last_cat, last_err, alias = None, None, None
        while attempts <= self.max_retries:
            try:
                cred, key = self.creds.select("gemini", "llm", exclude=tried)
            except CredentialError as e:
                return self._result(False, t_start, attempts, e.category if e.category != "no_valid_credential"
                                    else (last_cat or "credential_unavailable"), str(e), alias)
            alias = cred.alias
            attempts += 1
            try:
                client = self._client_for(cred.alias, key)
                resp = client.models.generate_content(
                    model=self.model, contents=contents, config=self._config(json_output, max_tokens))
            except Exception as exc:                              # categorised, never re-raised with the key
                last_cat, last_err = classify_gemini_error(exc), scrub(str(exc))[:300]
                if last_cat in ("credential_invalid", "credential_expired"):
                    self.creds.report(cred.alias, last_cat)
                    alt = self.creds.failover(cred, last_cat, "gemini", "llm", tried | {cred.alias})
                    tried.add(cred.alias)
                    if alt is None:
                        return self._result(False, t_start, attempts, last_cat, last_err, alias)
                    continue
                if last_cat == "rate_limited":
                    self.creds.report(cred.alias, "rate_limited")
                    tried.add(cred.alias)
                    if self.creds.failover(cred, last_cat, "gemini", "llm", tried) is None:
                        self._backoff(attempts)
                        tried.discard(cred.alias)
                    continue
                if last_cat in PERMANENT:
                    return self._result(False, t_start, attempts, last_cat, last_err, alias)
                self._backoff(attempts)                          # timeout / outage / network: bounded retry
                continue
            self.creds.report(cred.alias, "ok")
            usage = getattr(resp, "usage_metadata", None)
            pt = getattr(usage, "prompt_token_count", None) if usage else None
            ot = getattr(usage, "candidates_token_count", None) if usage else None
            text = getattr(resp, "text", None)
            if not text:
                return self._result(False, t_start, attempts, "refused", "the model returned no usable text",
                                    alias, pt, ot)
            if not json_output:
                return self._result(True, t_start, attempts, None, None, alias, pt, ot)
            try:
                plan = LLMRepairPlan.model_validate_json(text)
            except ValidationError as e:
                last_cat, last_err = "malformed_output", scrub(str(e))[:300]
                continue                                          # one bounded retry on malformed output
            return self._result(True, t_start, attempts, None, None, alias, pt, ot, plan)
        return self._result(False, t_start, attempts, last_cat or "unknown", last_err, alias)

    def _backoff(self, attempt: int):
        self._sleep(min(4.0, 0.5 * 2 ** (attempt - 1)) + random.uniform(0, 0.25))

    def _result(self, ok, t0, attempts, cat, err, alias, pt=None, ot=None, plan=None) -> LLMResult:
        r = LLMResult(provider=self.name, model=self.model, ok=ok, real=attempts > 0, plan=plan, error_category=cat,
                      error=scrub(err) if err else None, duration_ms=round((time.perf_counter() - t0) * 1000, 1),
                      prompt_tokens=pt, output_tokens=ot, credential_alias=alias, attempts=attempts, ts=now_iso())
        with self._lock:
            self.last = r.meta()
            if ok:
                self.last_success_at = r.ts
        return r

    def plan(self, context: dict) -> LLMResult:
        return self._request(build_prompt(context), json_output=True, max_tokens=2048)

    def check(self) -> LLMResult:
        """A minimal real request. 'verified' is reported only after one of these (or a plan call) succeeds."""
        return self._request("Reply with the single word OK.", json_output=False, max_tokens=64)

    def status(self) -> dict:
        configured = self.configured()
        if not configured:
            conn = "not_configured"
        elif self.last_success_at:
            conn = "verified"
        elif self.last:
            conn = "failing"
        else:
            conn = "configured_unverified"
        return {"provider": self.name, "model": self.model, "configured": configured, "connectivity": conn,
                "last_success_at": self.last_success_at,
                "last_error_category": self.last.get("error_category") if self.last else None,
                "credentials": [c for c in self.creds.status() if c["group"] == "gemini"]}
