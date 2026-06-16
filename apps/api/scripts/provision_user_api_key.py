#!/usr/bin/env python3
"""provision_user_api_key.py — mint an API key for an existing user.

Use case
    A user row exists in `users` but the user has no active row in `api_keys`
    (new signup that never received its first key, key deactivated by admin,
    etc.) and the dashboard can't load because /account/api-key returns
    api_key=null. This script provisions a fresh key linked to user_id,
    mirroring the canonical generation logic in routers/auth.py:register_user
    so the new key is indistinguishable from one issued through the normal
    signup path.

Safety
    The raw key is printed ONCE to stdout — that is the only time you can
    capture it. Pipe to a secure clipboard or paste into 1Password
    immediately; the script does NOT log it anywhere.

    By default the script deactivates any pre-existing active keys for the
    user before inserting the new one so there is exactly one canonical
    active key per account. Pass --keep-existing to skip that step (useful
    if you genuinely want multiple active keys, e.g. CI + production).

Usage
    Set DATABASE_URL (and optionally ENCRYPTION_KEY for "Reveal in UI"
    support) before running:

        export DATABASE_URL="postgresql://..."
        export ENCRYPTION_KEY="$(railway variables get ENCRYPTION_KEY)"
        python3 apps/api/scripts/provision_user_api_key.py \\
            --email demo_growth@wayforth.io --tier growth

    Or on Railway directly:

        railway run python3 apps/api/scripts/provision_user_api_key.py \\
            --email demo_growth@wayforth.io --tier growth

    Dry-run (looks up the user and prints what WOULD be done, no writes):

        python3 apps/api/scripts/provision_user_api_key.py \\
            --email demo_growth@wayforth.io --dry-run

Exit codes
    0 — key provisioned successfully (raw key printed to stdout)
    1 — argument error / missing env
    2 — user not found
    3 — DB error or unique-conflict
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import secrets
import sys

import asyncpg


# ── Tier defaults (kept in step with routers/auth.py TIER_LIMITS) ────────────
# rate_limit_per_minute and monthly_quota are stored on the api_keys row at
# insert time. Production reads them on each request, so we need the same
# values the admin endpoint would set.
_TIER_DEFAULTS: dict[str, dict[str, int]] = {
    "free":       {"rpm": 10,  "monthly": 1_000},
    "starter":    {"rpm": 30,  "monthly": 5_000},
    "builder":    {"rpm": 60,  "monthly": 20_000},
    "pro":        {"rpm": 120, "monthly": 100_000},
    "growth":     {"rpm": 300, "monthly": 500_000},
    "enterprise": {"rpm": 500, "monthly": -1},  # unlimited
}


def _generate_key() -> tuple[str, str, str]:
    """Return (raw_key, key_hash, key_prefix) matching register_user/regenerate-key."""
    raw = "wf_live_" + secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest(), raw[:12]


def _maybe_encrypt(raw_key: str) -> str | None:
    """If ENCRYPTION_KEY is set, Fernet-encrypt the raw key for the "Reveal"
    flow in /account/api-key. If not, return None — the UI will fall back to
    a prefix preview, same as legacy unencrypted keys."""
    key = os.environ.get("ENCRYPTION_KEY", "")
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode()).encrypt(raw_key.encode()).decode()
    except Exception as exc:
        print(
            f"WARN: ENCRYPTION_KEY set but Fernet encryption failed ({exc}). "
            "Inserting key without encrypted blob (Reveal UI will show prefix only).",
            file=sys.stderr,
        )
        return None


async def provision(
    email: str,
    tier: str,
    keep_existing: bool,
    dry_run: bool,
) -> int:
    # DATABASE_PUBLIC_URL (externally routable) is preferred when running
    # outside Railway's private network — `railway run` from a dev laptop
    # injects DATABASE_URL pointing at postgres.railway.internal which only
    # resolves from inside the cluster. Same fallback the crawler scripts
    # (e.g. apps/crawler/bulk_prober.py) use.
    db_url = (
        os.environ.get("DATABASE_PUBLIC_URL")
        or os.environ.get("DATABASE_URL", "")
    )
    if not db_url:
        print(
            "ERROR: neither DATABASE_PUBLIC_URL nor DATABASE_URL is set.",
            file=sys.stderr,
        )
        return 1

    if tier not in _TIER_DEFAULTS:
        print(
            f"ERROR: unknown tier {tier!r}. Valid: {', '.join(_TIER_DEFAULTS)}",
            file=sys.stderr,
        )
        return 1

    # asyncpg accepts plain postgresql:// — strip the SQLAlchemy +asyncpg form.
    asyncpg_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(asyncpg_url)
    try:
        async with conn.transaction():
            user_row = await conn.fetchrow(
                "SELECT id, email FROM users WHERE email = $1", email
            )
            if not user_row:
                print(f"ERROR: no user found with email {email!r}.", file=sys.stderr)
                return 2
            user_id = user_row["id"]

            existing = await conn.fetch(
                "SELECT id, key_prefix, active, created_at "
                "FROM api_keys WHERE user_id = $1::uuid "
                "ORDER BY created_at DESC",
                user_id,
            )
            active_existing = [r for r in existing if r["active"]]

            print(f"User found: {email}  id={user_id}")
            if existing:
                print(f"Existing api_key rows: {len(existing)} "
                      f"({len(active_existing)} active)")
                for r in existing:
                    flag = "ACTIVE" if r["active"] else "inactive"
                    print(f"  - {r['key_prefix']!s}  {flag}  created={r['created_at']}")
            else:
                print("Existing api_key rows: 0")

            limits = _TIER_DEFAULTS[tier]
            raw_key, key_hash, key_prefix = _generate_key()
            encrypted_key = _maybe_encrypt(raw_key)

            print()
            print(f"Plan:")
            print(f"  tier                  = {tier}")
            print(f"  rate_limit_per_minute = {limits['rpm']}")
            print(f"  monthly_quota         = {limits['monthly']}")
            print(f"  active                = TRUE (column default)")
            print(f"  encrypted_key         = "
                  f"{'<set>' if encrypted_key else '<null — UI shows prefix preview>'}")
            print(f"  key_prefix            = {key_prefix}")
            print(f"  deactivate existing   = {'NO (--keep-existing)' if keep_existing else 'YES'}")

            if dry_run:
                print()
                print("Dry-run: no writes. Re-run without --dry-run to apply.")
                return 0

            if not keep_existing and active_existing:
                deactivated = await conn.execute(
                    "UPDATE api_keys SET active = FALSE "
                    "WHERE user_id = $1::uuid AND active = TRUE",
                    user_id,
                )
                print(f"\nDeactivated existing active keys: {deactivated}")

            await conn.execute(
                """INSERT INTO api_keys
                       (key_hash, key_prefix, tier, user_id, owner_email,
                        encrypted_key, rate_limit_per_minute, monthly_quota)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
                key_hash, key_prefix, tier, user_id, email,
                encrypted_key, limits["rpm"], limits["monthly"],
            )

            # Confirm immediately inside the same transaction so a flaky read
            # replica can't hide a write that didn't actually land.
            inserted = await conn.fetchrow(
                "SELECT id, key_prefix, tier, active, monthly_quota, "
                "       rate_limit_per_minute, created_at "
                "FROM api_keys WHERE key_hash = $1",
                key_hash,
            )

        if not inserted or not inserted["active"]:
            print("ERROR: insert succeeded but row is not active. "
                  "Check schema constraints.", file=sys.stderr)
            return 3

        print("\n" + "=" * 60)
        print("✅ Key provisioned successfully")
        print("=" * 60)
        print(f"  prefix       : {inserted['key_prefix']}")
        print(f"  tier         : {inserted['tier']}")
        print(f"  active       : {inserted['active']}")
        print(f"  monthly_quota: {inserted['monthly_quota']}")
        print(f"  rpm          : {inserted['rate_limit_per_minute']}")
        print(f"  created_at   : {inserted['created_at']}")
        print()
        print(f"RAW KEY (shown once — copy now, this is your only chance):")
        print(f"  {raw_key}")
        print()
        return 0

    except asyncpg.UniqueViolationError as exc:
        print(f"ERROR: unique-constraint violation — {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"ERROR: DB error — {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    finally:
        await conn.close()


def main() -> int:
    p = argparse.ArgumentParser(
        description="Provision a Wayforth API key for an existing user.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--email", required=True,
                   help="Email of the user (must already exist in `users`).")
    p.add_argument("--tier", default="growth",
                   choices=sorted(_TIER_DEFAULTS.keys()),
                   help="Tier for the new key. Default: growth.")
    p.add_argument("--keep-existing", action="store_true",
                   help="Do NOT deactivate existing active keys before inserting. "
                        "Off by default so the new key is the single canonical one.")
    p.add_argument("--dry-run", action="store_true",
                   help="Look up the user and print the plan; do not write.")
    args = p.parse_args()

    return asyncio.run(provision(
        email=args.email.strip().lower(),
        tier=args.tier,
        keep_existing=args.keep_existing,
        dry_run=args.dry_run,
    ))


if __name__ == "__main__":
    sys.exit(main())
