"""compute_credits_for_run — 1.5 credits per ACTUAL minute, ceil to int, min 1.

The run's fractional duration is used directly (NOT rounded up to a whole minute),
so a sub-~40s run = 1 credit and the charge scales ~1.5/min beyond. Never a flat
1 credit/run; never a 2-credit floor.
Run: uv run pytest tests/test_compute_rate.py -v
"""
import pytest

from services.sandbox import compute_credits_for_run


@pytest.mark.parametrize("duration_ms,expected", [
    (0,        1),   # degenerate → 1-credit floor
    (1,        1),   # ~instant → 1
    (3_600,    1),   # ~3.6s typical run → 1 (NOT 2)
    (30_000,   1),   # 30s → 1
    (40_000,   1),   # ~40s boundary → 1 (1.5 × 40/60 = 1.0)
    (40_001,   2),   # just past ~40s → 2
    (45_000,   2),   # 45s → 2
    (59_999,   2),   # ~1 min → 2
    (60_000,   2),   # 1 min → 2 (1.5 × 1)
    (90_000,   3),   # 1.5 min → 2.25 → 3
    (120_000,  3),   # 2 min → 3 (1.5 × 2)
    (300_000,  8),   # 5 min → 7.5 → 8
    (600_000, 15),   # 10 min → 15 (1.5 × 10)
])
def test_compute_credits(duration_ms, expected):
    assert compute_credits_for_run(duration_ms) == expected


def test_subminute_is_one_credit():
    # A few-second run must deduct exactly 1 credit (the floor), not 2.
    assert compute_credits_for_run(3_600) == 1


def test_ten_minutes_is_fifteen():
    assert compute_credits_for_run(600_000) == 15


def test_never_below_one():
    assert compute_credits_for_run(0) >= 1
    assert compute_credits_for_run(1) >= 1


def test_scales_not_flat():
    # Not a flat 1/run — longer runs cost more.
    assert compute_credits_for_run(600_000) > compute_credits_for_run(3_600)
