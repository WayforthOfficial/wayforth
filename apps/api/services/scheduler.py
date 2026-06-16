"""services/scheduler.py — Wayforth Cloud scheduled-run dispatcher.

Runs as an asyncio background task inside the API process (single replica).
Every SCHEDULER_INTERVAL_SECS it scans hosted_agents for agents whose
next_run_at has passed, validates constraints, and dispatches via the same
_dispatch_run_internal + _execute_run path that manual runs use.

Skipped run model:
  When constraints block dispatch (concurrency cap, balance, bad state),
  the scheduler writes an agent_runs row with status='skipped' instead of
  silently dropping the fire. This gives a full audit trail without leaking
  any reserved credits — no reservation is attempted before the checks pass.

Reserve / release guarantees:
  _dispatch_run_internal pre-reserves credits, _execute_run releases the
  unused portion at completion (or releases the full amount on failure).
  This file never calls _release_reserve directly; it relies on _execute_run
  to handle every exit path including timeouts, OOM, and code errors.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger("wayforth")

SCHEDULER_INTERVAL_SECS = 30

# Per-user concurrent-run caps, keyed by package_tier.
_CONCURRENT_USER_CAP: dict[str, int] = {
    "free":       1,
    "starter":    1,
    "builder":    2,
    "pro":        5,
    "growth":     10,
    "enterprise": 25,
}


def _compute_next_run(schedule: str) -> datetime | None:
    """Return the next fire time for a cron expression (UTC)."""
    try:
        from croniter import croniter
        it = croniter(schedule, datetime.now(timezone.utc))
        return it.get_next(datetime)
    except Exception as exc:
        logger.warning("scheduler: invalid cron expression %r: %s", schedule, exc)
        return None


async def run_scheduler(pool) -> None:
    """Main scheduler loop — start as an asyncio task in main.py lifespan."""
    logger.info("Cloud scheduler started (interval=%ds)", SCHEDULER_INTERVAL_SECS)
    while True:
        try:
            await _tick(pool)
        except asyncio.CancelledError:
            logger.info("Cloud scheduler cancelled — shutting down")
            raise
        except Exception as exc:
            logger.exception("Scheduler tick failed: %s", exc)
        await asyncio.sleep(SCHEDULER_INTERVAL_SECS)


async def _tick(pool) -> None:
    """Single scheduler tick: find due agents and dispatch or skip."""
    async with pool.acquire() as conn:
        # FOR UPDATE SKIP LOCKED — safe even on single replica; prevents double-
        # dispatch if a future scale-out occurs before we move to a queue.
        due = await conn.fetch("""
            SELECT ha.*,
                   uc.credits_balance,
                   uc.package_tier AS tier
            FROM   hosted_agents ha
            JOIN   user_credits uc ON uc.user_id = ha.user_id
            WHERE  ha.trigger_type = 'schedule'
              AND  ha.next_run_at  IS NOT NULL
              AND  ha.next_run_at  <= NOW()
              AND  ha.status NOT IN ('draft')
            FOR UPDATE OF ha SKIP LOCKED
            LIMIT 50
        """)

    if not due:
        return

    logger.info("Scheduler tick: %d agent(s) due", len(due))
    for row in due:
        await _maybe_dispatch(pool, dict(row))


async def _maybe_dispatch(pool, agent: dict) -> None:
    """Validate constraints, dispatch or write a skipped row, update next_run_at."""
    # Deferred import to avoid circular dependency at module load time.
    from routers.cloud import _dispatch_run_internal

    user_id     = str(agent["user_id"])
    agent_id    = str(agent["id"])
    credit_cap  = agent.get("credit_cap") or 0
    balance     = int(agent.get("credits_balance") or 0)
    tier        = agent.get("tier") or "free"
    concurrent_max = int(agent.get("concurrent_max") or 1)
    schedule    = agent.get("schedule") or ""

    next_run = _compute_next_run(schedule)

    async with pool.acquire() as conn:
        # Per-agent concurrency: how many runs are actively queued/running?
        agent_active = int(await conn.fetchval("""
            SELECT COUNT(*) FROM agent_runs
            WHERE hosted_agent_id = $1::uuid
              AND status IN ('queued', 'running')
        """, agent_id) or 0)

        # Per-user tier cap: how many of this user's runs are active?
        user_active = int(await conn.fetchval("""
            SELECT COUNT(*) FROM agent_runs
            WHERE user_id = $1::uuid
              AND status IN ('queued', 'running')
        """, user_id) or 0)

        tier_cap = _CONCURRENT_USER_CAP.get(tier, 0)

        # Evaluate skip conditions — most specific first for clearest reason
        skip_reason: str | None = None
        if agent_active >= concurrent_max:
            skip_reason = (
                f"agent has {agent_active} active run(s) "
                f"(concurrent_max={concurrent_max})"
            )
        elif tier_cap > 0 and user_active >= tier_cap:
            skip_reason = (
                f"user at concurrent-run cap "
                f"({user_active}/{tier_cap} for {tier} tier)"
            )
        elif credit_cap > 0 and balance < credit_cap:
            skip_reason = (
                f"insufficient balance "
                f"({balance} credits < credit_cap {credit_cap})"
            )

        if skip_reason:
            run_id = str(uuid.uuid4())
            await conn.execute("""
                INSERT INTO agent_runs
                  (id, hosted_agent_id, user_id, status, trigger,
                   error_type, error_message)
                VALUES ($1::uuid, $2::uuid, $3::uuid,
                        'skipped', 'schedule', 'skipped', $4)
            """, run_id, agent_id, user_id, skip_reason)
            logger.info(
                "Scheduled run skipped agent=%s reason=%s",
                agent_id[:8], skip_reason,
            )
            # Still advance next_run_at so we try again next cycle
            if next_run:
                await conn.execute(
                    "UPDATE hosted_agents SET next_run_at = $1 WHERE id = $2::uuid",
                    next_run, agent_id,
                )
            return

        # Decode runner key for sandbox injection
        runner_key = _decrypt_runner_key(agent)
        if not runner_key:
            run_id = str(uuid.uuid4())
            await conn.execute("""
                INSERT INTO agent_runs
                  (id, hosted_agent_id, user_id, status, trigger,
                   error_type, error_message)
                VALUES ($1::uuid, $2::uuid, $3::uuid,
                        'skipped', 'schedule', 'no_runner_key',
                        'Agent has no runner key — recreate or update the agent to store one')
            """, run_id, agent_id, user_id)
            logger.warning("Scheduled run skipped agent=%s: no runner key", agent_id[:8])
            if next_run:
                await conn.execute(
                    "UPDATE hosted_agents SET next_run_at = $1 WHERE id = $2::uuid",
                    next_run, agent_id,
                )
            return

        # All checks pass — dispatch
        try:
            run_id, credits_reserved = await _dispatch_run_internal(
                pool, conn, agent, user_id, runner_key, trigger="schedule",
            )
            logger.info(
                "Scheduled run dispatched agent=%s run=%s reserved=%d",
                agent_id[:8], run_id[:8], credits_reserved,
            )
        except Exception as exc:
            logger.exception(
                "Scheduled dispatch failed agent=%s: %s", agent_id[:8], exc
            )

        # Always advance next_run_at regardless of dispatch outcome
        if next_run:
            await conn.execute(
                "UPDATE hosted_agents SET next_run_at = $1 WHERE id = $2::uuid",
                next_run, agent_id,
            )


def _decrypt_runner_key(agent: dict) -> str | None:
    """Decrypt and return the stored runner API key, or None if not set."""
    ct = agent.get("runner_key_encrypted")
    version = int(agent.get("runner_key_version") or 1)
    if not ct:
        return None
    try:
        from core.auth import decrypt_api_key
        return decrypt_api_key(ct, version)
    except Exception as exc:
        logger.warning("Failed to decrypt runner key: %s", exc)
        return None
