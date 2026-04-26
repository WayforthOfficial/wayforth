import json
import logging
import os
import traceback
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

_client: AsyncAnthropic | None = None

_SYSTEM = (
    "You are a service ranker for AI agents. Given an agent's intent and a list of "
    "services, return a JSON array of the same services ranked by relevance, each with "
    "an added 'score' (0-100) and 'reason' (one sentence). "
    "Return ONLY a JSON array, no explanation, no markdown, no code fences."
)


def _get_client() -> AsyncAnthropic | None:
    key = os.getenv("ANTHROPIC_API_KEY")
    logger.info("[ranker] ANTHROPIC_API_KEY present=%s", bool(key))
    if not key:
        return None
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=key)
    return _client


def _keyword_rank(intent: str, services: list[dict]) -> list[dict]:
    tokens = [w.lower() for w in intent.split() if len(w) > 2]

    def _score(s: dict) -> int:
        haystack = f"{s.get('name', '')} {s.get('description', '')}".lower()
        return sum(1 for t in tokens if t in haystack)

    ranked = sorted(services, key=_score, reverse=True)
    for s in ranked:
        s["score"] = _score(s) * 10
        s["reason"] = "keyword match"
    return ranked


_HAIKU_CANDIDATE_LIMIT = 20  # pre-filter before sending to Haiku


def compute_wri(service: dict, rank_score: float, popularity_boost: float = 0.0, payment_boost: float = 0.0) -> float:
    """
    Wayforth Reliability Index (WRI) — composite service quality score.
    Combines semantic relevance with reliability and usage signals.

    Production scoring is handled by the WayforthRank private service (RANK_SERVICE_URL).
    This local implementation is a reference fallback only.

    Range: 0-100. Higher = more trustworthy for agent use.
    """
    # Base: semantic relevance (primary signal)
    score = rank_score * 0.5

    # Reliability: coverage tier bonus
    tier = service.get("coverage_tier", 0)
    tier_bonus = {2: 20, 1: 5}.get(min(tier, 2), 0)  # Tier 3+ capped at Tier 2 bonus
    score += tier_bonus

    # Freshness: recent probe signal
    last_tested = service.get("last_tested_at")
    if last_tested:
        try:
            from datetime import datetime, timezone, timedelta
            from dateutil.parser import parse as parse_date
            if isinstance(last_tested, str):
                last_tested = parse_date(last_tested)
            if last_tested.tzinfo is None:
                last_tested = last_tested.replace(tzinfo=timezone.utc)
            if last_tested > datetime.now(timezone.utc) - timedelta(hours=24):
                score += 10
        except Exception:
            pass

    # Stability: consecutive failure signal
    if service.get("consecutive_failures", 1) == 0:
        score += 10

    # Protocol: institutional backing signal
    if service.get("payment_protocol") == "x402":
        score += 5

    # Usage: popularity signal (from search_analytics)
    score += min(popularity_boost, 5.0)
    # Conversion: payment signal (from search_outcomes, max +8)
    score += min(payment_boost, 8.0)

    return round(min(score, 100), 1)


async def rank_services_local(intent: str, services: list[dict], popularity_signals: dict = None, payment_signals: dict = None) -> list[dict]:
    """Rank services by semantic relevance using Claude Haiku; falls back to keyword ranking."""
    client = _get_client()
    if not client or not services:
        return _keyword_rank(intent, list(services))

    # Pre-filter: keyword rank → top N candidates so Haiku output fits in token budget
    candidates = _keyword_rank(intent, [dict(s) for s in services])[:_HAIKU_CANDIDATE_LIMIT]

    try:
        slim = [
            {"name": s.get("name"), "description": s.get("description"), "category": s.get("category")}
            for s in candidates
        ]
        msg = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=2048,
            system=_SYSTEM,
            messages=[{"role": "user", "content": f"Intent: {intent}\n\nServices:\n{json.dumps(slim)}"}],
        )
        text = msg.content[0].text
        logger.info("[ranker] Haiku raw: %r", text[:500])
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        ranked_slim = json.loads(text)
        name_to_meta = {item["name"]: item for item in ranked_slim}

        result = []
        for s in candidates:
            s_copy = dict(s)
            meta = name_to_meta.get(s.get("name"), {})
            s_copy["score"] = int(meta.get("score", 0))
            s_copy["reason"] = meta.get("reason", "")
            svc_id = str(s_copy.get('id', ''))
            boost = (popularity_signals or {}).get(svc_id, 0.0)
            pay_boost = (payment_signals or {}).get(svc_id, 0.0)
            s_copy['wri'] = compute_wri(s_copy, s_copy["score"], popularity_boost=boost, payment_boost=pay_boost)
            result.append(s_copy)

        return sorted(result, key=lambda x: x["score"], reverse=True)
    except Exception as exc:
        logger.error("[ranker] Haiku ranking failed: %r\n%s", exc, traceback.format_exc())
        return _keyword_rank(intent, candidates)
