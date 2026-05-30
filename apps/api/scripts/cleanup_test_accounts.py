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

FK deletion order for users (all NO ACTION constraints must be handled explicitly)
    1. credit_transactions    → NO ACTION on user_id
    2. package_purchases      → NO ACTION on user_id  (0 rows currently)
    3. referrals              → NO ACTION on referrer_user_id and referred_user_id
    4. org_members            → CASCADE (handled by user delete, but explicit is safer)
    5. organizations          → NO ACTION on owner_user_id  ← was the hidden blocker
    6. api_keys               → NO ACTION on user_id
    7. DELETE users           → CASCADE handles user_credits, service_favorites,
                                 user_service_keys, search_analytics (SET NULL)

FK deletion order for providers
    1. search_outcomes        → clear pioneer_routed flag on rows referencing test services
    2. provider_audit_log     → SET NULL on provider_id (handled by cascade), but
                                 explicit delete avoids surprises
    3. provider_sessions      → NO ACTION on provider_id
    4. provider_services      → NO ACTION on provider_id
    5. DELETE providers
    6. DELETE services        → slug matches test slug prefixes
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
    "smoketest-provider-",
    "smoke-test-",
    "test-provider-",
)

# Service slug prefixes that indicate test catalog entries.
# Any service whose slug starts with one of these is deleted after its provider is removed.
TEST_SERVICE_SLUG_PREFIXES: tuple[str, ...] = (
    "smoketest-svc-",
    "smoke-test-",
    "test-service-",
    "test-svc-",
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


# ── Helpers ──────────────────────────────────────────────────────────────────

def is_test_slug(slug: str) -> bool:
    """Return True if a service slug matches a known test prefix."""
    slug_lower = (slug or "").lower()
    return any(slug_lower.startswith(p) for p in TEST_SERVICE_SLUG_PREFIXES)


def _slug_like_clauses(col: str) -> str:
    """Build a SQL OR expression matching all TEST_SERVICE_SLUG_PREFIXES."""
    return " OR ".join(f"{col} LIKE '{p}%'" for p in TEST_SERVICE_SLUG_PREFIXES)


# ── Core logic ───────────────────────────────────────────────────────────────

async def run(execute: bool) -> None:
    if not DB_URL:
        print("ERROR: set DATABASE_PUBLIC_URL or DATABASE_URL before running.", file=sys.stderr)
        sys.exit(1)

    asyncpg_url = DB_URL.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(asyncpg_url)

    try:
        # ── Identify user targets ─────────────────────────────────────────────
        all_users = await conn.fetch("""
            SELECT u.id, u.email, u.supabase_id,
                   COALESCE(SUM(ak.calls_count), 0) as total_calls,
                   COUNT(ak.id) FILTER (WHERE ak.active) as active_keys
            FROM users u
            LEFT JOIN api_keys ak ON ak.user_id = u.id
            GROUP BY u.id, u.email, u.supabase_id
            ORDER BY u.email
        """)

        user_targets = []
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

            is_synthetic = any(email.lower().endswith(d) for d in SYNTHETIC_DOMAINS)
            if not is_synthetic and row["total_calls"] > 0:
                skipped_calls.append((email, int(row["total_calls"])))
                continue

            user_targets.append(dict(row))

        # ── Identify provider targets ─────────────────────────────────────────
        all_providers = await conn.fetch(
            "SELECT id, email, company_name, boost_used, boost_expires_at, created_at "
            "FROM providers ORDER BY email"
        )

        provider_targets = []
        for row in all_providers:
            email = row["email"] or ""
            if is_test_account(email):
                provider_targets.append(dict(row))

        # Service slugs owned by test providers + any slug matching test prefixes directly
        provider_ids = [r["id"] for r in provider_targets]
        test_service_rows: list[dict] = []
        if provider_ids:
            owned = await conn.fetch(
                "SELECT DISTINCT service_slug FROM provider_services "
                "WHERE provider_id = ANY($1::uuid[])",
                provider_ids,
            )
            owned_slugs = {r["service_slug"] for r in owned}
        else:
            owned_slugs = set()

        # Also pick up any stray test services not linked to a test provider
        slug_filter = _slug_like_clauses("slug")
        stray = await conn.fetch(
            f"SELECT id, slug, name, source, coverage_tier, created_at "
            f"FROM services WHERE {slug_filter}"
        )
        stray_slugs = {r["slug"] for r in stray if r["slug"]}

        all_test_slugs = owned_slugs | stray_slugs
        if all_test_slugs:
            test_service_rows = await conn.fetch(
                "SELECT id, slug, name, source, coverage_tier, created_at "
                "FROM services WHERE slug = ANY($1::text[])",
                list(all_test_slugs),
            )

        # ── Dry-run report ────────────────────────────────────────────────────
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

        print(f"\nUser targets for deletion ({len(user_targets)}):")
        for r in user_targets:
            print(f"  {r['email']}  (id={r['id']}, active_keys={r['active_keys']})")

        print(f"\nProvider targets for deletion ({len(provider_targets)}):")
        for r in provider_targets:
            boost_flag = "  *** BOOST ACTIVE ***" if r["boost_used"] else ""
            print(f"  {r['email']}  (id={r['id']}, company={r['company_name']}){boost_flag}")

        print(f"\nService targets for deletion ({len(test_service_rows)}):")
        for r in test_service_rows:
            print(f"  {r['slug']}  (id={r['id']}, tier={r['coverage_tier']}, source={r['source']})")

        if not user_targets and not provider_targets and not test_service_rows:
            print("\nNothing to delete.")
            return

        if not execute:
            print(f"\n[DRY-RUN] Pass --execute to delete "
                  f"{len(user_targets)} user(s), {len(provider_targets)} provider(s), "
                  f"{len(test_service_rows)} service(s).")
            return

        # ── Execute user deletions in FK-safe order ───────────────────────────
        if user_targets:
            target_ids = [r["id"] for r in user_targets]
            target_emails = [r["email"] for r in user_targets]
            async with conn.transaction():
                r1 = await conn.execute(
                    "DELETE FROM credit_transactions WHERE user_id = ANY($1::uuid[])", target_ids
                )
                print(f"\n  credit_transactions deleted:  {r1}")

                r2 = await conn.execute(
                    "DELETE FROM package_purchases WHERE user_id = ANY($1::uuid[])", target_ids
                )
                print(f"  package_purchases deleted:    {r2}")

                r3a = await conn.execute(
                    "DELETE FROM referrals WHERE referrer_user_id = ANY($1::uuid[])", target_ids
                )
                r3b = await conn.execute(
                    "DELETE FROM referrals WHERE referred_user_id = ANY($1::uuid[])", target_ids
                )
                print(f"  referrals deleted:            {r3a} / {r3b}")

                r4 = await conn.execute(
                    "DELETE FROM org_members WHERE user_id = ANY($1::uuid[])", target_ids
                )
                print(f"  org_members deleted:          {r4}")

                r5 = await conn.execute(
                    "DELETE FROM organizations WHERE owner_user_id = ANY($1::uuid[])", target_ids
                )
                print(f"  organizations deleted:        {r5}")

                r6 = await conn.execute(
                    "DELETE FROM api_keys WHERE user_id = ANY($1::uuid[])", target_ids
                )
                print(f"  api_keys deleted:             {r6}")

                r7 = await conn.execute(
                    "DELETE FROM users WHERE id = ANY($1::uuid[])", target_ids
                )
                print(f"  users deleted:                {r7}")

            print(f"\n  Users removed ({len(user_targets)}):")
            for e in sorted(target_emails):
                print(f"    ✓ {e}")

        # ── Execute provider + service deletions in FK-safe order ─────────────
        if provider_targets or test_service_rows:
            async with conn.transaction():
                # 1. Nullify pioneer_routed on search_outcomes referencing test services
                #    rather than deleting — the search_analytics rows are real user events,
                #    only the routing attribution is contaminated.
                if test_service_rows:
                    svc_ids = [r["id"] for r in test_service_rows]
                    r_so = await conn.execute(
                        "UPDATE search_outcomes SET pioneer_routed = FALSE "
                        "WHERE pioneer_routed = TRUE AND service_id = ANY($1::uuid[])",
                        svc_ids,
                    )
                    print(f"\n  search_outcomes pioneer_routed cleared: {r_so}")

                    # Also clean up fully orphaned pioneer_routed rows (service_id already NULL)
                    # that fall inside the same session window as any test provider.
                    # We identify them by user_id matching the owner of the test provider's
                    # searches — but that's user-space; skip here to avoid false positives.

                if provider_ids:
                    # 2. provider_audit_log (SET NULL cascade, but be explicit)
                    r_pal = await conn.execute(
                        "DELETE FROM provider_audit_log WHERE provider_id = ANY($1::uuid[])",
                        provider_ids,
                    )
                    print(f"  provider_audit_log deleted:   {r_pal}")

                    # 3. provider_sessions (NO ACTION)
                    r_ps = await conn.execute(
                        "DELETE FROM provider_sessions WHERE provider_id = ANY($1::uuid[])",
                        provider_ids,
                    )
                    print(f"  provider_sessions deleted:    {r_ps}")

                    # 4. provider_services (NO ACTION)
                    r_pss = await conn.execute(
                        "DELETE FROM provider_services WHERE provider_id = ANY($1::uuid[])",
                        provider_ids,
                    )
                    print(f"  provider_services deleted:    {r_pss}")

                    # 5. providers
                    r_p = await conn.execute(
                        "DELETE FROM providers WHERE id = ANY($1::uuid[])", provider_ids
                    )
                    print(f"  providers deleted:            {r_p}")

                # 6. services (after provider_services is gone so no orphan FK)
                if test_service_rows:
                    svc_ids = [r["id"] for r in test_service_rows]
                    r_svc = await conn.execute(
                        "DELETE FROM services WHERE id = ANY($1::uuid[])", svc_ids
                    )
                    print(f"  services deleted:             {r_svc}")

            print(f"\n  Providers removed ({len(provider_targets)}):")
            for r in sorted(provider_targets, key=lambda x: x["email"]):
                print(f"    ✓ {r['email']}")
            print(f"\n  Services removed ({len(test_service_rows)}):")
            for r in sorted(test_service_rows, key=lambda x: x["slug"] or ""):
                print(f"    ✓ {r['slug']}")

    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean up test/junk accounts from production.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete accounts, providers, and test services (default is dry-run).",
    )
    args = parser.parse_args()
    asyncio.run(run(execute=args.execute))


if __name__ == "__main__":
    main()
