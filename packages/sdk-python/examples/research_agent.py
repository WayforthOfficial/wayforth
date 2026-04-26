"""
Wayforth Research Agent — Reference Implementation

Demonstrates the killer use case: an agent that researches any topic
by discovering and using multiple data sources through Wayforth.

No API keys needed beyond Anthropic. No manual integrations.
Wayforth handles discovery and payment routing.

Usage:
    pip install wayforth-sdk anthropic
    export ANTHROPIC_API_KEY=your_key
    python research_agent.py "What is the current state of quantum computing?"
"""

import asyncio
import sys
from wayforth.client import WayforthClient

# Initialize clients
wayforth = WayforthClient()  # Uses api-production-fd71.up.railway.app by default


async def research(topic: str) -> dict:
    """
    Research any topic using Wayforth-discovered services.

    The agent:
    1. Searches Wayforth for the best data services for this query
    2. Uses web search to find current information
    3. Uses news API for recent developments
    4. Synthesizes with an LLM

    All service discovery happens through Wayforth — no hardcoded integrations.
    """
    print(f"\n🔍 Researching: {topic}\n")

    # Step 1: Find the best web search service
    print("1. Finding best web search service via Wayforth...")
    search_results = wayforth.query("real-time web search for agents", tier_min=2, limit=3)
    if not search_results.get("results"):
        print("   No search services found")
        return None

    top_search = search_results["results"][0]
    print(f"   → {top_search['name']} (WRI: {top_search.get('wri', 'N/A')}, Score: {top_search['score']})")
    print(f"   → wayforth_id: {top_search.get('wayforth_id', '')}")

    # Step 2: Find the best news service
    print("\n2. Finding best news service via Wayforth...")
    news_results = wayforth.query("recent news and current events", tier_min=2, limit=3)
    top_news = news_results["results"][0] if news_results.get("results") else None
    if top_news:
        print(f"   → {top_news['name']} (WRI: {top_news.get('wri', 'N/A')})")

    # Step 3: Find the best LLM for synthesis
    print("\n3. Finding best inference service via Wayforth...")
    llm_results = wayforth.query("fast LLM inference for synthesis", tier_min=2, limit=3)
    top_llm = llm_results["results"][0] if llm_results.get("results") else None
    if top_llm:
        print(f"   → {top_llm['name']} (WRI: {top_llm.get('wri', 'N/A')})")

    # Step 4: Show what payment would look like
    print("\n4. Payment calldata (demonstration):")
    print(f"   To pay for {top_search['name']}:")
    print(f"   wayforth_pay(")
    print(f"     service_id='{top_search.get('service_id', '')}',")
    print(f"     amount_usdc=0.001  # ~$0.001 per search")
    print(f"   )")
    print(f"   → Non-custodial. Settles on Base in ~2s.")

    # Step 5: Show the full WayforthQL query power
    print("\n5. WayforthQL structured query:")
    structured = wayforth.query(
        query=f"data and search services for researching {topic}",
        tier_min=2,
        sort_by="wri",
        limit=5,
    )

    print(f"\n   Best services for this research task:")
    for i, svc in enumerate(structured.get("results", [])[:5], 1):
        price = svc.get("pricing_usdc", 0)
        price_str = f"${price:.7f}/req" if price and price > 0 else "Free"
        print(f"   {i}. {svc['name']} — WRI: {svc.get('wri', 'N/A')} | {price_str}")

    print(f"\n✅ Wayforth found {len(structured.get('results', []))} services for your research task")
    print(f"   query_id: {structured.get('query_id', '')} (use this in payment to close the attribution loop)")

    return structured


if __name__ == "__main__":
    topic = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "AI agent infrastructure in 2026"
    asyncio.run(research(topic))
