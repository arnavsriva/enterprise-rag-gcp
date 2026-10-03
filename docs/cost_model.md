# Cost model

All inputs come from saved runs, extracted by `scripts/cost_inputs.py` into
[`results/cost/cost_inputs.json`](../results/cost/cost_inputs.json), plus list prices checked on
2026-10-02 (us-central1, `common/pricing.py`). These are **estimates**: the authoritative figure is
Cloud Billing. Arithmetic below is shown so every derived number can be checked.

## 1. Variable cost per query

**Direct `/query` (RAG: embed → retrieve 6 chunks → Gemini 3.8 Flash, thinking LOW):**

| Measure | Value | Source |
|---|---|---|
| Mean cost per query | $0.004331 → **$4.33 per 1K queries** | 198 queries, two full golden-set evaluations on the deployed API |
| Median / p95 per query | $0.004436 / $0.005672 | same |
| Mean tokens per query | 5,234 input, 170 output, 0 thinking, 15 embedding | 24 cloud smoke queries |

**Where the money goes** (per query, at the token mix above):
- Generation input (the retrieved context): **$0.003925, 86%**
- Generation output: $0.000639, 14%
- Query embedding: $0.000002, about 0%

**Main lever: context size.** Six chunks of about 750 tokens each are most of the input. Halving
`top_k` would roughly halve the cost, *but must be re-checked against the evaluation gate*: it
lowers retrieval recall (recall@3 0.742 vs recall@6 0.854 on the golden set).

**Price change on 2027-01-01:** Gemini 3.8 Flash's introductory price ($0.75 / $3.75 per 1M
input/output tokens) ends on 2026-12-31. Standard pricing is $1.50 / $7.50, which makes the same
token mix **$9.13 per 1K queries**.

**Agent `/agent`** (tool-calling, two model calls): **$6.06 per 1K queries** (4 cloud smoke
queries; mean 5,122 input and 578 output tokens). It's about 1.4× `/query`, so route
single-company factual questions to `/query`.

**Not included:**
- **Cloud Run compute:** scales to zero; per-request CPU time is small next to model cost but
  wasn't measured separately.
- **Logging and egress:** check Billing for both.

## 2. Fixed monthly cost

| Item | USD / month | Notes |
|---|---|---|
| Cloud SQL db-f1-micro + 10 GiB SSD (pgvector, system of record) | **9.37** | 730 h × $0.0105 + 10 GiB × 730 h × $0.000232877 |
| Vertex AI Vector Search: 1 × e2-standard-2 node + PSC endpoint | 75.78 (optional) | Off by default (ADR-0009); deploy only for scale |
| Cloud Run (API + jobs), Artifact Registry, GCS, Secret Manager | cents | Scale-to-zero / storage-only at this size |

## 3. One-off costs

| Item | USD | Source |
|---|---|---|
| Embed the full corpus (20 10-Ks, 2.91M tokens) | 0.44 | cloud ingest run |
| Draft the golden set (Gemini 3.1 Pro) | 0.96 | golden draft report |
| One full evaluation run (99 answers + LLM judge) | 1.74 | each of two evaluation runs; the judge is ~75% of it |
| Index deploy, per hour while deployed (only if Vector Search is used) | 0.104 | list price |

## 4. Monthly totals by traffic (default stack: pgvector, `/query`)

Monthly ≈ fixed $9.37 + (queries / 1,000) × per-1K cost (30-day month):

| Traffic | Queries / month | Introductory price ($4.33 / 1K) | Standard price from 2027 ($9.13 / 1K) |
|---|---|---|---|
| 100 / day | 3,000 | 9.37 + 13.0 ≈ **$22** | 9.37 + 27.4 ≈ **$37** |
| 1,000 / day | 30,000 | 9.37 + 129.9 ≈ **$139** | 9.37 + 273.9 ≈ **$283** |
| 10,000 / day | 300,000 | 9.37 + 1,299 ≈ **$1,308** | 9.37 + 2,739 ≈ **$2,748** |

**Add per release:** about $1.74 for the evaluation gate (one run per candidate).

**At 10,000 queries a day, consider:**
- a dedicated-core Cloud SQL tier;
- smaller context, if the gate allows it;
- context caching, if prompts repeat;
- Provisioned Throughput for Gemini, as much for a latency SLA as for cost (ADR-0007).

## 5. What this project spent (estimate from saved runs)

| Phase | Main items | ≈ USD |
|---|---|---|
| 2: ingestion (local) | embedding: sample, failed GS run, full run | 0.82 |
| 3: RAG + API | smoke runs, probes, dev queries | 0.30 |
| 4: deploy | cloud ingest 0.44, cloud smoke runs, Vector Search ~1.5 h | 0.80 |
| 5: evaluation | golden draft 0.96, three full evals ~1.74 each, smaller checks | 6.40 |
| 6: benchmark | Vector Search ~0.7 h, bench job, builds | 0.10 |
| Infrastructure idle time | Cloud SQL $0.31/day while up | ~0.3–0.6 |
| **Total** | | **≈ $9–10**, of a $120 project budget |

Confirm against Cloud Billing (Billing → Reports, filtered to project `enterprise-rag-510408`).
The budget alert tracks spend with credits excluded.
