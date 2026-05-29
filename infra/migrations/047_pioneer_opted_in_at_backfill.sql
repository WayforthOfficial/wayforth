-- Migration 047 (v0.8.2): Backfill pioneer_opted_in_at for users who were
-- already enrolled before the column existed or before the join path started
-- setting it. Sets it to NOW() as a conservative approximation — these users
-- are currently opted in, so "enrolled since now" is at least not a null/dash.
UPDATE users
   SET pioneer_opted_in_at = NOW()
 WHERE pioneer_opt_in = TRUE
   AND pioneer_opted_in_at IS NULL;
