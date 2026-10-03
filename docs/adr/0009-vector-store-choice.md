# ADR-0009: Vector store choice (pgvector by default; Vector Search when scale demands it)

- **Status:** Accepted
- **Date:** 2026-10-02
- **Evidence:** [`results/vector_store_comparison.md`](../../results/vector_store_comparison.md),
  rendered from `results/bench/20261002T194208Z_vector_bench.json`

## Context
ADR-0001 built both backends behind one retrieval interface, so the choice could be made on
measured evidence from this corpus rather than vendor claims. The benchmark (`bench/vector_bench.py`):
- runs as a Cloud Run Job inside the VPC, where the API runs;
- sends the identical query vector to both stores;
- scores golden-set evidence recall and ANN overlap against exact brute-force search;
- measures latency over 3 rounds, unfiltered and company-filtered;
- prices both from list prices.

## Measurements (4,340 x 768-d vectors, 99 questions x 3 rounds)
| | Recall@6 (unfiltered / filtered) | ANN overlap@10 vs exact | Latency p50 / p99, incl. text | Incremental $/month |
|---|---|---|---|---|
| Cloud SQL pgvector (HNSW, db-f1-micro) | 0.854 / 0.865 | 0.986 | 4.3 / 57.0 ms | $0 (Postgres needed anyway; instance $9.37) |
| Vertex AI Vector Search (tree-AH, 1 x e2-standard-2, PSC) | 0.865 / 0.865 | 1.000 | 10.0 / 15.2 ms (ANN only 6.2 / 7.2) | $75.78 |

## Decision
**Default to pgvector** (`RETRIEVAL_BACKEND=pgvector`).
- Quality is equivalent: one golden question of difference, 98.6% vs 100% overlap with exact.
- Median latency is lower: one round trip, versus an ANN call plus a Postgres hydration.
- It adds no incremental cost.
- Retrieval is under 1% of request latency either way.

Keep Vector Search integrated, tested and deployable on demand (`vector_search_deployed`, a
per-request `backend` override). It's the scale-out path when one or more of these hold:
- the vector count outgrows a reasonably sized Cloud SQL instance (tens to hundreds of millions);
- sustained high QPS needs autoscaled, sharded serving with a tight p99;
- there's no Postgres to reuse;
- streaming updates make in-database index maintenance a burden.

## Consequences
- The default deployment costs about $9/month in vector infrastructure, instead of about $85.
- **The decision is scale-dependent and must be re-measured.** The benchmark is a one-command
  rerun (`make bench`, with the index deployed) on the client's real corpus and load.
- **Not measured here:** concurrency and QPS saturation, tuned index parameters, dedicated-core
  Cloud SQL tiers (likely to tighten pgvector's p99). These are the next experiments if a client's
  load profile calls for them.
