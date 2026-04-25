"""
Service Graph Builder — computes co-search patterns from search_analytics.
Runs after each crawler cycle. Builds the Wayforth Service Graph.
"""
import asyncio
import asyncpg
import json
import logging
import os

logger = logging.getLogger("wayforth.graph")


async def build_service_graph(pool):
    """
    Compute co-occurrence edges from recent search_analytics.
    Services that appear together in the same session's top results
    get a co_search edge.
    """
    async with pool.acquire() as db:
        rows = await db.fetch(
            """
            SELECT results FROM search_analytics
            WHERE created_at > NOW() - INTERVAL '30 days'
            AND results IS NOT NULL
            AND jsonb_array_length(results) >= 2
            ORDER BY created_at DESC
            LIMIT 1000
            """
        )

        edge_counts: dict[tuple[str, str], int] = {}

        for row in rows:
            try:
                results = json.loads(row["results"]) if isinstance(row["results"], str) else row["results"]
                service_ids = [r.get("id") for r in results if r.get("id")][:5]

                for i in range(len(service_ids)):
                    for j in range(i + 1, len(service_ids)):
                        a, b = sorted([service_ids[i], service_ids[j]])
                        edge_counts[(a, b)] = edge_counts.get((a, b), 0) + 1
            except Exception:
                continue

        for (a, b), count in edge_counts.items():
            await db.execute(
                """
                INSERT INTO service_graph (service_a_id, service_b_id, co_search_count, last_updated)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (service_a_id, service_b_id)
                DO UPDATE SET co_search_count = service_graph.co_search_count + $3,
                              last_updated = NOW()
                """,
                a, b, count,
            )

        logger.info(f"Service graph: updated {len(edge_counts)} edges")


if __name__ == "__main__":
    async def main():
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=3)
        await build_service_graph(pool)
        await pool.close()

    asyncio.run(main())
