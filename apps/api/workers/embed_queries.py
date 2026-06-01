"""workers/embed_queries.py — Background worker: embed task_query_text via Jina Embeddings API.

Runs hourly. Reads credit_transactions rows that have task_query_text set but
no corresponding task_embeddings row, calls Jina's embeddings endpoint, and
writes results to task_embeddings.

Zero latency impact on the hot path — never called inline with executions.
"""
import asyncio
import json
import logging
import os

import httpx

logger = logging.getLogger("wayforth")

_JINA_EMBED_URL = "https://api.jina.ai/v1/embeddings"
_JINA_MODEL = "jina-embeddings-v2-base-en"
_BATCH_SIZE = 50
_INTERVAL_SECONDS = 3600  # hourly


async def _run_embed_batch(pool) -> int:
    """Embed one batch of unembedded queries. Returns the number of rows embedded."""
    jina_key = os.environ.get("JINA_API_KEY", "")
    if not jina_key:
        logger.debug("embed_queries: JINA_API_KEY not set, skipping batch")
        return 0

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT ct.id, ct.task_query_text
              FROM credit_transactions ct
             WHERE ct.task_query_text IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM task_embeddings te
                    WHERE te.transaction_id = ct.id
               )
             ORDER BY ct.created_at DESC
             LIMIT $1
        """, _BATCH_SIZE)

    if not rows:
        return 0

    texts = [r["task_query_text"] for r in rows]
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                _JINA_EMBED_URL,
                headers={
                    "Authorization": f"Bearer {jina_key}",
                    "Content-Type": "application/json",
                },
                json={"model": _JINA_MODEL, "input": texts},
            )
        if resp.status_code != 200:
            logger.warning("embed_queries: Jina returned %s — skipping batch", resp.status_code)
            return 0
        data = resp.json()
        embeddings = [item["embedding"] for item in data["data"]]
    except Exception as _e:
        logger.warning("embed_queries: Jina call failed: %s", _e)
        return 0

    inserted = 0
    async with pool.acquire() as conn:
        for row, embedding in zip(rows, embeddings):
            try:
                await conn.execute("""
                    INSERT INTO task_embeddings (transaction_id, embedding, model)
                    VALUES ($1::uuid, $2::real[], $3)
                    ON CONFLICT DO NOTHING
                """, str(row["id"]), embedding, _JINA_MODEL)
                inserted += 1
            except Exception as _e:
                logger.warning("embed_queries: insert failed tx=%s: %s", row["id"], _e)

    return inserted


async def embed_queries_loop() -> None:
    """Hourly loop: embed unembedded task_query_text rows."""
    from main import app
    await asyncio.sleep(300)  # 5-minute startup delay — let pool initialise
    while True:
        pool = getattr(app.state, "pool", None)
        if pool:
            try:
                n = await _run_embed_batch(pool)
                if n:
                    logger.info("embed_queries: embedded %d queries", n)
            except Exception as _e:
                logger.error("embed_queries loop error: %s", _e)
        await asyncio.sleep(_INTERVAL_SECONDS)
