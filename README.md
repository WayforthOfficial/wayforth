# Wayforth

**The discovery and payment rail for AI agents.**

Search 270+ verified APIs. Pay via card or crypto. One MCP install.

## Install

```bash
uvx wayforth-mcp
# or
claude mcp add wayforth -- uvx wayforth-mcp
```

## The Three Calls

```python
# 1. Discover — semantic search with WayforthRank scoring
wayforth_search("translate text to Spanish")
# → DeepL        WRI:82  Tier 2  $0.00003/call
# → LibreTranslate  WRI:71  Tier 2  Free

# 2. Pay — card or crypto, your choice
wayforth_pay("deepl", 0.001, track="card")   # Stripe Treasury, no crypto
wayforth_pay("deepl", 0.001, track="crypto") # Base calldata, non-custodial

# 3. Execute — service receives payment, returns result
```

## Payment Tracks

**Track A — Card (mainstream developers):**
- Fund with credit card at wayforth.io/dashboard
- Stripe Treasury backs your balance (FDIC insured)
- Agent pays automatically — no crypto knowledge needed
- 1.5% routing fee

**Track B — Crypto (crypto-native developers):**
- Your own Base wallet with USDC
- wayforth_pay() returns calldata
- Agent broadcasts from your wallet — fully non-custodial
- 1.5% routing fee captured on-chain

## What's Live Today

- **274+ real API endpoints** across 18 categories
- **232+ Tier 2 verified** — probed every 6h, auto-demoted on failure
- **WayforthRank** — payment-signal ranking engine (patent pending WF-2026-001)
- **Credits system** — 2,000 free on signup, card purchases live
- **Dual-track payments** — Stripe Treasury (card) + Base calldata (crypto)
- **Smart contracts** — Base Sepolia (mainnet Q3 2026)
- **Free tier** — 2,000 credits on signup, no credit card required

## Credits

```
1 credit = $0.001 USD (for search quota)

Starter:  $19  → 50,000 credits
Pro:      $99  → 300,000 credits
Growth:   $299 → 1,000,000 credits

1 search query = 1 credit
```

## Routing Fee

```
1.5% on all payments
30% → $WAYF burn (post-mainnet)
70% → Wayforth operations
```

## $WAYF Token (post-mainnet)

- 30% of routing fees buy + burn $WAYF permanently
- Stake $WAYF to operate a Verifier node
- Hold $WAYF for fee discounts
- Tier 3 service certification requires $WAYF bond

## Resources

- **Website:** https://wayforth.io
- **Dashboard:** https://wayforth.io/dashboard
- **Quickstart:** https://wayforth.io/quickstart
- **Leaderboard:** https://wayforth.io/leaderboard
- **Whitepaper:** https://wayforth.io/wayforth-whitepaper-v3.pdf
- **GitHub:** https://github.com/WayforthOfficial/wayforth
- **dev.to:** https://dev.to/wayforthofficial

## Architecture

```
DEVELOPER
├── Card Track  → Stripe Treasury → Service (Stripe payout)
└── Crypto Track → Base Wallet → calldata → broadcast → service (USDC)

WAYFORTH LAYER
├── wayforth_search() → WayforthRank → ranked results
├── wayforth_pay()    → routes to Track A or B
└── Routing fee (1.5%) → 30% $WAYF burn + 70% operations

DATA FLYWHEEL
Payment outcomes → WayforthRank update → better rankings
→ more developer trust → more payments → flywheel spins
```

## License

BSL 1.1 — Licensor: Wayforth LTD
Converts to Apache 2.0 on April 25, 2030.

© 2026 Wayforth LTD
