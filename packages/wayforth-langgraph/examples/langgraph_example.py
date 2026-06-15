"""
LangGraph end-to-end example — Wayforth tool drop-in.

Demonstrates a ReAct agent completing a real task (news search) entirely
through Wayforth's managed proxy: discovery at runtime, native response,
failover headers captured automatically.

Usage:
    pip install wayforth-langgraph langgraph langchain-anthropic
    export WAYFORTH_API_KEY=wf_live_...
    export ANTHROPIC_API_KEY=sk-ant-...
    python langgraph_example.py
"""
import json
import os
import sys

from langchain_anthropic import ChatAnthropic
from langgraph.prebuilt import create_react_agent

# Import the Wayforth drop-in — one import, one init
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from wayforth_langgraph import WayforthTool

WAYFORTH_KEY  = os.environ.get("WAYFORTH_API_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
if not WAYFORTH_KEY:
    sys.exit("Set WAYFORTH_API_KEY")
if not ANTHROPIC_KEY:
    sys.exit("Set ANTHROPIC_API_KEY")

def main() -> None:
    # ── Init ────────────────────────────────────────────────────────────────
    wayforth = WayforthTool(api_key=WAYFORTH_KEY)
    llm      = ChatAnthropic(model="claude-haiku-4-5-20251001", api_key=ANTHROPIC_KEY)
    agent    = create_react_agent(llm, tools=[wayforth])

    task = (
        "Find the top 3 recent news stories about AI agent frameworks. "
        "Use the wayforth tool to search, then summarise each result in one sentence."
    )
    print(f"Task: {task}\n{'─' * 60}")

    # ── Run ─────────────────────────────────────────────────────────────────
    state = agent.invoke({"messages": [("user", task)]})

    # ── Print tool calls and final answer ────────────────────────────────────
    for msg in state["messages"]:
        role = getattr(msg, "type", type(msg).__name__)

        if role == "tool":
            print(f"\n[tool call result]")
            try:
                data = json.loads(msg.content)
                print(f"  service  : {data.get('service')}")
                print(f"  wri      : {data.get('wri')}")
                print(f"  failover : {data.get('failover')}")
                print(f"  credits  : {data.get('credits')} remaining")
                if data.get("failover") == "true":
                    print(f"  routed   : {data.get('original_service')} → {data.get('routed_to')}")
                    print(f"  reason   : {data.get('reason')}")
                result = data.get("result", {})
                organic = result.get("organic") or result.get("results") or []
                print(f"  results  : {len(organic)} items returned")
            except (json.JSONDecodeError, AttributeError):
                print(f"  {str(msg.content)[:200]}")

        elif role == "ai":
            content = getattr(msg, "content", "")
            if isinstance(content, str) and content.strip():
                print(f"\n[agent answer]\n{content}")
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        print(f"\n[agent answer]\n{block['text']}")

    print("\n✓ end-to-end run complete")


if __name__ == "__main__":
    main()
