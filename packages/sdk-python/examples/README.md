# Wayforth Examples

## Research Agent

Demonstrates the core Wayforth workflow: discover → pay → use.

```bash
pip install wayforth-sdk
python research_agent.py "What are the best vector databases for RAG?"
```

**What it shows:**
- Natural language service discovery across 2,500+ services
- WayforthRank scoring — best service rises to top automatically
- WayforthQL structured queries
- Credits-based payment via wayforth_pay()
- The full search → pay attribution loop via query_id

No API keys needed to run the discovery. Payment uses Wayforth credits (get 2,000 free at wayforth.io/dashboard).
