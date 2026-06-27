-- ============================================================================
-- Data fix: deduplicate managed services & repair WRI base history
-- Date:    2026-06-03
-- Author:  platform / WRI recalibration
-- Context: the rank recalculate was returning 84.0 for several top
--          managed services. Root cause was a "base/clicks split": each of
--          these managed services had TWO rows —
--            * a source='managed'/canonical row that receives the click signal
--              (search_analytics.clicked_slug) but had NO service_score_history
--              entry, so base_wri defaulted to 60 → score 84.0; and
--            * a curated/seed duplicate row that DID have base_wri=90 but never
--              received clicks.
--          The matcher fix (recalculate now joins clicked_slug → services.slug
--          directly) exposed this for 8 services. This script consolidates each
--          pair: it copies the donor's latest base_wri onto the canonical row
--          and soft-retires (active=false) the duplicate so it cannot be matched
--          or surfaced again.
--
--          These are DATA fixes, not schema — recorded here for audit /
--          reproducibility. NOT part of infra/migrations. Idempotent-friendly:
--          re-running copies another (identical) base row and re-sets active.
--
-- Effect:  after running POST /admin/rank/recalculate, the 8 canonical rows
--          score from base_wri=90 (96.0 at 100% conversion + capped volume).
-- ============================================================================

BEGIN;

-- ── 8 base-history copies + 8 soft-retires ─────────────────────────────────
-- Helper pattern per pair (canonical_slug, donor_slug):
--   1) copy donor's latest service_score_history.wri_score onto canonical
--   2) UPDATE services SET active=false on the donor (soft-retire)

-- 1. tavily ← tavily_ai_search
INSERT INTO service_score_history (service_id, wri_score, rank_score, tier, consecutive_failures, recorded_at)
SELECT (SELECT id::text FROM services WHERE slug='tavily'),
       wri_score, rank_score, tier, consecutive_failures, NOW()
FROM service_score_history
WHERE service_id = (SELECT id::text FROM services WHERE slug='tavily_ai_search')
ORDER BY recorded_at DESC LIMIT 1;
UPDATE services SET active=false WHERE slug='tavily_ai_search';

-- 2. stability ← stability_ai
INSERT INTO service_score_history (service_id, wri_score, rank_score, tier, consecutive_failures, recorded_at)
SELECT (SELECT id::text FROM services WHERE slug='stability'),
       wri_score, rank_score, tier, consecutive_failures, NOW()
FROM service_score_history
WHERE service_id = (SELECT id::text FROM services WHERE slug='stability_ai')
ORDER BY recorded_at DESC LIMIT 1;
UPDATE services SET active=false WHERE slug='stability_ai';

-- 3. openweather ← openweathermap
INSERT INTO service_score_history (service_id, wri_score, rank_score, tier, consecutive_failures, recorded_at)
SELECT (SELECT id::text FROM services WHERE slug='openweather'),
       wri_score, rank_score, tier, consecutive_failures, NOW()
FROM service_score_history
WHERE service_id = (SELECT id::text FROM services WHERE slug='openweathermap')
ORDER BY recorded_at DESC LIMIT 1;
UPDATE services SET active=false WHERE slug='openweathermap';

-- 4. mistral ← mistral_ai
INSERT INTO service_score_history (service_id, wri_score, rank_score, tier, consecutive_failures, recorded_at)
SELECT (SELECT id::text FROM services WHERE slug='mistral'),
       wri_score, rank_score, tier, consecutive_failures, NOW()
FROM service_score_history
WHERE service_id = (SELECT id::text FROM services WHERE slug='mistral_ai')
ORDER BY recorded_at DESC LIMIT 1;
UPDATE services SET active=false WHERE slug='mistral_ai';

-- 5. together ← together_ai
INSERT INTO service_score_history (service_id, wri_score, rank_score, tier, consecutive_failures, recorded_at)
SELECT (SELECT id::text FROM services WHERE slug='together'),
       wri_score, rank_score, tier, consecutive_failures, NOW()
FROM service_score_history
WHERE service_id = (SELECT id::text FROM services WHERE slug='together_ai')
ORDER BY recorded_at DESC LIMIT 1;
UPDATE services SET active=false WHERE slug='together_ai';

-- 6. jina ← jina_reader   (jina_embeddings / jina_ai_remote_mcp_server are
--    distinct Jina products and are intentionally NOT retired)
INSERT INTO service_score_history (service_id, wri_score, rank_score, tier, consecutive_failures, recorded_at)
SELECT (SELECT id::text FROM services WHERE slug='jina'),
       wri_score, rank_score, tier, consecutive_failures, NOW()
FROM service_score_history
WHERE service_id = (SELECT id::text FROM services WHERE slug='jina_reader')
ORDER BY recorded_at DESC LIMIT 1;
UPDATE services SET active=false WHERE slug='jina_reader';

-- 7. alphavantage ← alpha_vantage
INSERT INTO service_score_history (service_id, wri_score, rank_score, tier, consecutive_failures, recorded_at)
SELECT (SELECT id::text FROM services WHERE slug='alphavantage'),
       wri_score, rank_score, tier, consecutive_failures, NOW()
FROM service_score_history
WHERE service_id = (SELECT id::text FROM services WHERE slug='alpha_vantage')
ORDER BY recorded_at DESC LIMIT 1;
UPDATE services SET active=false WHERE slug='alpha_vantage';

-- 8. brave ← brave_search_2   (mcp_brave_search is a distinct MCP wrapper and
--    is intentionally NOT retired)
INSERT INTO service_score_history (service_id, wri_score, rank_score, tier, consecutive_failures, recorded_at)
SELECT (SELECT id::text FROM services WHERE slug='brave'),
       wri_score, rank_score, tier, consecutive_failures, NOW()
FROM service_score_history
WHERE service_id = (SELECT id::text FROM services WHERE slug='brave_search_2')
ORDER BY recorded_at DESC LIMIT 1;
UPDATE services SET active=false WHERE slug='brave_search_2';

-- ── 1 gemini base seed ─────────────────────────────────────────────────────
-- gemini (managed, Tier 2) had NO service_score_history at all and no curated
-- donor to copy from, so its base_wri seeds at 90.0 — consistent with the other
-- verified Tier-2 managed services.
INSERT INTO service_score_history (service_id, wri_score, tier, consecutive_failures, recorded_at)
VALUES ((SELECT id::text FROM services WHERE slug='gemini'), 90.0, 2, 0, NOW());

COMMIT;

-- After commit: POST /admin/rank/recalculate to recompute scores.
