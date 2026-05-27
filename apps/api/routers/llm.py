"""routers/llm.py — OpenAI-compatible LLM gateway.

POST /v1/chat/completions

Routes groq/*, together/*, mistral/*, gemini/*, perplexity/* prefixed
model names to the matching managed adapter. No-prefix requests
auto-select via Groq → Together → Mistral failover chain (skipping any
provider whose env-var key is unset). stream:true returns SSE in
OpenAI delta format.

Auth: X-Wayforth-API-Key header. Builder+ tier required.
Credits: deducted per SERVICE_CONFIGS cost for the provider used.
"""

from __future__ import annotations

import json as _json
import logging
import os
import time as _time
import uuid as _uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from core.auth import _resolve_user
from core.credits import _increment_calls, check_and_deduct_credits
from core.db import get_db
from core.rate_limit import limiter
from core.tier_gates import check_rate_limit, require_tier
from services.managed import (
    SERVICE_CONFIGS,
    call_gemini,
    call_groq,
    call_mistral,
    call_perplexity,
    call_together,
    stream_groq,
    stream_together,
)

logger = logging.getLogger("wayforth")

router = APIRouter()

# ── Model prefix → provider routing ──────────────────────────────────────────

PREFIX_MAP: dict[str, str] = {
    "groq/":        "groq",
    "together/":    "together",
    "mistral/":     "mistral",
    "gemini/":      "gemini",
    "perplexity/":  "perplexity",
}

# Failover chain for auto-select (no prefix) AND when a provider fails.
# Perplexity / Gemini / Mistral don't participate in auto-select failover
# by design (different quality/cost profiles).
FAILOVER_CHAIN: list[str] = ["groq", "together", "mistral"]

# Streaming is only supported for Groq and Together.
_STREAMING_PROVIDERS = {"groq", "together"}


# ── Request model ─────────────────────────────────────────────────────────────

class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[dict]
    temperature: float | None = 0.7
    max_tokens: int | None = 1000
    stream: bool = False
    # Accept and ignore all extra OpenAI-compatible fields (e.g. top_p, n, etc.)
    model_config = ConfigDict(extra="allow")


# ── Adapter helpers ───────────────────────────────────────────────────────────

_CALL_FNS = {
    "groq":        call_groq,
    "together":    call_together,
    "mistral":     call_mistral,
    "gemini":      call_gemini,
    "perplexity":  call_perplexity,
}

_STREAM_FNS = {
    "groq":    stream_groq,
    "together": stream_together,
}


def _provider_key(provider: str) -> str | None:
    """Return the configured env-var value for a provider, or None if unset."""
    cfg = SERVICE_CONFIGS.get(provider)
    if not cfg:
        return None
    return os.environ.get(cfg["key_var"]) or None


def _resolve_provider(model: str) -> tuple[str, str, bool]:
    """Resolve (provider, bare_model_name, is_prefixed) from the request model string.

    Returns:
        provider      — e.g. "groq"
        bare_model    — model name stripped of prefix, passed to the adapter
        is_prefixed   — True when an explicit prefix was present

    Raises ValueError with an error dict for unknown prefixes.
    """
    for prefix, provider in PREFIX_MAP.items():
        if model.startswith(prefix):
            bare = model[len(prefix):]
            return provider, bare, True
    # No prefix → auto-select
    return "", model, False


def _openai_response(
    completion_id: str,
    provider: str,
    model_used: str,
    content: str,
    usage: dict,
    fallback: bool,
) -> dict:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(_time.time()),
        "model": model_used,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
        "x-wayforth-provider": provider,
        "x-wayforth-fallback": fallback,
    }


async def _call_provider(provider: str, bare_model: str, body: ChatCompletionRequest) -> dict:
    """Call a single provider's non-streaming adapter. Returns the raw adapter dict."""
    api_key = _provider_key(provider)
    if not api_key:
        raise RuntimeError(f"No API key configured for provider {provider!r}")
    params = {
        "model": bare_model,
        "messages": body.messages,
        "max_tokens": body.max_tokens if body.max_tokens is not None else 1000,
        "temperature": body.temperature if body.temperature is not None else 0.7,
    }
    fn = _CALL_FNS[provider]
    return await fn(params, api_key)


# ── SSE streaming generator ───────────────────────────────────────────────────

async def _sse_stream(
    provider: str,
    bare_model: str,
    body: ChatCompletionRequest,
    completion_id: str,
    pool,
    user_id: str,
    api_key_id: str,
    credit_cost: int,
):
    """Async generator producing OpenAI-delta SSE events."""
    api_key = _provider_key(provider)
    if not api_key:
        # Shouldn't happen (checked before entering), but guard anyway.
        yield f"data: {_json.dumps({'error': 'provider_key_missing', 'done': True})}\n\n"
        return

    params = {
        "model": bare_model,
        "messages": body.messages,
        "max_tokens": body.max_tokens if body.max_tokens is not None else 1000,
    }
    stream_fn = _STREAM_FNS[provider]
    succeeded = False
    try:
        async for token in stream_fn(params, api_key):
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "choices": [{"delta": {"content": token}, "index": 0}],
            }
            yield f"data: {_json.dumps(chunk)}\n\n"
        succeeded = True
        yield "data: [DONE]\n\n"
    except Exception as exc:
        logger.warning("llm_gateway stream error provider=%s err=%s", provider, exc)
        yield f"data: {_json.dumps({'error': str(exc)[:200]})}\n\n"
        yield "data: [DONE]\n\n"
    finally:
        if succeeded:
            # Deduct credits and increment call counter after successful stream.
            try:
                async with pool.acquire() as db:
                    await check_and_deduct_credits(
                        db, user_id, credit_cost,
                        endpoint="/v1/chat/completions",
                        service_id=provider,
                        tx_type="execution",
                        api_key_id=api_key_id,
                    )
                await _increment_calls(pool, api_key_id)
            except Exception as billing_exc:
                logger.warning("llm_gateway stream billing error: %s", billing_exc)
        else:
            # Stream failed — no credits charged, nothing to undo.
            pass


# ── Main endpoint ─────────────────────────────────────────────────────────────

@router.post("/v1/chat/completions", tags=["LLM Gateway"])
@limiter.limit("120/minute")
async def chat_completions(
    request: Request,
    body: ChatCompletionRequest,
    db=Depends(get_db),
):
    """OpenAI-compatible chat completions endpoint.

    Supports groq/*, together/*, mistral/*, gemini/*, perplexity/* model
    prefixes. No prefix = auto-select via Groq→Together→Mistral failover.
    stream:true returns SSE. Requires Builder+ tier.
    """
    raw_key = request.headers.get("X-Wayforth-API-Key", "")
    if not raw_key:
        return JSONResponse(status_code=401, content={"error": "missing_api_key"})

    user_id, api_key_id, tier = await _resolve_user(db, raw_key)
    require_tier(tier, "byok")          # Builder+ required
    await check_rate_limit(str(api_key_id), tier)

    completion_id = f"wf-{_uuid.uuid4()}"

    # ── Resolve provider ──────────────────────────────────────────────────────
    try:
        provider, bare_model, is_prefixed = _resolve_provider(body.model)
    except ValueError as e:
        return JSONResponse(status_code=400, content=dict(e.args[0]))

    # Validate explicit prefix
    if is_prefixed and provider not in _CALL_FNS:
        return JSONResponse(status_code=400, content={
            "error": "unknown_model_prefix",
            "model": body.model,
        })

    # Unknown prefix (not in PREFIX_MAP but contains a slash that looks prefixed)
    if not is_prefixed and "/" in body.model:
        # User supplied a "something/" prefix we don't recognise
        prefix_part = body.model.split("/")[0] + "/"
        if not any(body.model.startswith(p) for p in PREFIX_MAP):
            return JSONResponse(status_code=400, content={
                "error": "unknown_model_prefix",
                "model": body.model,
            })

    # ── Build the list of providers to attempt ────────────────────────────────
    if is_prefixed:
        # Try the requested provider, then fall through failover chain
        # (but only if the explicit provider itself isn't the only one)
        providers_to_try = [provider] + [
            p for p in FAILOVER_CHAIN if p != provider and p in _CALL_FNS
        ]
        # For gemini / perplexity, don't auto-fallover into the main chain
        if provider not in FAILOVER_CHAIN:
            providers_to_try = [provider]
        # Use the bare model for the first attempt; fall back to provider default
        bare_models_to_try = [bare_model] + [
            bare_model for _ in range(len(providers_to_try) - 1)
        ]
    else:
        # Auto-select: try only providers in FAILOVER_CHAIN with a key set
        providers_to_try = [
            p for p in FAILOVER_CHAIN if _provider_key(p) is not None
        ]
        bare_models_to_try = [body.model] * len(providers_to_try)
        if not providers_to_try:
            return JSONResponse(status_code=503, content={
                "error": "all_providers_failed",
                "attempted": list(FAILOVER_CHAIN),
                "message": "No inference provider keys are configured.",
            })

    # ── Streaming path ────────────────────────────────────────────────────────
    if body.stream:
        # Find the first provider in the list that supports streaming and has a key.
        stream_provider = None
        stream_bare = None
        for p, bm in zip(providers_to_try, bare_models_to_try):
            if p in _STREAMING_PROVIDERS and _provider_key(p):
                stream_provider = p
                stream_bare = bm
                break

        if stream_provider is None:
            # Fall back to non-streaming path if no streaming provider available
            pass
        else:
            credit_cost = SERVICE_CONFIGS[stream_provider]["credits"]
            # Pre-check credits (non-deducting) before opening the stream.
            credits_row = await db.fetchrow(
                "SELECT credits_balance FROM user_credits WHERE user_id = $1::uuid",
                user_id,
            )
            balance = credits_row["credits_balance"] if credits_row else 0
            if balance < credit_cost:
                return JSONResponse(status_code=402, content={
                    "error": "insufficient_credits",
                    "credits_remaining": balance,
                    "credits_required": credit_cost,
                    "upgrade_url": "https://wayforth.io/pricing",
                })

            pool = request.app.state.pool
            gen = _sse_stream(
                stream_provider, stream_bare, body,
                completion_id, pool, str(user_id), str(api_key_id), credit_cost,
            )
            return StreamingResponse(
                gen,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

    # ── Non-streaming path ────────────────────────────────────────────────────
    attempted: list[str] = []
    last_error: str = ""
    result = None
    used_provider: str = ""
    used_bare_model: str = ""
    fallback_used = False

    for idx, (p, bm) in enumerate(zip(providers_to_try, bare_models_to_try)):
        if not _provider_key(p):
            continue
        attempted.append(p)
        try:
            result = await _call_provider(p, bm, body)
            used_provider = p
            used_bare_model = bm
            fallback_used = idx > 0
            break
        except Exception as exc:
            last_error = str(exc)[:300]
            logger.warning(
                "llm_gateway non-streaming failed provider=%s model=%s err=%s",
                p, bm, last_error,
            )
            continue

    if result is None:
        return JSONResponse(status_code=503, content={
            "error": "all_providers_failed",
            "attempted": attempted or [providers_to_try[0]] if providers_to_try else [],
            "last_error": last_error,
        })

    # Deduct credits and increment call counter.
    credit_cost = SERVICE_CONFIGS[used_provider]["credits"]
    success, balance_after = await check_and_deduct_credits(
        db, str(user_id), credit_cost,
        endpoint="/v1/chat/completions",
        service_id=used_provider,
        tx_type="execution",
        api_key_id=str(api_key_id),
    )
    if not success:
        return JSONResponse(status_code=402, content={
            "error": "insufficient_credits",
            "credits_remaining": balance_after,
            "credits_required": credit_cost,
            "upgrade_url": "https://wayforth.io/pricing",
        })

    pool = request.app.state.pool
    await _increment_calls(pool, str(api_key_id))

    # Build usage dict — Groq, Together, Mistral return tokens_used; Gemini too.
    tokens = result.get("tokens_used", 0) or 0
    usage = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": tokens,
    }

    # The model name returned by the adapter (provider may normalise it).
    model_returned = result.get("model", used_bare_model)

    return JSONResponse(content=_openai_response(
        completion_id=completion_id,
        provider=used_provider,
        model_used=model_returned,
        content=result.get("content", ""),
        usage=usage,
        fallback=fallback_used,
    ))
