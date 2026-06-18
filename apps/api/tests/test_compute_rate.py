"""compute_credits_for_run — 1.5 credits/min, ceil to whole minutes, min 1.

Credits are integers, so the 1.5×minutes product is rounded up (never undercharge).
Run: uv run pytest tests/test_compute_rate.py -v
"""
import pytest

from services.sandbox import compute_credits_for_run


@pytest.mark.parametrize("duration_ms,expected", [
    (0,        1),   # zero/degenerate → 1-credit floor
    (1,        2),   # <1 min → ceil(1 min) × 1.5 = ceil(1.5) = 2
    (3_600,    2),   # ~3.6s typical run → 2
    (59_999,   2),   # just under 1 min → 2
    (60_000,   2),   # exactly 1 min → 2
    (60_001,   3),   # just over 1 min → 2 min × 1.5 = 3
    (120_000,  3),   # 2 min → 3
    (120_001,  5),   # 3 min → ceil(4.5) = 5
    (180_000,  5),   # 3 min → 5
    (240_000,  6),   # 4 min → 6
])
def test_compute_credits(duration_ms, expected):
    assert compute_credits_for_run(duration_ms) == expected


def test_never_below_one():
    assert compute_credits_for_run(0) >= 1


def test_rate_is_1_5_per_whole_minute():
    # For even whole-minute counts the product is exact: N min → 1.5*N.
    assert compute_credits_for_run(120_000) == 3      # 2 × 1.5
    assert compute_credits_for_run(240_000) == 6      # 4 × 1.5
    # Old rate was 1/min — confirm we increased it.
    assert compute_credits_for_run(60_000) > 1
