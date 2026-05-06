import math
from datetime import datetime, timezone


def payment_rate_score(payments: int, total_clicks: int) -> float:
    """0 clicks → neutral 50; else normalize conversion rate to 0–100."""
    if total_clicks == 0:
        return 50.0
    return min((payments / total_clicks) * 100.0, 100.0)


def volume_score(total_payments: int) -> float:
    """Log-normalize payment count to 0–100. log(101) ≈ 4.615 as ceiling."""
    if total_payments == 0:
        return 0.0
    return min(math.log(total_payments + 1) / math.log(101) * 100.0, 100.0)


def recency_score(last_seen: datetime | None) -> float:
    """≤7 days → 100, ≤30 days → 70, older → 40, never → 20."""
    if last_seen is None:
        return 20.0
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    delta = (datetime.now(timezone.utc) - last_seen).days
    if delta <= 7:
        return 100.0
    if delta <= 30:
        return 70.0
    return 40.0


def compute_wri_v2(
    base_wri: float,
    payments: int,
    total_clicks: int,
    last_seen: datetime | None,
) -> float:
    """WayforthRank v2: payment-signal weighted composite score (0–100).

    Weights:
      40% base_wri (existing heuristic score)
      35% payment conversion rate (payments / total_clicks, normalized 0–100)
      15% execution volume (log-normalized payment count)
      10% recency (last 7d=100, 30d=70, older=40, never=20)
    """
    score = (
        base_wri * 0.40
        + payment_rate_score(payments, total_clicks) * 0.35
        + volume_score(payments) * 0.15
        + recency_score(last_seen) * 0.10
    )
    return round(min(score, 100.0), 1)
