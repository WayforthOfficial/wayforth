-- Best-effort backfill: search_analytics has no api_key_id column so this
-- produces 0 rows updated. Included for audit trail.
DO $$
DECLARE updated_count INT;
BEGIN
    UPDATE search_analytics sa
    SET user_id = u.id
    FROM users u
    JOIN api_keys ak ON ak.owner_email = u.email
    WHERE sa.api_key_id = ak.id
      AND sa.user_id IS NULL;
    GET DIAGNOSTICS updated_count = ROW_COUNT;
    RAISE NOTICE 'Backfilled % search_analytics rows', updated_count;
END $$;
