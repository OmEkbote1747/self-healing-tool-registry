"""Plug SchemaHeal into an OpenAI-compatible tool-calling loop.

The model never talks to your APIs directly: it gets tool definitions from SchemaHeal and every
tool call it makes is executed (and, where safe, repaired) by SchemaHeal.

    SCHEMAHEAL_URL=http://localhost:8000 SCHEMAHEAL_KEY=... python examples/openai_agent_loop.py
"""
import os

from adaptix.client import SchemaHealClient

sh = SchemaHealClient(os.environ.get("SCHEMAHEAL_URL", "http://localhost:8000"), os.environ["SCHEMAHEAL_KEY"])
tools = sh.openai_tools()


def agent_turn(llm_client, model: str, messages: list[dict]) -> list[dict]:
    """One turn with any OpenAI-compatible chat-completions client."""
    response = llm_client.chat.completions.create(model=model, messages=messages, tools=tools)
    msg = response.choices[0].message
    messages.append(msg.model_dump(exclude_none=True))
    if msg.tool_calls:
        # SchemaHeal runs each call; results come back as role="tool" messages. A result with
        # status "awaiting_approval" means a reviewer must approve a repair before retrying.
        messages += sh.handle_tool_calls(msg.tool_calls, agent_id="support-bot")
    return messages


if __name__ == "__main__":
    print("Tools exposed to the model:")
    for t in tools:
        print(" -", t["function"]["name"], t["function"]["parameters"]["required"])
