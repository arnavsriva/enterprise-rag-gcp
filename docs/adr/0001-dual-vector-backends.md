# ADR-0001: Two switchable vector backends (Vertex AI Vector Search and pgvector)

- **Status:** Accepted
- **Date:** 2026-10-02

## Context
Enterprise clients usually arrive with one of two situations: they already run Postgres and want
the least new infrastructure, or they expect large, growing corpora and want a managed ANN service.
An FDE has to justify the choice with numbers from the client's own data, not vendor claims.

## Options considered
1. **pgvector only:** cheapest, one database for vectors and metadata. ANN scaling is limited by a
   single Postgres instance.
2. **Vertex AI Vector Search only:** managed and horizontally scalable. Bills per node-hour while
   an index is deployed, even when idle, and metadata lives elsewhere.
3. **Both, behind one retrieval interface (chosen):** ingest once into both, select per request
   with `RETRIEVAL_BACKEND`, and benchmark them side by side.

## Decision
Build both. Chunk IDs are identical in both stores (`{accession}:{chunk_index}`). Postgres is the
system of record for chunk text and metadata. Vector Search returns IDs that are hydrated from
Postgres, so the generator sees identical context whichever backend retrieved it.

Index settings are chosen so the rankings are comparable:
- **Embeddings:** `text-embedding-005`, 768-d, L2-normalised.
- **pgvector:** cosine distance with an HNSW index.
- **Vector Search:** `DOT_PRODUCT_DISTANCE` with `UNIT_L2_NORM`, so its ranking equals cosine.
- **Vector Search update method:** `STREAM_UPDATE`, so ingestion upserts directly instead of
  writing batch files.
- **Vector Search scale:** `SHARD_SIZE_SMALL` on one `e2-standard-2` node (the smallest option).

## Consequences
- Ingestion writes twice. A failure between the writes is handled by idempotent upserts and re-runs.
- The deployed index is the largest idle cost in the project, so Terraform gates it behind
  `vector_search_deployed` (default `false`).
- `bench/` produces the evidence for the trade-off in `results/vector_store_comparison.md`.
