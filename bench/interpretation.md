## Interpretation (for this run; see the caveats)

**Retrieval quality is effectively identical at this scale.**
- Against exact brute-force search, Vector Search's tree-AH index returned the exact top-10 for
  every query (overlap 1.000). pgvector's HNSW returned 98.6% of them (0.986).
- On the golden-set evidence, recall@6 differs by one question out of 89 (0.865 vs 0.854
  unfiltered), and is identical with a company filter (0.865).
- Both are well within run-to-run noise for answer quality (ADR-0008). pgvector's unfiltered
  recall@6 (0.854) matches the evaluation baseline exactly, as it should.

**Latency favours pgvector for what the RAG service actually needs, but both are negligible.**
- pgvector answers the full query (neighbours plus text and metadata) in one round trip: p50
  4.3 ms.
- Vector Search's ANN call alone is fast and very consistent (p50 6.2 ms, p99 7.2 ms). But the
  service must then fetch text from Postgres, so end to end it's p50 10.0 ms, p99 15.2 ms.
- pgvector's unfiltered p99 (57 ms) is its weak point. A plausible cause is the shared-core
  `db-f1-micro`, but this run doesn't isolate it.
- Either way, retrieval is under 1% of a `/query` request, whose time is dominated by Gemini
  generation (seconds; ADR-0007).

**Cost is the deciding factor.**
- Vector Search adds about $76/month (one e2-standard-2 node plus the PSC endpoint) on top of the
  Postgres instance the system needs anyway as its system of record. That's roughly 8× the
  database's own cost.

**Recommendation for this workload: pgvector** (the current default). Vector Search earns its
cost when one or more of these hold:
- the corpus grows to tens or hundreds of millions of vectors, beyond a comfortably sized Cloud
  SQL instance;
- sustained high QPS needs autoscaled, sharded serving with a tight p99;
- there's no Postgres to reuse;
- streaming updates at a scale where index maintenance in Postgres becomes an operational burden.

The same benchmark, re-run on the client's real corpus size and query load, is how that decision
should be made.

**Caveats**
- Small corpus (4,340 vectors): ANN differences grow with scale, and both indexes ran close to
  exact here.
- Single client, sequential queries. No concurrency or QPS saturation test.
- Default index parameters (HNSW m=16, ef_construction=64, ef_search=100; tree-AH leaf search
  7%), untuned.
- Cloud SQL is shared-core `db-f1-micro`. A dedicated-core tier would likely tighten pgvector's
  tail.
- Prices are list prices checked 2026-10-02, us-central1, with no committed-use discounts.
