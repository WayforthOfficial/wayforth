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


async def rank_services_local(intent: str, services: list[dict]) -> list[dict]:
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
            result.append(s_copy)

        return sorted(result, key=lambda x: x["score"], reverse=True)
    except Exception as exc:
        logger.error("[ranker] Haiku ranking failed: %r\n%s", exc, traceback.format_exc())
        return _keyword_rank(intent, candidates)
