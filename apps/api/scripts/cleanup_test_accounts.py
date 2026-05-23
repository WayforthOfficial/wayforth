#!/usr/bin/env python3
"""cleanup_test_accounts.py — identify and delete test/junk accounts from production.

Usage
    Dry-run (default — prints what would be deleted, changes nothing):
        python3 apps/api/scripts/cleanup_test_accounts.py

    Execute:
        python3 apps/api/scripts/cleanup_test_accounts.py --execute

Safety
    The following safelist is ALWAYS preserved regardless of pattern matching:

        dorassulin1@gmail.com   — primary owner account
        vanesyadnaura@vvo.me    — real early user (joined 2026-05-19)
        assulindor@gmail.com    — real early user
        support@wayforth.io     — operational support account

    Accounts that have made real API calls (calls_count > 0) are also skipped
    unless their email matches a @example.invalid or @wayforth.test domain,
    which are provably non-real.

FK deletion order (all NO ACTION constraints must be handled explicitly)
    1. credit_transactions    → NO ACTION on user_id
    2. package_purchases      → NO ACTION on user_id  (0 rows currently)
    3. referrals              → NO ACTION on referrer_user_id and referred_user_id
    4. org_members            → CASCADE (handled by user delete, but explicit is safer)
    5. organizations          → NO ACTION on owner_user_id  ← was the hidden blocker
    6. api_keys               → NO ACTION on user_id
    7. DELETE users           → CASCADE handles user_credits, service_favorites,
                                 user_service_keys, search_analytics (SET NULL)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

# ── Constants ────────────────────────────────────────────────────────────────

SAFELIST: frozenset[str] = frozenset({
    "dorassulin1@gmail.com",
    "vanesyadnaura@vvo.me",
    "assulindor@gmail.com",
})

# Domains that are provably synthetic — delete regardless of call count.
SYNTHETIC_DOMAINS: tuple[str, ...] = (
    "@example.invalid",
    "@wayforth.test",
    "@audit-research.io",
    "@example.com",
)

# Email prefixes that indicate test/internal accounts.
# Matched case-insensitively as a prefix before the '@'.
TEST_PREFIXES: tuple[str, ...] = (
    "ratelimit-test-",
    "audit-",
    "demo_",
    "demo-",
    "probe-",
    "pentest",
    "staging",
    "noreply",
    "bot-",
    "temp-",
    "fake-",
    "victim",
    "some-other-email",
    "test-",
    "test+",
)

# Exact local-parts (before @) at @wayforth.io that are internal/unmonitored.
INTERNAL_WAYFORTH_LOCALS: frozenset[str] = frozenset({
    "founders", "admin", "info", "contact",
    "hello", "billing", "team", "dev", "legal",
    "noreply", "support-test", "probe", "support",
})

DB_URL = (
    os.environ.get("DATABASE_PUBLIC_URL")
    or os.environ.get("DATABASE_URL", "")
)


# ── Pattern matching ─────────────────────────────────────────────────────────

def is_test_account(email: str) -> bool:
    """Return True if this email looks like a test/junk account."""
    if not email:
        return False
    email_lower = email.lower()

    if email_lower in {e.lower() for e in SAFELIST}:
        return False

    # Synthetic domains — always junk
    for domain in SYNTHETIC_DOMAINS:
        if email_lower.endswith(domain):
            return True

    local, _, domain_part = email_lower.partition("@")

    # Internal wayforth locals (founders@, admin@, etc.)
    if domain_part == "wayforth.io" and local in INTERNAL_WAYFORTH_LOCALS:
        return True

    # Test prefixes
    for prefix in TEST_PREFIXES:
        if local.startswith(prefix):
            return True

    return False


# ── Core logic ───────────────────────────────────────────────────────────────

async def run(execute: bool) -> None:
    if not DB_URL:
        print("ERROR: set DATABASE_PUBLIC_URL or DATABASE_URL before running.", file=sys.stderr)
        sys.exit(1)

    asyncpg_url = DB_URL.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(asyncpg_url)

    try:
        # Gather candidate accounts
        all_users = await conn.fetch("""
            SELECT u.id, u.email, u.supabase_id,
                   COALESCE(SUM(ak.calls_count), 0) as total_calls,
                   COUNT(ak.id) FILTER (WHERE ak.active) as active_keys
            FROM users u
            LEFT JOIN api_keys ak ON ak.user_id = u.id
            GROUP BY u.id, u.email, u.supabase_id
            ORDER BY u.email
        """)

        targets = []
        skipped_calls = []
        skipped_safelist = []

        for row in all_users:
            email = row["email"] or ""

            if email.lower() in {e.lower() for e in SAFELIST}:
                skipped_safelist.append(email)
                continue

            if not is_test_account(email):
                skipped_safelist.append(email)
                continue

            # Keep accounts that have real call history unless provably synthetic domain
            is_synthetic = any(email.lower().endswith(d) for d in SYNTHETIC_DOMAINS)
            if not is_synthetic and row["total_calls"] > 0:
                skipped_calls.append((email, int(row["total_calls"])))
                continue

            targets.append(dict(row))

        target_ids = [r["id"] for r in targets]
        target_emails = [r["email"] for r in targets]

        print("=" * 70)
        print(f"{'DRY-RUN' if not execute else 'EXECUTING'}: cleanup_test_accounts.py")
        print("=" * 70)
        print(f"\nSafelist preserved ({len(skipped_safelist)}):")
        for e in sorted(skipped_safelist):
            print(f"  {e}")

        if skipped_calls:
            print(f"\nSkipped — real call history ({len(skipped_calls)}):")
            for e, c in sorted(skipped_calls):
                print(f"  {e}  ({c} calls)")

        print(f"\nTargets for deletion ({len(targets)}):")
        for r in targets:
            print(f"  {r['email']}  (id={r['id']}, active_keys={r['active_keys']})")

        if not targets:
            print("\nNothing to delete.")
            return

        if not execute:
            print(f"\n[DRY-RUN] Pass --execute to delete {len(targets)} accounts.")
            return

        # ── Execute deletions in FK-safe order ──────────────────────────────
        async with conn.transaction():
            # 1. credit_transactions (NO ACTION)
            r1 = await conn.execute(
                "DELETE FROM credit_transactions WHERE user_id = ANY($1::uuid[])", target_ids
            )
            print(f"\n  credit_transactions deleted:  {r1}")

            # 2. package_purchases (NO ACTION)
            r2 = await conn.execute(
                "DELETE FROM package_purchases WHERE user_id = ANY($1::uuid[])", target_ids
            )
            print(f"  package_purchases deleted:    {r2}")

            # 3. referrals (NO ACTION on both columns)
            r3a = await conn.execute(
                "DELETE FROM referrals WHERE referrer_user_id = ANY($1::uuid[])", target_ids
            )
            r3b = await conn.execute(
                "DELETE FROM referrals WHERE referred_user_id = ANY($1::uuid[])", target_ids
            )
            print(f"  referrals deleted:            {r3a} / {r3b}")

            # 4. org_members (CASCADE, but explicit for clarity)
            r4 = await conn.execute(
                "DELETE FROM org_members WHERE user_id = ANY($1::uuid[])", target_ids
            )
            print(f"  org_members deleted:          {r4}")

            # 5. organizations (NO ACTION on owner_user_id) ← key fix
            r5 = await conn.execute(
                "DELETE FROM organizations WHERE owner_user_id = ANY($1::uuid[])", target_ids
            )
            print(f"  organizations deleted:        {r5}")

            # 6. api_keys (NO ACTION)
            r6 = await conn.execute(
                "DELETE FROM api_keys WHERE user_id = ANY($1::uuid[])", target_ids
            )
            print(f"  api_keys deleted:             {r6}")

            # 7. users (CASCADE handles user_credits, service_favorites,
            #    user_service_keys; SET NULL handles search_analytics)
            r7 = await conn.execute(
                "DELETE FROM users WHERE id = ANY($1::uuid[])", target_ids
            )
            print(f"  users deleted:                {r7}")

        print(f"\nDone. {len(targets)} accounts removed.")
        for e in sorted(target_emails):
            print(f"  ✓ {e}")

    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean up test/junk accounts from production.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete accounts (default is dry-run).",
    )
    args = parser.parse_args()
    asyncio.run(run(execute=args.execute))


if __name__ == "__main__":
    main()
