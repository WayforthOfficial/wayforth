"""wayforthrank.py — WRI (Wayforth Reliability Index) scoring."""


def compute_wri(service: dict, rank_score: float, popularity_boost: float = 0.0, payment_boost: float = 0.0) -> float:
    """WRI v2 — composite reliability score with popularity and payment signals. Range: 0-100."""
    score = rank_score * 0.5
    tier = service.get("coverage_tier", 0)
    if tier >= 2:
        score += 20
    elif tier >= 1:
        score += 5
    last_tested = service.get("last_tested_at")
    if last_tested:
        from datetime import datetime, timezone, timedelta
        try:
            if isinstance(last_tested, str):
                from dateutil.parser import parse
                last_tested = parse(last_tested)
            if last_tested.tzinfo is None:
                last_tested = last_tested.replace(tzinfo=timezone.utc)
            if last_tested > datetime.now(timezone.utc) - timedelta(hours=24):
                score += 10
        except Exception:
            pass  # non-critical: date parse failure skips the recency bonus
    if service.get("consecutive_failures", 1) == 0:
        score += 10
    score += min(popularity_boost, 5.0)
    score += min(payment_boost, 8.0)
    return round(min(score, 100), 1)
