-- Migration 057: enforce UNIQUE email_canonical (FINDING-107).
--
-- v0.8.8 added email_canonical (migration 052) but deliberately left it
-- non-unique, so canonicalization was advisory only — the DB still allowed two
-- accounts that canonicalize to the same identity (alias farming, duplicate
-- free-credit/Launch-Boost grants). This migration makes the canonical identity
-- authoritative at the DB level.
--
-- SAFETY: a UNIQUE index fails if pre-existing duplicate canonical values exist.
-- Rather than silently mutate data, each DO block FLAGS the duplicates by
-- aborting the migration with the offending values listed, so an operator
-- resolves them deliberately (merge/rename) before re-running. Resolve with:
--   SELECT email_canonical, count(*), array_agg(email)
--     FROM users GROUP BY email_canonical HAVING count(*) > 1;

-- ── users ─────────────────────────────────────────────────────────────────────
DO $$
DECLARE
    dup_count int;
    dup_list  text;
BEGIN
    SELECT count(*),
           string_agg(email_canonical || ' (x' || c || ')', ', ')
      INTO dup_count, dup_list
      FROM (
        SELECT email_canonical, count(*) AS c
          FROM users
         WHERE email_canonical IS NOT NULL
         GROUP BY email_canonical
        HAVING count(*) > 1
      ) d;
    IF COALESCE(dup_count, 0) > 0 THEN
        RAISE EXCEPTION
          'FINDING-107: % duplicate users.email_canonical value(s) must be resolved before the UNIQUE index can be created: %',
          dup_count, dup_list;
    END IF;
END $$;

-- Partial unique index (NULLs allowed for any not-yet-backfilled rows).
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_canonical_unique
    ON users (email_canonical)
 WHERE email_canonical IS NOT NULL;

-- The non-unique lookup index from 052 is now redundant (the unique index
-- serves equality lookups on the same column).
DROP INDEX IF EXISTS idx_users_email_canonical;

-- ── providers ─────────────────────────────────────────────────────────────────
DO $$
DECLARE
    dup_count int;
    dup_list  text;
BEGIN
    SELECT count(*),
           string_agg(email_canonical || ' (x' || c || ')', ', ')
      INTO dup_count, dup_list
      FROM (
        SELECT email_canonical, count(*) AS c
          FROM providers
         WHERE email_canonical IS NOT NULL
         GROUP BY email_canonical
        HAVING count(*) > 1
      ) d;
    IF COALESCE(dup_count, 0) > 0 THEN
        RAISE EXCEPTION
          'FINDING-107: % duplicate providers.email_canonical value(s) must be resolved before the UNIQUE index can be created: %',
          dup_count, dup_list;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_providers_email_canonical_unique
    ON providers (email_canonical)
 WHERE email_canonical IS NOT NULL;

DROP INDEX IF EXISTS idx_providers_email_canonical;
