"""routers/templates.py — Wayforth Cloud Templates Marketplace.

GET  /templates                      — list all templates (no code)
GET  /templates/{template_id}        — full template detail including readme
POST /templates/{template_id}/deploy — create a hosted agent from a template

Deploy creates an agent with code pre-loaded and status='ready' in one call.
Requires cloud_agents tier gate (starter+). Auth via X-Wayforth-API-Key.
"""
from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.agent_secrets import encrypt_env
from core.auth import _resolve_user, encrypt_api_key
from core.db import get_db
from core.tier_gates import require_tier
from services.templates import get_template, list_templates

router = APIRouter(tags=["Templates"])


class DeployTemplateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    slug: str | None = Field(None, min_length=1, max_length=80,
                              pattern=r"^[a-z0-9][a-z0-9\-]*$")
    runtime: str | None = Field(None, description="python3.12 or node20")
    env_vars: dict[str, str] = Field(default_factory=dict)
    credit_cap: int = Field(default=0, ge=0,
                             description="Max credits per run (0 = no cap)")


@router.get("/templates")
async def list_all_templates():
    """List all available Cloud agent templates.

    Returns metadata for each template (name, description, services, credits,
    runtimes). Code is excluded — use GET /templates/{id} for full detail.
    """
    return {
        "templates": list_templates(),
        "total": len(list_templates()),
    }


@router.get("/templates/{template_id}")
async def get_template_detail(template_id: str):
    """Full template detail including readme and code per runtime."""
    tmpl = get_template(template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail={
            "error": "template_not_found",
            "template_id": template_id,
            "available": [t["id"] for t in list_templates()],
        })
    return tmpl


@router.post("/templates/{template_id}/deploy")
async def deploy_template(
    template_id: str,
    body: DeployTemplateRequest,
    request: Request,
    db=Depends(get_db),
):
    """Create a hosted agent pre-loaded with a template.

    One-call deploy: the resulting agent has code already loaded,
    status='ready'. Dispatch with POST /cloud/agents/{id}/runs.

    Required tier: starter+
    """
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail={"error": "X-Wayforth-API-Key required"})
    user_id, _, tier = await _resolve_user(db, api_key)
    require_tier(tier, "cloud_agents")

    tmpl = get_template(template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail={
            "error": "template_not_found",
            "template_id": template_id,
            "available": [t["id"] for t in list_templates()],
        })

    runtime = body.runtime or tmpl["default_runtime"]
    if runtime not in tmpl["runtimes"]:
        raise HTTPException(status_code=400, detail={
            "error": "unsupported_runtime",
            "requested": runtime,
            "supported": tmpl["runtimes"],
        })

    code = tmpl["code"][runtime]

    slug = body.slug
    if not slug:
        slug = re.sub(r"[^a-z0-9\-]", "-", body.name.lower())
        slug = re.sub(r"-+", "-", slug).strip("-")[:80]
        if not slug:
            slug = template_id

    # Encrypt env vars the same way cloud.py does (Fernet via core.agent_secrets)
    env_encrypted = encrypt_env(body.env_vars) if body.env_vars else None

    # Encrypt caller's API key for future scheduled/webhook dispatch
    runner_key_ct: str | None = None
    runner_key_ver: int = 1
    if api_key:
        try:
            runner_key_ct, runner_key_ver = encrypt_api_key(api_key)
        except Exception:
            pass  # ENCRYPTION_KEY not set; schedule/webhook runs will skip

    # Deduplicate slug within this user's agents
    existing = await db.fetchrow(
        "SELECT id FROM hosted_agents WHERE user_id = $1::uuid AND slug = $2",
        uuid.UUID(str(user_id)), slug,
    )
    if existing:
        slug = f"{slug}-{uuid.uuid4().hex[:6]}"

    row = await db.fetchrow(
        """
        INSERT INTO hosted_agents
            (user_id, name, slug, runtime, trigger_type, status,
             credit_cap, env_encrypted, code, sandbox_provider,
             runner_key_encrypted, runner_key_version)
        VALUES ($1::uuid, $2, $3, $4, 'manual', 'ready',
                $5, $6, $7, 'e2b', $8, $9)
        RETURNING id, name, slug, runtime, trigger_type, status, credit_cap,
                  created_at, webhook_id
        """,
        uuid.UUID(str(user_id)),
        body.name,
        slug,
        runtime,
        body.credit_cap,
        env_encrypted,
        code,
        runner_key_ct,
        runner_key_ver,
    )

    return {
        "agent_id":       str(row["id"]),
        "name":           row["name"],
        "slug":           row["slug"],
        "runtime":        row["runtime"],
        "trigger_type":   row["trigger_type"],
        "status":         row["status"],
        "credit_cap":     row["credit_cap"],
        "template_id":    template_id,
        "template_name":  tmpl["name"],
        "webhook_id":     str(row["webhook_id"]) if row["webhook_id"] else None,
        "created_at":     row["created_at"].isoformat(),
        "dispatch_url":   f"POST /cloud/agents/{row['id']}/runs",
        "code_url":       f"GET /cloud/agents/{row['id']}/code",
        "credits_per_run": tmpl["credits_per_run"],
        "env_vars_configured": list(body.env_vars.keys()),
        "env_var_defaults": [
            {
                "name":     v["name"],
                "default":  v.get("default", ""),
                "required": v.get("required", False),
            }
            for v in tmpl.get("env_vars", [])
            if v["name"] not in body.env_vars
        ],
    }
