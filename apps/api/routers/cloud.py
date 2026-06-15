"""routers/cloud.py — Wayforth Cloud: hosted agent deploy + run API.

Agent lifecycle:
  POST /cloud/agents              — create agent
  PUT  /cloud/agents/{id}/code    — upload code (text body)
  GET  /cloud/agents              — list agents
  GET  /cloud/agents/{id}         — agent detail + last run summary
  POST /cloud/agents/{id}/runs    — dispatch a run (async, returns 202)
  GET  /cloud/agents/{id}/runs    — run history
  GET  /cloud/agents/{id}/runs/{run_id}          — run detail
  POST /cloud/agents/{id}/runs/{run_id}/cancel   — cancel queued/running run
  DELETE /cloud/agents/{id}       — delete agent + all run history

Security model:
  - Sandbox isolation: E2B Firecracker microVMs (VM-level, approved)
  - Secrets: AES-256-GCM at rest, decrypt-at-dispatch, never logged
  - Network: RFC-1918 + metadata egress denied at sandbox level
  - Credits: proxy calls deduct from owner's balance normally;
    compute charge (1 credit/min, ceil) deducted at run completion

WayforthRank data path:
  At run completion, credit_transactions WHERE agent_id = run_id are
  reconciled and written to agent_runs.services_called / failover_events /
  substitutions — this is the primary signal path for Cloud moat data.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.agent_secrets import decrypt_env, encrypt_env
from core.auth import _resolve_user, decrypt_api_key, encrypt_api_key
from core.credits import check_and_deduct_credits
from core.db import get_db
from core.rate_limit import limiter
from core.tier_gates import require_tier, CONCURRENT_RUNS_PER_USER
from services.sandbox import compute_credits_for_run, get_provider

logger = logging.getLogger("wayforth")

router = APIRouter(prefix="/cloud", tags=["Cloud"])

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,61}[a-z0-9]$")
_VALID_RUNTIMES = {"python3.12", "node20"}
_VALID_TRIGGERS = {"manual", "schedule", "webhook"}
_MAX_CODE_BYTES = 512 * 1024   # 512 KB
_DEFAULT_TIMEOUT = 300         # 5 min
_MAX_TIMEOUT = 1800            # 30 min


# ── Pydantic models ───────────────────────────────────────────────────────────

class CreateAgentBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    slug: str = Field(..., description="URL-safe identifier, unique per account")
    runtime: str = Field("python3.12", description="python3.12 | node20")
    trigger_type: str = Field("manual", description="manual | schedule | webhook")
    schedule: str | None = Field(None, description="Cron expression (required for schedule trigger)")
    env: dict[str, str] = Field(default_factory=dict, description="User-supplied env vars (encrypted at rest)")
    credit_cap: int | None = Field(None, ge=1, description="Max credits per run; null = unlimited")


class UpdateAgentBody(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    trigger_type: str | None = None
    schedule: str | None = None
    env: dict[str, str] | None = None
    credit_cap: int | None = Field(None, ge=1)
    concurrent_max: int | None = Field(None, ge=1)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_agent_or_404(db, user_id: str, agent_id: str) -> dict:
    row = await db.fetchrow(
        "SELECT * FROM hosted_agents WHERE id = $1::uuid AND user_id = $2::uuid",
        agent_id, user_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail={"error": "agent_not_found"})
    return dict(row)


async def _resolve_caller(request: Request, db) -> tuple[str, str, str]:
    """Returns (user_id, api_key_id, tier)."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail={"error": "X-Wayforth-API-Key required"})
    user_id, api_key_id, tier = await _resolve_user(db, api_key)
    return str(user_id), str(api_key_id), tier


async def _reconcile_run_signals(pool, run_id: str, user_id: str) -> dict[str, Any]:
    """Query credit_transactions attributed to this run and build WayforthRank signal dict."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                service_id,
                ABS(amount)           AS credits,
                substitution_from,
                substitution_to,
                substitution_reason,
                created_at
            FROM credit_transactions
            WHERE user_id = $1::uuid
              AND agent_id = $2
              AND type IN ('execution', 'cross_rail')
            ORDER BY created_at
        """, user_id, run_id)

    service_map: dict[str, dict] = {}
    substitutions = []
    failover_events = 0
    total_proxy_credits = 0

    for r in rows:
        slug = r["service_id"] or "unknown"
        credits = int(r["credits"] or 0)
        total_proxy_credits += credits

        entry = service_map.setdefault(slug, {"slug": slug, "calls": 0, "credits_spent": 0})
        entry["calls"] += 1
        entry["credits_spent"] += credits

        if r["substitution_from"]:
            failover_events += 1
            substitutions.append({
                "from":      r["substitution_from"],
                "to":        r["substitution_to"],
                "reason":    r["substitution_reason"],
                "timestamp": r["created_at"].isoformat() if r["created_at"] else None,
            })

    return {
        "services_called":  list(service_map.values()),
        "failover_events":  failover_events,
        "substitutions":    substitutions,
        "credits_proxy":    total_proxy_credits,
    }


async def _release_reserve(pool, user_id: str, amount: int, run_id: str) -> None:
    """Return unused pre-reserved credits to the balance.

    Writes a credit_transactions row with type='agent_release' so the
    pre-reserve/release cycle is fully auditable in the transactions ledger.
    """
    if amount <= 0:
        return
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE user_credits SET credits_balance = credits_balance + $1, "
                "updated_at = NOW() WHERE user_id = $2::uuid RETURNING credits_balance",
                amount, user_id,
            )
            new_bal = row["credits_balance"] if row else 0
            await conn.execute("""
                INSERT INTO credit_transactions
                  (user_id, amount, balance_after, type, description, service_id, agent_id)
                VALUES ($1::uuid, $2, $3, 'agent_release',
                        'Cloud run reserve release', 'cloud_compute', $4)
            """, user_id, amount, new_bal, run_id)


def _compute_next_run(schedule: str) -> "datetime | None":
    """Return the next fire time for a cron expression (UTC), or None on error."""
    try:
        from croniter import croniter
        return croniter(schedule, datetime.now(timezone.utc)).get_next(datetime)
    except Exception:
        return None


async def _dispatch_run_internal(
    pool,
    db,
    agent: dict,
    user_id: str,
    user_api_key: str,
    trigger: str = "manual",
) -> tuple[str, int]:
    """Reserve credits, insert agent_runs, fire _execute_run background task.

    Returns (run_id, credits_reserved).

    Caller must validate concurrency limits and balance BEFORE calling —
    this function does not re-check those constraints so it can be called
    identically from the HTTP endpoint, the scheduler, and the webhook handler.
    """
    credit_cap = agent.get("credit_cap") or 0
    credits_reserved = 0
    run_id = str(uuid.uuid4())

    if credit_cap > 0:
        await check_and_deduct_credits(
            db, user_id, credit_cap,
            f"/cloud/agents/{agent['id']}/runs",
            service_id="cloud_compute",
            tx_type="agent_reserve",
            agent_id=run_id,
        )
        credits_reserved = credit_cap

    await db.execute("""
        INSERT INTO agent_runs
          (id, hosted_agent_id, user_id, status, trigger, credits_reserved)
        VALUES ($1::uuid, $2::uuid, $3::uuid, 'queued', $4, $5)
    """, run_id, agent["id"], user_id, trigger, credits_reserved)

    await db.execute(
        "UPDATE hosted_agents SET status = 'running', updated_at = NOW() "
        "WHERE id = $1::uuid",
        agent["id"],
    )

    asyncio.create_task(
        _execute_run(pool, run_id, dict(agent), user_id, user_api_key, credits_reserved)
    )

    return run_id, credits_reserved


async def _execute_run(
    pool,
    run_id: str,
    agent: dict,
    user_id: str,
    user_api_key: str,
    credits_reserved: int,
) -> None:
    """Background task: dispatch to E2B sandbox, settle credits, write WayforthRank signals.

    Pre-reserve model:
      credits_reserved  — deducted from balance at dispatch (= credit_cap when set, else 0)
      credits_proxy     — proxy call deductions during the run (from credit_transactions)
      credits_compute   — 1 credit/min compute charge, deducted at completion
      credits_released  — max(0, reserved - proxy - compute), returned to balance at completion
    """

    async def _update(status: str, **fields) -> None:
        sets = ["status = $1"]
        vals: list = [status]
        for k, v in fields.items():
            vals.append(v)
            sets.append(f"{k} = ${len(vals)}")
        vals.append(run_id)
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE agent_runs SET {', '.join(sets)} WHERE id = ${len(vals)}::uuid",
                *vals,
            )

    try:
        await _update("running", started_at=datetime.now(timezone.utc))

        # Decrypt user env vars at dispatch — decrypted value never stored or logged
        user_env: dict[str, str] = {}
        if agent.get("env_encrypted"):
            user_env = decrypt_env(bytes(agent["env_encrypted"]))

        run_env = {
            **user_env,
            "WAYFORTH_API_KEY": user_api_key,
            "X_WAYFORTH_AGENT_ID": run_id,  # tags all proxy credit_transactions to this run
        }

        async with pool.acquire() as conn:
            code_row = await conn.fetchrow(
                "SELECT code FROM hosted_agents WHERE id = $1::uuid", agent["id"]
            )
        code = (code_row or {}).get("code") or ""
        if not code.strip():
            if credits_reserved > 0:
                await _release_reserve(pool, user_id, credits_reserved, run_id)
            await _update(
                "failed",
                completed_at=datetime.now(timezone.utc),
                credits_reserved=credits_reserved,
                credits_released=credits_reserved,
                error_type="no_code",
                error_message="No code uploaded for this agent.",
            )
            return

        timeout_s = _DEFAULT_TIMEOUT
        provider = get_provider(agent.get("sandbox_provider") or "e2b")

        result = await provider.run(
            code=code,
            runtime=agent["runtime"],
            env=run_env,
            timeout_seconds=min(timeout_s, _MAX_TIMEOUT),
        )

        # Deduct compute charge (1 credit/min, ceil, min 1)
        compute_credits = compute_credits_for_run(result.duration_ms)
        async with pool.acquire() as conn:
            await check_and_deduct_credits(
                conn, user_id, compute_credits,
                f"/cloud/agents/{agent['id']}/runs",
                service_id="cloud_compute",
                tx_type="cloud_compute",
                agent_id=run_id,
            )

        # Reconcile WayforthRank signals from proxy calls during this run
        signals = await _reconcile_run_signals(pool, run_id, user_id)
        proxy_credits = signals["credits_proxy"]

        # Release unused pre-reserve: reserved - (proxy + compute), min 0
        actual_spend = proxy_credits + compute_credits
        credits_released = max(0, credits_reserved - actual_spend)
        if credits_released > 0:
            await _release_reserve(pool, user_id, credits_released, run_id)

        log_combined = f"=== STDOUT ===\n{result.stdout}\n=== STDERR ===\n{result.stderr}"
        log_tail = log_combined[-4096:] if len(log_combined) > 4096 else log_combined

        final_status = "completed" if result.exit_code == 0 else "failed"
        error_type = None
        if result.exit_code != 0:
            err = (result.stderr or "").lower()
            if "timeout" in err or "timed out" in err:
                error_type = "timeout"
            elif "memoryerror" in err or "killed" in err:
                error_type = "oom"
            else:
                error_type = "code_error"

        await _update(
            final_status,
            completed_at=datetime.now(timezone.utc),
            duration_ms=result.duration_ms,
            exit_code=result.exit_code,
            sandbox_id=result.sandbox_id,
            credits_reserved=credits_reserved,
            credits_compute=compute_credits,
            credits_proxy=proxy_credits,
            credits_total=actual_spend,
            credits_released=credits_released,
            services_called=json.dumps(signals["services_called"]),
            failover_events=signals["failover_events"],
            substitutions=json.dumps(signals["substitutions"]),
            error_type=error_type,
            error_message=result.stderr[:2000] if result.exit_code != 0 else None,
            log_tail=log_tail,
        )

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE hosted_agents SET last_run_at = NOW(), status = 'ready', "
                "updated_at = NOW() WHERE id = $1::uuid",
                agent["id"],
            )

    except asyncio.CancelledError:
        if credits_reserved > 0:
            await _release_reserve(pool, user_id, credits_reserved, run_id)
        await _update(
            "cancelled",
            completed_at=datetime.now(timezone.utc),
            credits_reserved=credits_reserved,
            credits_released=credits_reserved,
        )
    except Exception as exc:
        logger.exception("Cloud run %s failed: %s", run_id, exc)
        if credits_reserved > 0:
            await _release_reserve(pool, user_id, credits_reserved, run_id)
        await _update(
            "failed",
            completed_at=datetime.now(timezone.utc),
            credits_reserved=credits_reserved,
            credits_released=credits_reserved,
            error_type="sandbox_error",
            error_message=str(exc)[:2000],
        )


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/agents", status_code=201)
@limiter.limit("30/minute")
async def create_agent(
    request: Request,
    body: CreateAgentBody,
    db=Depends(get_db),
) -> dict:
    user_id, _, tier = await _resolve_caller(request, db)
    require_tier(tier, "cloud_agents")

    if not _SLUG_RE.match(body.slug):
        raise HTTPException(status_code=422, detail={
            "error": "invalid_slug",
            "message": "slug must be 2-63 chars, lowercase alphanumeric and hyphens, "
                       "not starting or ending with a hyphen",
        })
    if body.runtime not in _VALID_RUNTIMES:
        raise HTTPException(status_code=422, detail={"error": "invalid_runtime",
            "valid": sorted(_VALID_RUNTIMES)})
    if body.trigger_type not in _VALID_TRIGGERS:
        raise HTTPException(status_code=422, detail={"error": "invalid_trigger_type",
            "valid": sorted(_VALID_TRIGGERS)})
    if body.trigger_type == "schedule" and not body.schedule:
        raise HTTPException(status_code=422, detail={"error": "schedule_required",
            "message": "schedule (cron expression) is required for trigger_type='schedule'"})

    env_encrypted = encrypt_env(body.env) if body.env else None

    # Encrypt caller's API key for scheduler/webhook dispatch (runner key).
    # Never stored decrypted — same Fernet key as BYOK key encryption.
    runner_key_ct: str | None = None
    runner_key_ver: int = 1
    api_key_header = request.headers.get("X-Wayforth-API-Key", "")
    if api_key_header:
        try:
            runner_key_ct, runner_key_ver = encrypt_api_key(api_key_header)
        except Exception:
            pass  # ENCRYPTION_KEY not configured; scheduled/webhook runs will skip

    next_run_at = None
    if body.trigger_type == "schedule" and body.schedule:
        next_run_at = _compute_next_run(body.schedule)

    try:
        agent_id = await db.fetchval("""
            INSERT INTO hosted_agents
              (user_id, name, slug, runtime, trigger_type, schedule,
               env_encrypted, credit_cap, sandbox_provider,
               runner_key_encrypted, runner_key_version, next_run_at)
            VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, 'e2b', $9, $10, $11)
            RETURNING id
        """,
            user_id, body.name, body.slug, body.runtime,
            body.trigger_type, body.schedule,
            env_encrypted, body.credit_cap,
            runner_key_ct, runner_key_ver, next_run_at,
        )
    except Exception as exc:
        if "unique" in str(exc).lower():
            raise HTTPException(status_code=409, detail={
                "error": "slug_conflict",
                "message": f"An agent with slug '{body.slug}' already exists in your account.",
            })
        raise

    # Fetch the webhook_id generated by the DB default
    wh_row = await db.fetchrow(
        "SELECT webhook_id FROM hosted_agents WHERE id = $1::uuid", agent_id
    )
    webhook_id = str(wh_row["webhook_id"]) if wh_row and wh_row["webhook_id"] else None

    return {
        "id":           str(agent_id),
        "name":         body.name,
        "slug":         body.slug,
        "runtime":      body.runtime,
        "status":       "draft",
        "trigger_type": body.trigger_type,
        "webhook_id":   webhook_id,
        "next_run_at":  next_run_at.isoformat() if next_run_at else None,
        "created_at":   datetime.now(timezone.utc).isoformat(),
        "next_step":    f"Upload code: PUT /cloud/agents/{agent_id}/code",
    }


@router.put("/agents/{agent_id}/code", status_code=200)
@limiter.limit("30/minute")
async def upload_code(
    request: Request,
    agent_id: str,
    db=Depends(get_db),
) -> dict:
    user_id, _, tier = await _resolve_caller(request, db)
    require_tier(tier, "cloud_agents")

    agent = await _get_agent_or_404(db, user_id, agent_id)
    if agent["status"] == "running":
        raise HTTPException(status_code=409, detail={"error": "agent_running",
            "message": "Cannot upload code while a run is in progress."})

    body_bytes = await request.body()
    if len(body_bytes) > _MAX_CODE_BYTES:
        raise HTTPException(status_code=413, detail={"error": "code_too_large",
            "max_bytes": _MAX_CODE_BYTES})

    code = body_bytes.decode("utf-8", errors="replace")

    await db.execute(
        "UPDATE hosted_agents SET code = $1, status = 'ready', updated_at = NOW() "
        "WHERE id = $2::uuid",
        code, agent_id,
    )

    ext = "ts" if agent["runtime"] == "node20" else "py"
    return {
        "id": agent_id,
        "status": "ready",
        "size_bytes": len(body_bytes),
        "runtime": agent["runtime"],
        "message": f"Code uploaded. Run: POST /cloud/agents/{agent_id}/runs",
        "file": f"agent.{ext}",
    }


@router.get("/agents")
@limiter.limit("60/minute")
async def list_agents(request: Request, db=Depends(get_db)) -> dict:
    user_id, _, tier = await _resolve_caller(request, db)
    require_tier(tier, "cloud_agents")

    rows = await db.fetch("""
        SELECT id, name, slug, runtime, status, trigger_type,
               credit_cap, last_run_at, created_at
        FROM hosted_agents
        WHERE user_id = $1::uuid
        ORDER BY created_at DESC
        LIMIT 100
    """, user_id)

    return {
        "agents": [
            {
                "id":           str(r["id"]),
                "name":         r["name"],
                "slug":         r["slug"],
                "runtime":      r["runtime"],
                "status":       r["status"],
                "trigger_type": r["trigger_type"],
                "credit_cap":   r["credit_cap"],
                "last_run_at":  r["last_run_at"].isoformat() if r["last_run_at"] else None,
                "created_at":   r["created_at"].isoformat(),
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.get("/agents/{agent_id}")
@limiter.limit("60/minute")
async def get_agent(request: Request, agent_id: str, db=Depends(get_db)) -> dict:
    user_id, _, tier = await _resolve_caller(request, db)
    require_tier(tier, "cloud_agents")

    agent = await _get_agent_or_404(db, user_id, agent_id)

    # Last run summary
    last_run = await db.fetchrow("""
        SELECT id, status, trigger, started_at, completed_at, duration_ms,
               credits_total, error_type
        FROM agent_runs
        WHERE hosted_agent_id = $1::uuid
        ORDER BY created_at DESC LIMIT 1
    """, agent_id)

    return {
        "id":             str(agent["id"]),
        "name":           agent["name"],
        "slug":           agent["slug"],
        "runtime":        agent["runtime"],
        "status":         agent["status"],
        "trigger_type":   agent["trigger_type"],
        "schedule":       agent["schedule"],
        "credit_cap":     agent["credit_cap"],
        "concurrent_max": agent.get("concurrent_max") or 1,
        "sandbox_provider": agent["sandbox_provider"],
        "webhook_id":     str(agent["webhook_id"]) if agent.get("webhook_id") else None,
        "next_run_at":    agent["next_run_at"].isoformat() if agent.get("next_run_at") else None,
        "last_run_at":    agent["last_run_at"].isoformat() if agent["last_run_at"] else None,
        "created_at":     agent["created_at"].isoformat(),
        "last_run": {
            "id":           str(last_run["id"]),
            "status":       last_run["status"],
            "trigger":      last_run["trigger"],
            "started_at":   last_run["started_at"].isoformat() if last_run["started_at"] else None,
            "completed_at": last_run["completed_at"].isoformat() if last_run["completed_at"] else None,
            "duration_ms":  last_run["duration_ms"],
            "credits_total": last_run["credits_total"],
            "error_type":   last_run["error_type"],
        } if last_run else None,
    }


@router.post("/agents/{agent_id}/runs", status_code=202)
@limiter.limit("20/minute")
async def dispatch_run(
    request: Request,
    agent_id: str,
    db=Depends(get_db),
) -> dict:
    user_id, _, tier = await _resolve_caller(request, db)
    require_tier(tier, "cloud_agents")

    agent = await _get_agent_or_404(db, user_id, agent_id)

    if agent["status"] == "draft":
        raise HTTPException(status_code=409, detail={"error": "no_code",
            "message": "Upload code first: PUT /cloud/agents/{id}/code"})

    # Per-agent concurrency check
    concurrent_max = int(agent.get("concurrent_max") or 1)
    active_count = int(await db.fetchval("""
        SELECT COUNT(*) FROM agent_runs
        WHERE hosted_agent_id = $1::uuid AND status IN ('queued', 'running')
    """, agent_id) or 0)
    if active_count >= concurrent_max:
        raise HTTPException(status_code=409, detail={
            "error": "concurrent_limit",
            "active_runs": active_count,
            "concurrent_max": concurrent_max,
            "message": f"Agent has {active_count} active run(s) (concurrent_max={concurrent_max}).",
        })

    # Per-user tier concurrency cap
    tier_cap = CONCURRENT_RUNS_PER_USER.get(tier, 0)
    if tier_cap > 0:
        user_active = int(await db.fetchval("""
            SELECT COUNT(*) FROM agent_runs
            WHERE user_id = $1::uuid AND status IN ('queued', 'running')
        """, user_id) or 0)
        if user_active >= tier_cap:
            raise HTTPException(status_code=429, detail={
                "error": "concurrent_run_cap",
                "message": f"Concurrent run limit ({tier_cap}) reached for {tier} tier.",
                "upgrade_url": "https://wayforth.io/pricing",
            })

    # Balance + pre-reserve checks
    balance_row = await db.fetchrow(
        "SELECT credits_balance FROM user_credits WHERE user_id = $1::uuid", user_id
    )
    balance = int((balance_row or {}).get("credits_balance") or 0)
    if balance < 1:
        raise HTTPException(status_code=402, detail={"error": "insufficient_credits",
            "message": "Insufficient credits to run an agent. Top up at wayforth.io/billing"})

    credit_cap = agent.get("credit_cap") or 0
    if credit_cap > 0 and balance < credit_cap:
        raise HTTPException(status_code=402, detail={
            "error": "insufficient_credits",
            "message": f"This agent requires {credit_cap} credits reserved. "
                       f"You have {balance}. Top up at wayforth.io/billing",
            "balance": balance,
            "credit_cap": credit_cap,
        })

    api_key_header = request.headers.get("X-Wayforth-API-Key", "")

    from main import app
    run_id, credits_reserved = await _dispatch_run_internal(
        app.state.pool, db, dict(agent), user_id, api_key_header, trigger="manual",
    )

    return {
        "run_id":           run_id,
        "agent_id":         agent_id,
        "status":           "queued",
        "credits_reserved": credits_reserved,
        "message":          "Run dispatched. Poll GET /cloud/agents/{id}/runs/{run_id} for status.",
        "poll_url":         f"/cloud/agents/{agent_id}/runs/{run_id}",
    }


@router.get("/agents/{agent_id}/runs")
@limiter.limit("60/minute")
async def list_runs(
    request: Request,
    agent_id: str,
    limit: int = 20,
    db=Depends(get_db),
) -> dict:
    user_id, _, tier = await _resolve_caller(request, db)
    require_tier(tier, "cloud_agents")

    await _get_agent_or_404(db, user_id, agent_id)
    limit = min(max(1, limit), 100)

    rows = await db.fetch("""
        SELECT id, status, trigger, started_at, completed_at, duration_ms,
               credits_compute, credits_proxy, credits_total, error_type, created_at
        FROM agent_runs
        WHERE hosted_agent_id = $1::uuid
        ORDER BY created_at DESC
        LIMIT $2
    """, agent_id, limit)

    return {
        "agent_id": agent_id,
        "runs": [
            {
                "id":            str(r["id"]),
                "status":        r["status"],
                "trigger":       r["trigger"],
                "started_at":    r["started_at"].isoformat() if r["started_at"] else None,
                "completed_at":  r["completed_at"].isoformat() if r["completed_at"] else None,
                "duration_ms":   r["duration_ms"],
                "credits_compute": r["credits_compute"],
                "credits_proxy":   r["credits_proxy"],
                "credits_total":   r["credits_total"],
                "error_type":    r["error_type"],
                "created_at":    r["created_at"].isoformat(),
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.get("/agents/{agent_id}/runs/{run_id}")
@limiter.limit("60/minute")
async def get_run(
    request: Request,
    agent_id: str,
    run_id: str,
    db=Depends(get_db),
) -> dict:
    user_id, _, tier = await _resolve_caller(request, db)
    require_tier(tier, "cloud_agents")

    await _get_agent_or_404(db, user_id, agent_id)

    row = await db.fetchrow("""
        SELECT id, status, trigger, sandbox_id, started_at, completed_at, duration_ms,
               exit_code, credits_reserved, credits_compute, credits_proxy,
               credits_total, credits_released,
               services_called, failover_events, substitutions,
               error_type, error_message, log_tail, created_at
        FROM agent_runs
        WHERE id = $1::uuid AND hosted_agent_id = $2::uuid
    """, run_id, agent_id)

    if not row:
        raise HTTPException(status_code=404, detail={"error": "run_not_found"})

    def _parse_jsonb(v):
        if v is None:
            return []
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return []
        return v

    return {
        "id":            str(row["id"]),
        "agent_id":      agent_id,
        "status":        row["status"],
        "trigger":       row["trigger"],
        "sandbox_id":    row["sandbox_id"],
        "started_at":    row["started_at"].isoformat() if row["started_at"] else None,
        "completed_at":  row["completed_at"].isoformat() if row["completed_at"] else None,
        "duration_ms":   row["duration_ms"],
        "exit_code":     row["exit_code"],
        "credits": {
            "reserved": row["credits_reserved"],
            "compute":  row["credits_compute"],
            "proxy":    row["credits_proxy"],
            "total":    row["credits_total"],
            "released": row["credits_released"],
        },
        "wayforthrank": {
            "services_called":  _parse_jsonb(row["services_called"]),
            "failover_events":  row["failover_events"],
            "substitutions":    _parse_jsonb(row["substitutions"]),
        },
        "error_type":    row["error_type"],
        "error_message": row["error_message"],
        "log_tail":      row["log_tail"],
        "created_at":    row["created_at"].isoformat(),
    }


@router.post("/agents/{agent_id}/runs/{run_id}/cancel", status_code=200)
@limiter.limit("20/minute")
async def cancel_run(
    request: Request,
    agent_id: str,
    run_id: str,
    db=Depends(get_db),
) -> dict:
    user_id, _, tier = await _resolve_caller(request, db)
    require_tier(tier, "cloud_agents")

    await _get_agent_or_404(db, user_id, agent_id)

    row = await db.fetchrow(
        "SELECT status FROM agent_runs WHERE id = $1::uuid AND hosted_agent_id = $2::uuid",
        run_id, agent_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail={"error": "run_not_found"})
    if row["status"] not in ("queued", "running"):
        raise HTTPException(status_code=409, detail={
            "error": "run_not_cancellable",
            "status": row["status"],
        })

    await db.execute("""
        UPDATE agent_runs
        SET status = 'cancelled', completed_at = NOW()
        WHERE id = $1::uuid
    """, run_id)
    await db.execute(
        "UPDATE hosted_agents SET status = 'ready', updated_at = NOW() WHERE id = $1::uuid",
        agent_id,
    )

    return {"run_id": run_id, "status": "cancelled"}


@router.patch("/agents/{agent_id}", status_code=200)
@limiter.limit("30/minute")
async def update_agent(
    request: Request,
    agent_id: str,
    body: UpdateAgentBody,
    db=Depends(get_db),
) -> dict:
    user_id, _, tier = await _resolve_caller(request, db)
    require_tier(tier, "cloud_agents")

    agent = await _get_agent_or_404(db, user_id, agent_id)
    if agent["status"] == "running":
        raise HTTPException(status_code=409, detail={"error": "agent_running",
            "message": "Cannot update a running agent."})

    sets: list[str] = ["updated_at = NOW()"]
    vals: list = []

    if body.name is not None:
        vals.append(body.name)
        sets.append(f"name = ${len(vals)}")

    if body.trigger_type is not None:
        if body.trigger_type not in _VALID_TRIGGERS:
            raise HTTPException(status_code=422, detail={"error": "invalid_trigger_type"})
        vals.append(body.trigger_type)
        sets.append(f"trigger_type = ${len(vals)}")

    if body.schedule is not None:
        vals.append(body.schedule)
        sets.append(f"schedule = ${len(vals)}")

    if body.credit_cap is not None:
        if body.credit_cap < 1:
            raise HTTPException(status_code=422, detail={"error": "credit_cap must be >= 1"})
        vals.append(body.credit_cap)
        sets.append(f"credit_cap = ${len(vals)}")

    if body.concurrent_max is not None:
        if body.concurrent_max < 1:
            raise HTTPException(status_code=422, detail={"error": "concurrent_max must be >= 1"})
        vals.append(body.concurrent_max)
        sets.append(f"concurrent_max = ${len(vals)}")

    if body.env is not None:
        vals.append(encrypt_env(body.env) if body.env else None)
        sets.append(f"env_encrypted = ${len(vals)}")

    # Recompute next_run_at whenever trigger_type or schedule changes
    new_trigger = body.trigger_type or agent["trigger_type"]
    new_schedule = body.schedule if body.schedule is not None else (agent.get("schedule") or "")
    if new_trigger == "schedule" and new_schedule:
        next_run = _compute_next_run(new_schedule)
        vals.append(next_run)
        sets.append(f"next_run_at = ${len(vals)}")
    elif body.trigger_type is not None and body.trigger_type != "schedule":
        vals.append(None)
        sets.append("next_run_at = NULL")

    vals.append(agent_id)
    await db.execute(
        f"UPDATE hosted_agents SET {', '.join(sets)} WHERE id = ${len(vals)}::uuid",
        *vals,
    )

    updated = await _get_agent_or_404(db, user_id, agent_id)
    return {
        "id":             str(updated["id"]),
        "name":           updated["name"],
        "slug":           updated["slug"],
        "runtime":        updated["runtime"],
        "status":         updated["status"],
        "trigger_type":   updated["trigger_type"],
        "schedule":       updated["schedule"],
        "credit_cap":     updated["credit_cap"],
        "concurrent_max": updated.get("concurrent_max") or 1,
        "next_run_at":    updated["next_run_at"].isoformat() if updated.get("next_run_at") else None,
        "updated_at":     datetime.now(timezone.utc).isoformat(),
    }


@router.post("/webhooks/{webhook_id}", status_code=202)
@limiter.limit("60/minute")
async def trigger_webhook(
    request: Request,
    webhook_id: str,
    db=Depends(get_db),
) -> dict:
    """Trigger a webhook agent. Auth: the webhook_id URL segment IS the credential."""
    try:
        wh_uuid = uuid.UUID(webhook_id)
    except ValueError:
        raise HTTPException(status_code=404, detail={"error": "not_found"})

    agent_row = await db.fetchrow(
        "SELECT * FROM hosted_agents WHERE webhook_id = $1::uuid", wh_uuid
    )
    if not agent_row:
        raise HTTPException(status_code=404, detail={"error": "not_found"})

    agent = dict(agent_row)
    if agent["trigger_type"] != "webhook":
        raise HTTPException(status_code=409, detail={
            "error": "not_a_webhook_agent",
            "message": "This agent is not configured for webhook triggers.",
        })
    if agent["status"] == "draft":
        raise HTTPException(status_code=409, detail={"error": "no_code",
            "message": "Upload code first: PUT /cloud/agents/{id}/code"})

    user_id = str(agent["user_id"])

    # Concurrency checks (same as manual dispatch)
    concurrent_max = int(agent.get("concurrent_max") or 1)
    active_count = int(await db.fetchval("""
        SELECT COUNT(*) FROM agent_runs
        WHERE hosted_agent_id = $1::uuid AND status IN ('queued', 'running')
    """, agent["id"]) or 0)
    if active_count >= concurrent_max:
        raise HTTPException(status_code=409, detail={
            "error": "concurrent_limit",
            "active_runs": active_count,
            "concurrent_max": concurrent_max,
        })

    # Balance check
    balance_row = await db.fetchrow(
        "SELECT credits_balance, package_tier FROM user_credits WHERE user_id = $1::uuid",
        user_id,
    )
    balance = int((balance_row or {}).get("credits_balance") or 0)
    tier = (balance_row or {}).get("package_tier") or "free"
    if balance < 1:
        raise HTTPException(status_code=402, detail={"error": "insufficient_credits"})

    credit_cap = agent.get("credit_cap") or 0
    if credit_cap > 0 and balance < credit_cap:
        raise HTTPException(status_code=402, detail={
            "error": "insufficient_credits",
            "balance": balance,
            "credit_cap": credit_cap,
        })

    tier_cap = CONCURRENT_RUNS_PER_USER.get(tier, 0)
    if tier_cap > 0:
        user_active = int(await db.fetchval("""
            SELECT COUNT(*) FROM agent_runs
            WHERE user_id = $1::uuid AND status IN ('queued', 'running')
        """, user_id) or 0)
        if user_active >= tier_cap:
            raise HTTPException(status_code=429, detail={"error": "concurrent_run_cap"})

    # Decrypt runner key stored at agent creation time
    runner_key_ct = agent.get("runner_key_encrypted") or ""
    runner_key_ver = int(agent.get("runner_key_version") or 1)
    if not runner_key_ct:
        raise HTTPException(status_code=409, detail={
            "error": "no_runner_key",
            "message": "Agent has no runner key. Re-create or PATCH the agent to refresh it.",
        })
    try:
        user_api_key = decrypt_api_key(runner_key_ct, runner_key_ver)
    except Exception:
        raise HTTPException(status_code=500, detail={"error": "runner_key_decrypt_failed"})

    from main import app
    run_id, credits_reserved = await _dispatch_run_internal(
        app.state.pool, db, agent, user_id, user_api_key, trigger="webhook",
    )

    return {
        "run_id":           run_id,
        "agent_id":         str(agent["id"]),
        "status":           "queued",
        "credits_reserved": credits_reserved,
        "poll_url":         f"/cloud/agents/{agent['id']}/runs/{run_id}",
    }


@router.delete("/agents/{agent_id}", status_code=200)
@limiter.limit("10/minute")
async def delete_agent(
    request: Request,
    agent_id: str,
    db=Depends(get_db),
) -> dict:
    user_id, _, tier = await _resolve_caller(request, db)
    require_tier(tier, "cloud_agents")

    agent = await _get_agent_or_404(db, user_id, agent_id)
    if agent["status"] == "running":
        raise HTTPException(status_code=409, detail={"error": "agent_running",
            "message": "Stop the running agent before deleting."})

    await db.execute(
        "DELETE FROM hosted_agents WHERE id = $1::uuid AND user_id = $2::uuid",
        agent_id, user_id,
    )
    return {"id": agent_id, "deleted": True}
