#!/usr/bin/env python3
"""
Rate limit verification script.

Creates temporary test keys directly in the DB for each tier,
fires rapid requests at GET /search, reports status codes, then
cleans up.
"""
import asyncio
import hashlib
import secrets
import sys
import os

import asyncpg
import httpx

BASE = os.environ.get("API_BASE", "https://gateway.wayforth.io")
DB_URL_RAW = os.environ["DATABASE_URL"].replace(
    "postgres.railway.internal:5432", "shortline.proxy.rlwy.net:41067"
)

# Use demo account user IDs as "owners" for the temporary test keys.
# The test key tier is independent of the account tier — api_keys.tier
# is what drives rate limiting, not the user's plan.
USER_IDS = {
    "free":       "76440223-6dd0-4fce-9c89-2a6d0d050f49",  # demo_free
    "builder":    "76440223-6dd0-4fce-9c89-2a6d0d050f49",  # demo_free (reused)
    "starter":    "c73ac47a-8f3e-4f5a-8039-2101ae1fe40a",  # demo_starter
    "growth":     "75f1a043-29b5-44c0-b07e-4feb4849bf7d",  # demo_growth
    "enterprise": "a6d67cf5-1fee-4d9d-b3b2-7f7b0cd2631b",  # support@wayforth.io
}

# (expected_limit_per_minute, requests_to_fire, should_hit_limit)
TIER_TESTS = {
    "free":       (15,  20,  True),   # expect 429 on req 16
    "builder":    (120, 125, True),   # expect 429 on req 121
    "starter":    (300, 35,  False),  # 35 < 300, all should pass
    "growth":     (None, 40, False),  # unlimited
    "enterprise": (None, 40, False),  # unlimited
}


async def create_test_key(pool, tier: str) -> tuple[str, str]:
    """Insert a temporary api_key row; return (raw_key, key_uuid)."""
    raw = "wf_live_rl_test_" + secrets.token_urlsafe(12)
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    prefix = raw[:16]
    row = await pool.fetchrow(
        """
        INSERT INTO api_keys
            (user_id, key_hash, key_prefix, tier, active,
             owner_email, monthly_quota, rate_limit_per_minute)
        VALUES ($1::uuid, $2, $3, $4, true, 'rl-test@wayforth.io', 100000, 9999)
        RETURNING id::text
        """,
        USER_IDS[tier], key_hash, prefix, tier,
    )
    return raw, row["id"]


async def delete_test_key(pool, key_id: str) -> None:
    await pool.execute("DELETE FROM api_keys WHERE id = $1::uuid", key_id)


async def fire_requests(key: str, n: int) -> list[int]:
    """Fire n sequential requests; return list of HTTP status codes."""
    codes = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for _ in range(n):
            try:
                r = await client.get(
                    f"{BASE}/search",
                    params={"q": "rate limit test", "limit": "1"},
                    headers={"X-Wayforth-API-Key": key},
                )
                codes.append(r.status_code)
            except Exception as exc:
                codes.append(f"ERR:{exc}")
    return codes


def analyse(codes: list, expected_limit: int | None, n: int, should_hit: bool) -> str:
    ok = [c for c in codes if c == 200]
    limited = [c for c in codes if c == 429]
    errors = [c for c in codes if c not in (200, 429)]

    first_429 = next((i + 1 for i, c in enumerate(codes) if c == 429), None)

    if should_hit:
        if first_429 is None:
            verdict = "FAIL — no 429 seen"
        elif first_429 == expected_limit + 1:
            verdict = f"PASS — 429 on request {first_429} (limit {expected_limit})"
        else:
            verdict = f"WARN — 429 on request {first_429} (expected {expected_limit + 1})"
    else:
        if limited:
            verdict = f"FAIL — got {len(limited)} unexpected 429s"
        else:
            verdict = f"PASS — {len(ok)}/{n} requests succeeded, no 429"

    detail = f"200:{len(ok)}  429:{len(limited)}"
    if errors:
        detail += f"  ERR:{len(errors)}"
    if first_429:
        detail += f"  first_429_at_req#{first_429}"

    return f"{verdict}  [{detail}]"


async def main() -> None:
    print(f"Target: {BASE}\n")

    pool = await asyncpg.create_pool(DB_URL_RAW, min_size=1, max_size=3)
    created: dict[str, str] = {}

    print("── Creating temporary test keys ─────────────────────────────")
    for tier in TIER_TESTS:
        raw, key_id = await create_test_key(pool, tier)
        created[tier] = (raw, key_id)
        print(f"  {tier:12s} {raw[:24]}…  (id={key_id[:8]}…)")

    print("\n── Firing requests ──────────────────────────────────────────")
    results = {}
    for tier, (limit, n, should_hit) in TIER_TESTS.items():
        raw, _ = created[tier]
        limit_str = f"{limit}/min" if limit else "unlimited"
        print(f"  {tier:12s}  limit={limit_str:<12}  firing {n} requests…", end=" ", flush=True)
        codes = await fire_requests(raw, n)
        summary = analyse(codes, limit, n, should_hit)
        results[tier] = (codes, summary)
        print(summary)

    print("\n── Cleaning up test keys ────────────────────────────────────")
    for tier, (_, key_id) in created.items():
        await delete_test_key(pool, key_id)
        print(f"  deleted {tier} test key {key_id[:8]}…")

    await pool.close()

    print("\n── Summary ──────────────────────────────────────────────────")
    all_pass = True
    for tier, (_, summary) in results.items():
        status = "✓" if "PASS" in summary else ("!" if "WARN" in summary else "✗")
        print(f"  {status} {tier:12s}  {summary}")
        if "FAIL" in summary:
            all_pass = False

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    asyncio.run(main())
