"""Minimal Python SDK for the SchemaHeal middleware API.

    from adaptix.client import SchemaHealClient
    sh = SchemaHealClient("http://localhost:8000", api_key="...")
    sh.register_tool("update_user", base_url="https://api.example.com", path="/users/update",
                     arg_schema={"user_id": "str", "email": "str"})
    tools = sh.openai_tools()                      # pass as `tools=` to a chat-completions request
    messages += sh.handle_tool_calls(response.choices[0].message.tool_calls)
"""
from __future__ import annotations

import json

import httpx


class SchemaHealClient:
    def __init__(self, base_url: str, api_key: str, client: httpx.Client | None = None, timeout: float = 30.0):
        self.http = client or httpx.Client(base_url=base_url, timeout=timeout)
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def _req(self, method: str, path: str, **kw):
        r = self.http.request(method, path, headers=self.headers, **kw)
        r.raise_for_status()
        return r.json()

    def register_tool(self, name: str, base_url: str, path: str, arg_schema: dict, method: str = "POST",
                      description: str = ""):
        return self._req("POST", "/v1/tools", json={"name": name, "base_url": base_url, "path": path,
                                                    "method": method, "arg_schema": arg_schema,
                                                    "description": description})

    def call(self, name: str, arguments: dict, agent_id: str = "agent") -> dict:
        return self._req("POST", f"/v1/tools/{name}/call", json={"arguments": arguments, "agent_id": agent_id})

    def openai_tools(self) -> list[dict]:
        return self._req("GET", "/v1/openai/tools")

    def handle_tool_calls(self, tool_calls, agent_id: str = "agent") -> list[dict]:
        """Accepts OpenAI tool_call objects or dicts; returns role='tool' messages to append."""
        calls = []
        for tc in tool_calls or []:
            if not isinstance(tc, dict):
                tc = {"id": tc.id, "type": "function",
                      "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            calls.append(tc)
        return self._req("POST", "/v1/openai/tool_calls", params={"agent_id": agent_id}, json=calls)

    def pending_repairs(self) -> list[dict]:
        return self._req("GET", "/v1/repairs")

    def approve(self, proposal_id: str) -> dict:
        return self._req("POST", f"/v1/repairs/{proposal_id}/approve")

    def reject(self, proposal_id: str) -> dict:
        return self._req("POST", f"/v1/repairs/{proposal_id}/reject")

    def set_policy(self, tool: str, **overrides) -> dict:
        return self._req("PUT", f"/v1/policies/{tool}", json=overrides)

    def rollback(self, tool: str) -> dict:
        return self._req("POST", f"/v1/tools/{tool}/rollback")
