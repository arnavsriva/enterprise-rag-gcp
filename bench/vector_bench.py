"""Vector store benchmark: Vertex AI Vector Search vs Cloud SQL pgvector.

    python -m bench.vector_bench [--rounds 3] [--limit N]

Runs inside the VPC (Cloud Run Job `rag-dev-bench`) so latencies are measured where the API
runs and the Vector Search PSC endpoint is reachable. Every question in the golden set is
embedded ONCE and the same vector is sent to both stores, so only the store differs.

Measures, per backend and scenario (unfiltered / company-filtered):
- evidence recall@6 and @10 (golden-set evidence quotes, as in eval/metrics.py);
- ANN overlap@10: |ANN top-10 ∩ exact top-10| / 10, where "exact" is a brute-force scan in
  Postgres with the HNSW index disabled. This isolates index approximation error from
  embedding quality (unfiltered only);
- latency p50/p95/p99 over `rounds` passes (backend order alternates per round). Vector Search
  is reported for the ANN call alone and including hydration of text from Postgres (what the
  RAG service actually needs);
- estimated monthly cost from list prices (common.pricing).

Writes results/bench/<ts>.json (uploaded to GCS when INGEST_USE_GCS / BENCH_UPLOAD is set) and
bench.report renders results/vector_store_comparison.md from it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from common import gcs
from common import pricing as p
from common.config import get_settings
from common.logging import configure_logging
from eval.golden import GOLDEN_DIR, GoldenItem, load
from eval.metrics import coverage_rank
from ingest.embed import VertexEmbedder
from rag.retrieval import PgSearch, create_retrieval_pool
from rag.service import QueryEncoder
from rag.types import Filters, RetrievedChunk
from rag.vector_search import VertexMatcher, config_from_settings

K = 10
RESULTS_DIR = Path("results/bench")


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q / 100 * (len(ordered) - 1)))]


def latency_summary(ms: list[float]) -> dict[str, float | int]:
    return {
        "n": len(ms),
        "p50_ms": round(statistics.median(ms), 2),
        "p95_ms": round(pct(ms, 95), 2),
        "p99_ms": round(pct(ms, 99), 2),
        "mean_ms": round(statistics.mean(ms), 2),
    }


def as_sources(chunks: list[RetrievedChunk]) -> list[dict[str, Any]]:
    return [{"ticker": c.ticker, "text": c.content} for c in chunks]


def filters_for(item: GoldenItem, scenario: str) -> Filters:
    if scenario == "ticker_filtered" and item.tickers:
        return Filters(tickers=tuple(item.tickers))
    return Filters()


async def exact_top_k(pool: Any, vector: list[float], k: int) -> list[str]:
    """Brute-force nearest neighbours: index scans disabled for this transaction only."""
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SET LOCAL enable_indexscan = off")
        rows = await conn.fetch(
            "SELECT id FROM chunks ORDER BY embedding <=> $1 LIMIT $2", vector, k
        )
    return [r["id"] for r in rows]


def monthly_costs(index_gib: float) -> dict[str, Any]:
    h = p.HOURS_PER_MONTH
    cloud_sql = p.CLOUD_SQL_DB_F1_MICRO_PER_HOUR * h + p.CLOUD_SQL_SSD_PER_GIB_HOUR * 10 * h
    vs_serving = p.VECTOR_SEARCH_E2_STANDARD_2_PER_NODE_HOUR * h
    psc = p.PSC_ENDPOINT_PER_HOUR * h
    return {
        "hours_per_month": h,
        "prices_checked": p.PRICES_CHECKED,
        "pgvector_cloud_sql_db_f1_micro_10gib_ssd": round(cloud_sql, 2),
        "vector_search_1x_e2_standard_2": round(vs_serving, 2),
        "vector_search_psc_endpoint": round(psc, 2),
        "vector_search_total_serving": round(vs_serving + psc, 2),
        "vector_search_one_off_build_usd": round(index_gib * p.VECTOR_SEARCH_BUILD_PER_GIB, 4),
        "vector_search_one_off_stream_insert_usd": round(
            index_gib * p.VECTOR_SEARCH_STREAM_INSERT_PER_GIB, 4
        ),
        "index_size_gib": round(index_gib, 4),
        "note": (
            "Postgres is required either way as the system of record (text, metadata, keyword "
            "search), so Vector Search is an incremental cost on top of Cloud SQL."
        ),
    }


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    s = get_settings()
    configure_logging("WARNING")
    items = load(GOLDEN_DIR / "golden_v1.jsonl")
    items = items[: args.limit] if args.limit else items
    started = datetime.now(UTC)

    pool = await create_retrieval_pool(s.pg_dsn())
    search = PgSearch(pool)
    encoder = QueryEncoder(
        VertexEmbedder(
            project=s.gcp_project_id or "",
            location=s.gcp_region,
            model=s.embedding_model,
            dim=s.embedding_dim,
            task_type="RETRIEVAL_QUERY",
        )
    )
    vs = config_from_settings(s)
    matcher = VertexMatcher(vs) if vs and vs.queryable else None
    n_vectors = await pool.fetchval("SELECT count(*) FROM chunks")

    # 1) one embedding per question, shared by both stores
    vectors: dict[str, list[float]] = {}
    for item in items:
        vectors[item.id] = (await encoder.encode(item.question))[0]

    # 2) exact ground truth for ANN overlap
    exact = {item.id: await exact_top_k(pool, vectors[item.id], K) for item in items}

    async def pg(vec: list[float], f: Filters) -> tuple[list[RetrievedChunk], float, float]:
        t = time.perf_counter()
        hits = await search.vector(vec, f, K)
        ms = (time.perf_counter() - t) * 1000
        return hits, ms, ms

    async def vertex(vec: list[float], f: Filters) -> tuple[list[RetrievedChunk], float, float]:
        assert matcher is not None  # noqa: S101 - only registered when the matcher exists
        t = time.perf_counter()
        ids = await matcher.search(vec, f, K)
        ann_ms = (time.perf_counter() - t) * 1000
        hits = await search.by_ids(ids)
        return hits, ann_ms, (time.perf_counter() - t) * 1000

    backends: dict[
        str, Callable[[list[float], Filters], Awaitable[tuple[list[RetrievedChunk], float, float]]]
    ] = {"pgvector": pg}
    if matcher is not None:
        backends["vertex_vector_search"] = vertex
    else:
        print("Vector Search not deployed/reachable: benchmarking pgvector only", file=sys.stderr)

    # 3) warm-up (connection pools, the gRPC channel to the PSC endpoint)
    for fn in backends.values():
        for item in items[:5]:
            await fn(vectors[item.id], Filters())

    scenarios = ["unfiltered", "ticker_filtered"]
    lat: dict[str, dict[str, dict[str, list[float]]]] = {
        b: {sc: {"ann": [], "total": []} for sc in scenarios} for b in backends
    }
    first_round: dict[str, dict[str, dict[str, list[RetrievedChunk]]]] = {
        b: {sc: {} for sc in scenarios} for b in backends
    }
    order = list(backends)
    for rnd in range(args.rounds):
        for sc in scenarios:
            for item in items:
                for b in order if rnd % 2 == 0 else list(reversed(order)):
                    hits, ann_ms, total_ms = await backends[b](
                        vectors[item.id], filters_for(item, sc)
                    )
                    lat[b][sc]["ann"].append(ann_ms)
                    lat[b][sc]["total"].append(total_ms)
                    if rnd == 0:
                        first_round[b][sc][item.id] = hits

    answerable = [i for i in items if i.answerable]
    results: dict[str, Any] = {}
    for b in backends:
        results[b] = {}
        for sc in scenarios:
            hits_by_item = first_round[b][sc]
            ranks = {i.id: coverage_rank(i, as_sources(hits_by_item[i.id])) for i in answerable}
            entry: dict[str, Any] = {
                "evidence_recall@6": round(
                    sum(1 for r in ranks.values() if r and r <= 6) / len(answerable), 4
                ),
                "evidence_recall@10": round(
                    sum(1 for r in ranks.values() if r and r <= 10) / len(answerable), 4
                ),
                "latency_total": latency_summary(lat[b][sc]["total"]),
                "latency_ann_only": latency_summary(lat[b][sc]["ann"]),
                "returned_k_mean": round(statistics.mean(len(h) for h in hits_by_item.values()), 2),
            }
            if sc == "unfiltered":
                overlaps = [
                    len({h.chunk_id for h in hits_by_item[i.id]} & set(exact[i.id])) / K
                    for i in items
                ]
                entry["ann_overlap@10_vs_exact"] = round(statistics.mean(overlaps), 4)
            results[b][sc] = entry

    index_gib = n_vectors * s.embedding_dim * 4 / 1024**3
    report = {
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "environment": os.environ.get("BENCH_ENVIRONMENT", "local"),
        "config": {
            "questions": len(items),
            "answerable": len(answerable),
            "rounds": args.rounds,
            "k": K,
            "vectors": n_vectors,
            "dim": s.embedding_dim,
            "embedding_model": s.embedding_model,
            "pgvector": "HNSW (m=16, ef_construction=64 defaults), cosine, ef_search=100, iterative_scan=relaxed_order",
            "vertex_vector_search": "tree-AH, DOT_PRODUCT on unit vectors, SHARD_SIZE_SMALL, 1x e2-standard-2, PSC",
            "cloud_sql": "db-f1-micro (shared core), 10 GiB SSD, private IP",
            "ground_truth": "exact brute-force cosine in Postgres (index scans disabled)",
        },
        "results": results,
        "monthly_cost_usd": monthly_costs(index_gib),
    }
    name = f"{started.strftime('%Y%m%dT%H%M%SZ')}_vector_bench.json"
    body = json.dumps(report, indent=2)
    if s.ingest_use_gcs and s.gcs_bucket:  # Cloud Run Job: local disk is ephemeral
        location = await gcs.upload(
            s.gcs_bucket, f"results/bench/{name}", body.encode(), "application/json"
        )
    else:
        location = str(RESULTS_DIR / name)
        await asyncio.to_thread(_write, RESULTS_DIR / name, body)
    print(f"report: {location}", file=sys.stderr)
    print(json.dumps(results, indent=2), file=sys.stderr)
    await pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
