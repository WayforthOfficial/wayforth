# Changelog

All notable changes to the Wayforth platform are documented here.

---

## v0.8.8 — Credits & Pioneer fixes — 2026-06-04

- Subscription reset now fires correctly — `quota_reset_at` and `monthly_calls_reset_at` fields synced; overdue reset triggered immediately
- Credit alerts now aware of the Pioneer reserve pool — low/zero alerts only fire when total credits (plan + reserve) are depleted
- Pioneer day counter now derived from distinct Pacific calendar dates, not raw drip event count — robust against makeup drips and out-of-band events
- Migration 056 (reset-date sync) committed; data-fix for the overdue reset applied

---

## v0.8.7 — Pioneer reserve pool — 2026-06-04

- Pioneer drip credits now live in a separate overflow pool (`pioneer_credits_balance`) instead of the main credits balance
- Spend order: plan credits first, Pioneer reserve activates automatically when plan hits zero
- Dashboard shows two separate credit bars — plan and pioneer reserve — with overflow and low-credit banners
- Subscription reset: plan credits reset to plan max, pioneer reserve resets to zero each cycle
- USDC prepaid balance preserved on reset (Option A: `GREATEST(credits_balance, plan_max)`)
- `/billing/balance` and `/auth/me` now return `pioneer_credits_remaining` and `total_credits` fields
- Migration 055 + one-time data-fix committed

---

## v0.8.6 — WayforthRank integrity — 2026-06-03

- Fixed slug matcher bug: recalculate now matches `clicked_slug` directly against `services.slug` (was using a name-derived proxy, which hit the wrong duplicate rows)
- Deduped 8 managed service rows with split signal/base history; donor rows soft-retired (`active=false`, reversible)
- Added base-only scoring fallback for managed services with no signal — zero NULL `wri_score` enforced across all 16 managed services
- Added `service_health.error_rate` failure penalty (`score × (1 − error_rate × 0.3)`, default on via `ENABLE_FAILURE_PENALTY`)
- Wired `failure_code` on `/execute/batch` (was the only execution path missing it)
- Recalculate now stamps `updated_at` on every score write
- DB changes documented in `scripts/data-fixes/2026-06-03-dedup-managed-services.sql`

---

## v0.8.5 — Security hardening (post internal audit) — 2026-06-03

Remediation of the 17-finding v0.8.4 internal adversarial audit. The x402 and
USDC rails are disabled pending proper on-chain settlement; do not re-enable
until the v0.9.0 real-money test sequence.

- Disabled x402 and USDC rails pending proper on-chain settlement implementation (env-gated, default off)
- Stripped shadow execute/search/webhook API from wayforth-rank (now `/health` + `/v1/rank/recalculate` only)
- Pinned JWT algorithm to {RS256, ES256} from JWKS only; never read `alg` from the token header; pinned issuer
- Closed webhook SSRF DNS-rebind gap (IPv4-mapped-IPv6 unwrap, fail-closed parse, pre-connect re-validation)
- Moved anonymous search counters to Redis; IPv6 /64 keying; fail-closed on Redis loss
- Email canonicalization (plus-suffix / Gmail-dot) before uniqueness checks on signup and provider registration
- Auth throttle now fails closed on Redis loss (strict in-memory fallback)
- USDC watcher: payer-address binding, persisted block cursor, atomic tx claim (no genesis re-scan, no double-credit)
- USDC top-up and subscribe: payer-address verification against the on-chain sender
- Added admin-only `POST /admin/usdc/reconcile` for manually-reviewed stranded funds
- Added self-serve account deletion (`DELETE /account` + `/account/undelete`) with 24h grace and a reaper
- Circuit breakers on rate-capped managed services (global + per-tier per-user daily caps)
- WRI self-dealing signal exclusion (a provider can't inflate its own ranking)
- Removed a 122-line dead duplicate `check_auth` that carried the old anon-counter bypass
- Committed previously ad-hoc migrations (credits CHECK, pioneer counters, email-canonical, deletion, USDC payer-binding)
- Fixed USDC monthly-reset date math (no February skip)

---

## v0.8.4 — Integrity Patch — 2026-06-03

### Credits
- DB-level CHECK constraint: `credits_balance >= 0`
- Fixed multi-key credit replenishment vector — users with multiple API keys were eligible for N credit resets per month; now gated to once per calendar month per user regardless of key count

### WayforthRank
- Fixed pioneer `signal_weight` discount — pioneer-routed payment conversions now correctly weighted at 0.75× in both ranking paths (`ranker_client.py` and `search.py`)
- Restored x402 +5 bonus to v1 inline formula in `services/wayforthrank.py` (had silently drifted from `ranker.py`)
- `/admin/rank/recalculate` endpoint fixed (was 500 in production — `wayforth_rank_v2.py` is gitignored and not deployed in API container); formula inlined
- `/admin/rank/recalculate` now runs automatically every 6h after health monitoring completes (previously manual-only; `services.wri_score` was stale by default)
- `feed_signal.py` now actually scheduled at 06:00 UTC daily (was documented but never wired)

### Security
- Production database password rotated
- 25 orphaned null-user-id API keys removed

---

## v0.8.3 — Calibration — 2026-05-29

This is a feature + correctness release. It corrects the credit model (v0.8.2 quotas were wrong), fixes a data integrity bug in credit transaction logging, adds annual billing for providers, and completes the Pioneer Program routing instrumentation.

### Credits system overhaul

- **Plan quotas corrected.** `calls_included` in all plan definitions now equals the true monthly credit allowance. Previous values were legacy call-count approximations (`CREDITS_PER_CALL = 6` divisor), not actual credit amounts.

  | Plan | Old quota (stale) | Correct quota |
  |------|------------------|---------------|
  | Free | 100 | 100 |
  | Builder | 1,000 | 6,000 |
  | Starter | 3,500 | 21,000 |
  | Pro | 12,000 | 72,000 |
  | Growth | 40,000 | **240,000** |
  | Enterprise | 100,000 | 1,000,000 |

- **Per-service credit deduction enforced.** `_increment_calls()` now accepts a `cost` parameter and increments `monthly_calls_count` by the service's credit cost (e.g. 3 for Groq, 86 for Stability AI Core), not by 1. `monthly_calls_count` now tracks credits consumed, not call count.

- **`credits_per_call` column added to `services` table.** Populated for all 17 managed service slugs. Enables balance recalculation via JOIN to services table.

- **Balance endpoint (`GET /billing/balance`) fixed.** `credits_remaining` now reads from `user_credits.credits_balance` (the authoritative credit ledger), not from `calls_included - monthly_calls_count`. `credits_included` returns the plan's `monthly_credits` (e.g. 240,000 for Growth). `daily_avg_credits` replaces `daily_avg_calls` in the forecast object (`daily_avg_calls` kept as backward-compat alias).

- **Monthly reset replenishes `credits_balance`.** `_monthly_topup_reset` now sets `credits_balance = GREATEST(credits_balance, plan.monthly_credits)` on the first of each month, in addition to zeroing `monthly_calls_count`.

### Transaction type bug fixed

- **`type='usage'` default changed to `type='execution'`** in `check_and_deduct_credits`. Historically all callers that didn't pass `tx_type` explicitly wrote `type='usage'` — producing ~5,470 transactions that were silently dropped by any query filtering on `type='execution'`. The two `/pay` (x402) call sites were explicitly updated to `tx_type='cross_rail'`.

### Webhook event renamed

- **`wayf.calls_reset` → `wayf.credits_reset`.** No providers were subscribed to this event at rename time.

### Pioneer Program fixes

- **Drip scheduler uses Pacific timezone boundary** (`America/Los_Angeles`) instead of UTC `CURRENT_DATE`. Prevents users near UTC midnight in PDT from being skipped when the job runs at 00:05 UTC.

- **Enrollment confirmed indefinite** — no 30-day cap. The `days_remaining` field in `/account/pioneer/status` counts down the 7-day *rejoin cooldown* after opting out, not an enrollment window. Renamed to `cooldown_days_remaining` (`days_remaining` kept as alias).

- **Pioneer routing fields on every authenticated search response.** `pioneer_routing`, `pioneer_routed_to_boosted`, `signal_weight`, and `boost_active` are now always present in `/search` responses for authenticated users. Previously they only appeared when `pioneer_routing=true`.

- **New fields in `/account/pioneer/status`:**
  - `pioneer_boosted_searches` / `pioneer_boosted_searches_this_month` — renamed from `pioneer_calls_made` / `pioneer_calls_this_month` (old names kept as aliases). These count searches routed to boosted providers, not total API calls.
  - `active_boosted_providers` — count of providers currently in an active boost window.

### Provider Dashboard

- **Annual billing added.** `POST /provider/billing/upgrade` now accepts `billing_interval: "month" | "year"`. Annual price: Intelligence $984/yr ($82/mo), Premium $2,988/yr ($249/mo) — 17% discount (10 months pricing).
- `billing_interval` field added to `providers` table and returned from `GET /provider/me`.
- Boost (15-day / 30-day) is tied to plan tier, not billing interval — unchanged.

### Analytics API

- `/account/analytics` response updated:
  - New `"credits"` object (monthly quota status) alongside backward-compat `"calls"` alias.
  - New `"api_calls"` object (actual request counts) alongside backward-compat `"executions"` alias.
  - `by_service[*].request_count` added alongside `count`.
  - `wri_scores[*].request_count` added alongside `calls`.

### Infrastructure

- `services.active` column: soft-delete flag for `DELETE /provider/services/{slug}`.
- `providers.billing_interval` column added.

### Signal enrichment system

Eight new columns added to `credit_transactions` — one outcome graph node per execution:

| Column | Type | Description |
|--------|------|-------------|
| `failure_code` | TEXT | `timeout` · `rate_limit` · `auth` · `unavailable` · `parse_error` — NULL on success |
| `task_query_text` | TEXT | Preceding search query (used for embedding, populated async) |
| `output_length_chars` | INT | Character count of response body — completeness proxy |
| `model_routing_attempted` | JSONB | Models tried in failover order (LLM paths only) |
| `model_routing_selected` | TEXT | Model that served the request (LLM paths only) |
| `substitution_from` | TEXT | Original slug when service substitution occurred |
| `substitution_to` | TEXT | Replacement slug used |
| `substitution_reason` | TEXT | Failure code that triggered substitution |

New `task_embeddings` table stores float32 embedding vectors (`REAL[]`) keyed to `transaction_id`. Populated hourly by `workers/embed_queries.py` via Jina Embeddings API (`jina-embeddings-v2-base-en`). Zero hot-path latency — strictly background.

New `GET /account/signal-summary` endpoint returns monthly aggregate: `executions_this_month`, `success_rate`, `credits_consumed`, `failure_breakdown` (all 5 codes), `top_services` (with per-service success rates), `substitution_events`, `top_substitution_pairs`, `avg_output_length_chars`.

Signal fields are populated on all three execution paths:
- **Managed `/execute`** — full instrumentation including substitution tracking
- **`/run` streaming (SSE)** — token accumulation + model routing in generator `finally` block via `asyncio.ensure_future`
- **`/run` non-streaming** — full instrumentation including fallback chain substitution
- **BYOK `/execute`** — `failure_code`, `output_length_chars`, `task_query_text`, model routing

---

## v0.8.2 — Gravity — 2026-05-29

Security hardening release. See PR #12 and PR #13.

### Security fixes (12 total across wayforth + wayforth-rank)

- Pioneer double-award race: atomic `UPDATE … WHERE pioneer_credits_awarded=FALSE RETURNING id`
- Provider boost activation race: atomic `UPDATE … WHERE boost_used=FALSE RETURNING id`
- Provider agents-tab cross-tenant data leak: scoped to `clicked_slug` + masked IDs
- `POST /submit` (custom_services, Growth-only) missing tier gate — added `require_tier`
- Pioneer 60/40 routing seed: re-seeded from server UUID (was client query text, manipulable)
- Provider session tokens stored hashed-only (completed half-done migration)
- Gateway: rate limiter now returns 429 (was headers-only)
- Gateway: IP-based throttle on repeated invalid API key attempts (enumeration)
- Gateway: plan tier bug fix (`getattr(dict, "plan")` always returned "free")
- Gateway: credit refund on upstream 5xx/timeout in `/execute`, `/run`, `/search`
- Gateway: outbound webhook HMAC signing (`X-Wayforth-Signature`)
- Gateway: webhook retries were dead (status='failed' fell out of pending index); fixed + 413 body limit

### Pioneer Program (initial)

- One-time award → monthly daily credit drip with 7-day rejoin cooldown
- `pioneer_cooldown_until` and `pioneer_last_drip_date` columns added
- Background drip loop: awards at UTC+00:05 daily, idempotent per calendar day

### Provider Dashboard

- Provider service management: `POST/PATCH/DELETE /provider/services/{slug}`
- `services.active` soft-delete column + search filters
- Startup config assertions for `REDIS_URL` (hard-fail in production) and `STRIPE_WEBHOOK_SECRET` (warn-only)

### Refund idempotency

- `_do_refund`: all 7 call sites now pass deterministic `refund_idempotency_key` seeded from request ID
- SSE stream refunds routed through `_do_refund` (previously raw UPDATE, no audit trail)
- Per-user refund rate alarm: >20 refunds/hour logs to admin audit log

### Admin hardening

- Legacy `/admin/*` `?key=` query param removed; `X-Admin-Key` header only
- `admin.html` scrubs key from URL immediately via `history.replaceState`

---

## v0.8.1 — Gravity

- Pioneer Program rollout
- Credits/calls language standardisation in email templates and webhook payloads
- API response aliases: `credits_remaining` / `credits_included` added alongside `calls_remaining` / `calls_included`

---

## v0.8.0 — Gravity

- LLM gateway: `POST /v1/chat/completions` with Groq → Together → Mistral failover and SSE streaming
- Provider Pioneer Boost: one-time WRI boost for Intelligence/Premium providers (15 or 30 days)
- Provider email verification gating write endpoints
- Admin audit log: append-only, trigger-enforced
- Webhook delivery retry queue with exponential backoff and suspension
- Trace ID propagation end-to-end (`X-Wayforth-Trace-ID`)
