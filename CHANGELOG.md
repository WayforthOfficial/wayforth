# Changelog

All notable changes to the Wayforth platform are documented here.

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
