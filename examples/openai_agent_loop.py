"""Plug ADAPTIX into an OpenAI-compatible tool-calling loop.

The model never calls your APIs directly: it gets tool definitions from ADAPTIX, and every tool call it
makes is executed by ADAPTIX, which repairs supported schema drift on the way and stops (with a reason)
on anything unsafe.

    ADAPTIX_API_URL=http://localhost:8000 ADAPTIX_ADMIN_TOKEN=... python examples/openai_agent_loop.py
"""
import os

import httpx

api = httpx.Client(base_url=os.environ.get("ADAPTIX_API_URL", "http://localhost:8000"),
                   headers={"Authorization": f"Bearer {os.environ['ADAPTIX_ADMIN_TOKEN']}"}, timeout=60)


def adaptix_tools() -> list[dict]:
    return api.get("/v1/openai/tools").raise_for_status().json()


def run_tool_calls(tool_calls) -> list[dict]:
    """Execute the model's tool calls through ADAPTIX; returns role="tool" messages."""
    calls = [{"id": tc.id, "type": "function",
              "function": {"name": tc.function.name, "arguments": tc.function.arguments}} for tc in tool_calls]
    return api.post("/v1/openai/tool_calls", json=calls).raise_for_status().json()


def agent_turn(llm_client, model: str, messages: list[dict], tools: list[dict]) -> list[dict]:
    """One turn with any OpenAI-compatible chat-completions client."""
    msg = llm_client.chat.completions.create(model=model, messages=messages, tools=tools).choices[0].message
    messages.append(msg.model_dump(exclude_none=True))
    if msg.tool_calls:
        # Each result carries status "ok" / "recovered" (repaired automatically) or "stopped" with a reason.
        messages += run_tool_calls(msg.tool_calls)
    return messages


if __name__ == "__main__":
    print("Tools exposed to the model:")
    for t in adaptix_tools():
        print(" -", t["function"]["name"], "·", t["function"]["description"])
