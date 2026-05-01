#!/usr/bin/env python3
"""
Catalog miss rate & gap analysis report.
Usage: python3 scripts/catalog_misses.py

Requires env vars:
  ADMIN_KEY   — Wayforth admin key
  API_URL     — optional, defaults to https://api.wayforth.io
"""
import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "wayforth-misses-script/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def bar(value: int, max_value: int, width: int = 20) -> str:
    filled = round(value / max_value * width) if max_value else 0
    return "█" * filled + "░" * (width - filled)


def fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso[:16]


def print_section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def main() -> None:
    admin_key = os.environ.get("ADMIN_KEY", "")
    if not admin_key:
        print("ERROR: ADMIN_KEY env var not set.", file=sys.stderr)
        sys.exit(1)

    base = os.environ.get("API_URL", "https://api.wayforth.io").rstrip("/")
    key_param = urllib.parse.urlencode({"key": admin_key})
    misses_url = f"{base}/admin/catalog/misses?{key_param}"
    gaps_url = f"{base}/admin/catalog/gaps?{key_param}"

    print(f"\n{'═' * 60}")
    print(f"  WAYFORTH CATALOG MISS REPORT")
    print(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'═' * 60}")

    # ── Miss rate ────────────────────────────────────────────────
    print("\nFetching miss data...", end=" ", flush=True)
    try:
        misses = fetch(misses_url)
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        sys.exit(1)
    print("OK")

    print_section("MISS RATE SUMMARY")
    total = misses.get("total_searches", 0)
    zero = misses.get("zero_result_searches", 0)
    rate = misses.get("miss_rate_pct", 0.0)
    print(f"  Period          : {misses.get('period_days', 30)} days")
    print(f"  Total searches  : {total:,}")
    print(f"  Zero-result     : {zero:,}")
    print(f"  Miss rate       : {rate}%")

    top_misses = misses.get("top_misses", [])
    if top_misses:
        print_section("TOP MISSED QUERIES")
        max_count = top_misses[0]["count"] if top_misses else 1
        print(f"  {'#':<4} {'Query':<35} {'Count':>6}  {'Last Seen':<17}")
        print(f"  {'─'*4} {'─'*35} {'─'*6}  {'─'*16}")
        for i, m in enumerate(top_misses, 1):
            q = m["query"][:34]
            cnt = m["count"]
            last = fmt_dt(m.get("last_searched"))
            b = bar(cnt, max_count, width=10)
            print(f"  {i:<4} {q:<35} {cnt:>6}  {last:<17}  {b}")
    else:
        print("\n  No missed queries in the last 30 days.")

    miss_by_cat = misses.get("miss_by_category", {})
    if miss_by_cat:
        print_section("MISSES BY CATEGORY  (low-confidence matches)")
        max_cat = max(miss_by_cat.values())
        for cat, cnt in sorted(miss_by_cat.items(), key=lambda x: -x[1]):
            b = bar(cnt, max_cat, width=20)
            print(f"  {cat:<16} {b}  {cnt}")

    # ── Gap analysis ─────────────────────────────────────────────
    print("\nFetching gap data...", end=" ", flush=True)
    try:
        gaps_data = fetch(gaps_url)
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        sys.exit(1)
    print("OK")

    gaps = gaps_data.get("gaps", [])
    if gaps:
        print_section("CATALOG GAPS  (searches_per_service > 10, last 7 days)")
        print(f"  {'Category':<16} {'Searches':>9} {'Services':>9} {'Ratio':>7}  Suggested")
        print(f"  {'─'*16} {'─'*9} {'─'*9} {'─'*7}  {'─'*30}")
        for g in gaps:
            cat = g["category"][:15]
            s7d = g["searches_7d"]
            svc = g["services_available"]
            ratio = g["searches_per_service"]
            sug = ", ".join(g.get("suggested_services", []))
            print(f"  {cat:<16} {s7d:>9} {svc:>9} {ratio:>7.1f}  {sug}")
    else:
        print("\n  No significant gaps detected (no category exceeds 10 searches/service).")

    print(f"\n{'═' * 60}\n")


if __name__ == "__main__":
    main()
