# Enterprise RAG Platform on GCP

> Document Q&A over SEC 10-K filings, provisioned end-to-end with Terraform on Google Cloud,
> with automated evaluation gating deployments.

**Status:** 🚧 Phase 6 complete: deployed, evaluated (gated releases), and both vector stores benchmarked. Docs phase next.

## Overview

An enterprise-style Retrieval-Augmented Generation (RAG) system deployed the way a Forward
Deployed Engineer would deploy it inside a client's Google Cloud environment:

- **Corpus:** the latest 10-K for 20 large US companies across sectors, pinned by accession
  number ([`ingest/corpus.toml`](ingest/corpus.toml)): FY2025, plus FY2026 where already filed.
- **Retrieval:** LlamaIndex retrievers over two switchable backends, Vertex AI Vector Search and
  Cloud SQL Postgres + pgvector, with hybrid vector + full-text search and metadata filters
  (company, fiscal year, 10-K Item).
- **Generation:** Gemini 3.8 Flash on Vertex AI, with cited answers and an explicit refusal when
  the filings don't contain the answer. A small LangChain agent (tools: search filings, compare
  two companies) handles multi-step questions.
- **Serving:** FastAPI on Cloud Run, logging latency, tokens and estimated cost per request.
- **Quality gate:** Vertex AI Pipelines (KFP v2) evaluation on a golden question set —
  retrieval recall@k and answer faithfulness (LLM-as-judge) — failing on regression vs baseline.

## Architecture

_Diagram placeholder — added in the foundations phase._

| Layer        | Component                                                        |
|--------------|------------------------------------------------------------------|
| Network      | VPC, private subnet, private service access                      |
| Identity     | Least-privilege service accounts per workload                    |
| Storage      | GCS (raw filings, chunks), Artifact Registry (images)            |
| Vector store | Vertex AI Vector Search index + endpoint; Cloud SQL + pgvector   |
| Compute      | Cloud Run (API), Vertex AI Pipelines (eval)                      |
| Models       | Vertex AI text embeddings, Gemini                                |

Design decisions are recorded in [`docs/adr/`](docs/adr/).

## Quickstart

Local development: no cloud resources, no cost. Needs [uv](https://docs.astral.sh/uv/) and Docker.

```bash
cp .env.example .env          # local defaults work as-is
make setup                    # Python 3.11 venv, locked deps, pre-commit hooks
make db-up                    # Postgres 16 + pgvector on localhost:5433
make migrate                  # apply schema
make lint test test-db
make ingest-dry               # free: download + parse + chunk the 20 pinned 10-Ks
make ingest                   # embed with Vertex AI (~$0.45) into local pgvector
make ask Q="What were Apple's total net sales in fiscal 2025?"
make serve                    # API on http://127.0.0.1:8080 (OpenAPI docs at /docs)
```

```bash
curl -s localhost:8080/query -H 'content-type: application/json' \
  -d '{"question": "How much net interest income did JPMorgan report for 2025?", "tickers": ["JPM"]}'
```

The response includes the answer, numbered sources (company, fiscal year, 10-K Item, SEC URL,
cited flag), token usage, per-stage latency and estimated cost. `POST /agent` takes
`{"question": ...}` for comparisons across companies. `/query` also accepts
`"backend": "pgvector" | "vertex_vector_search"` to compare vector stores per request.

## Deploy

See [`docs/runbook.md`](docs/runbook.md). From an empty project:

```bash
scripts/bootstrap_state.sh <PROJECT_ID>   # one-time: versioned Terraform state bucket
make tf-init && make tf-plan              # review the plan and the cost
make up                                   # [billable] VPC, Cloud SQL, Vector Search, Cloud Run, IAM...
make image                                # Cloud Build -> Artifact Registry
gcloud run jobs update rag-dev-ingest --image $(cat .last-image) --region us-central1
make ingest-cloud                         # migrations + ingestion inside the VPC
make deploy                               # roll the API to the image
make cloud-ask Q="What were Apple's total net sales in fiscal 2025?"
``` **Every apply creates billable resources** — review the plan and
cost estimate first.

## Evaluation

- **Golden set:** [`eval/golden_set/golden_v1.jsonl`](eval/golden_set/golden_v1.jsonl), 99
  questions, human-approved ([`REVIEW.md`](eval/golden_set/REVIEW.md)):
  - 60 numeric and 20 narrative single-company questions,
  - 9 two-company comparisons,
  - 10 unanswerable questions.
- **Relevance labels:** each answerable item carries a verbatim evidence quote from the filing,
  so retrieval is scored by evidence rather than chunk IDs.
- **Metrics:**
  - retrieval: recall@1/3/6 and MRR;
  - answers, graded by an **LLM judge** (Gemini 3.1 Pro, a different model from the generator):
    correctness against the reference, faithfulness to the retrieved sources, hallucination rate,
    refusal accuracy, false refusals and invalid citations;
  - ops: errors, latency and cost.
- **Gate:** [`eval/gate.py`](eval/gate.py) compares each run with
  [`eval/baseline.json`](eval/baseline.json), using noise tolerances plus hard limits
  (hallucination ≤ 10%, refusal accuracy ≥ 80%).
- **Release:** `make release` deploys a no-traffic candidate revision and runs the evaluation as
  a **Vertex AI Pipelines (KFP v2)** job against it. It promotes the candidate only if the gate
  passes. See [ADR-0008](docs/adr/0008-evaluation-and-release-gate.md).

## Results

Every number here is copied from a saved run in [`results/`](results/). Nothing is estimated or
invented.

### Ingestion (local, Phase 2)

From [`results/ingest/20261002T090735Z_ingest.json`](results/ingest/20261002T090735Z_ingest.json)
(`python -m ingest.run`: all 20 filings re-processed after the chunker v2 change.
Chunker `v2-c3000-t1200-o400`, `gemini-embedding-001` @ 768-d, local Docker pgvector, MacBook.)

| Filings | Chunks | Embedding tokens (API-reported) | Wall time | Throughput | Est. embedding cost |
|---|---|---|---|---|---|
| 20 / 20 ingested | 4,340 | 2,907,624 | 49.8 s | 24.1 docs/min · 5,228 chunks/min | $0.44 |

### Query smoke test (local, Phase 3)

From [`results/smoke/20261002T093608Z.json`](results/smoke/20261002T093608Z.json) (`make smoke`:
14 fixed questions run sequentially, from a laptop to Vertex AI, with local pgvector; `gemini-3.8-flash`
global endpoint, thinking LOW, hybrid retrieval, top-k 6):

| Path | Questions | p50 latency | p95 latency | Est. cost / 1K queries | Answered or refused as expected | Invalid citations |
|---|---|---|---|---|---|---|
| `/query` | 12 | 2.7 s | 5.2 s | $4.48 | 12 / 12 | 0 |
| `/agent` | 2 | 9.2 s | 9.4 s | $6.25 | 2 / 2 | 0 |

This is a smoke test (small n, no ground truth), not an evaluation. Retrieval recall and answer
faithfulness come from the Phase 5 golden set.

### Vector store comparison (Phase 6)

Full write-up: [`results/vector_store_comparison.md`](results/vector_store_comparison.md), rendered
from [`results/bench/20261002T194208Z_vector_bench.json`](results/bench/20261002T194208Z_vector_bench.json).
Benchmark run as a Cloud Run Job inside the VPC: 4,340 vectors, 99 questions x 3 rounds, with the
same query vector sent to both stores.

| Backend | Recall@6 (golden) | ANN overlap@10 vs exact | Latency p50 / p99, incl. text | Incremental cost / month |
|---|---|---|---|---|
| Cloud SQL pgvector (HNSW) | 0.854 | 0.986 | 4.3 / 57.0 ms | $0 (Postgres is the system of record; instance $9.37) |
| Vertex AI Vector Search (tree-AH, PSC) | 0.865 | 1.000 | 10.0 / 15.2 ms | $75.78 |

At this scale, quality is equivalent and both are under 1% of request latency, so **pgvector is
the default** and Vector Search is the scale-out path ([ADR-0009](docs/adr/0009-vector-store-choice.md)).

### Golden-set evaluation (Phase 5)

**Baseline:** [`results/eval/20261002T135259Z_api_pgvector_vector_baseline.json`](results/eval/20261002T135259Z_api_pgvector_vector_baseline.json).
99 questions against the deployed API (Cloud Run → Cloud SQL pgvector, vector retrieval,
`gemini-3.8-flash`), judged by `gemini-3.1-pro-preview`, with 0 request errors.

| Retrieval recall@1 / @3 / @6 | MRR | Correctness | Hallucination rate | Faithfulness | Refusal accuracy | False refusals | Cost / 1K queries |
|---|---|---|---|---|---|---|---|
| 0.596 / 0.742 / 0.854 | 0.686 | 0.938 (93.3% fully correct) | 0.0% | 100% | 10 / 10 | 4.5% | $4.33 |

- **By category:** recall@6 is 0.90 for numeric, 0.90 for narrative and **0.44 for comparisons**
  (both companies must be retrieved in one search).
- **All 5 wrong answers trace to retrieval misses.** In each case the system refused (4) or
  faithfully reported what it did retrieve (1). None were hallucinated.
- **Server latency** was p50 4.9 s / p95 37.1 s during this run (Gemini endpoint congestion; see
  ADR-0007).

**Evaluation-gated release** ([`results/eval/20261002T170420Z_release-22331e5.json`](results/eval/20261002T170420Z_release-22331e5.json)):
`make release` deployed a no-traffic candidate and evaluated it on **Vertex AI Pipelines**.
- **Gate passed, and the candidate was promoted to 100% of traffic.**
- **Retrieval** was identical to the baseline (recall@6 0.854, MRR 0.686; it's deterministic).
- **Correctness** was 0.927 vs 0.938, and false refusals 5.6% vs 4.5%: about one question of
  run-to-run variation from generation at temperature 1.0 and the LLM judge, within the gate's
  tolerances.
- **Unchanged:** hallucination 0%, refusal accuracy 10/10, 0 errors.
- **Latency:** p50 1.8 s / p95 7.0 s in this window.
- An earlier release attempt was correctly **blocked** by the gate: 99/99 requests failed with
  401, because ID tokens were minted for the tag URL. Fixed (ADR-0008).

**Retrieval mode** (retrieval only, same golden set, `results/eval/*_retrieval.json`):

| Mode | recall@1 | recall@6 | MRR |
|---|---|---|---|
| vector (default) | 0.596 | 0.843 | 0.683 |
| hybrid, vector weight 0.7 | 0.573 | 0.843 | 0.671 |
| hybrid, vector weight 0.5 | 0.494 | 0.798 | 0.600 |

### On GCP (Phase 4)

**Cloud ingestion.** Cloud Run Job inside the VPC → Cloud SQL + Vector Search. Source:
[`results/ingest/20261002T110300Z_ingest_cloud.json`](results/ingest/20261002T110300Z_ingest_cloud.json),
reconstructed from Cloud Logging (see the provenance note in the file).

| Filings | Chunks | Embedding tokens | Wall time | Throughput | Est. cost |
|---|---|---|---|---|---|
| 20 / 20 | 4,340 | 2,907,624 (identical to the local run) | 109.3 s | 11.0 docs/min | $0.44 |

**Re-run (idempotency).** [`results/ingest/20261002T112206Z_ingest.json`](results/ingest/20261002T112206Z_ingest.json):
20/20 skipped, $0.00, 3.5 s.

**Deployed API smoke tests.** Same 14 questions against Cloud Run (pgvector backend on Cloud SQL),
run 4 minutes apart. Latency is the API's server-side total.

| Run | `/query` p50 | `/query` p95 | Est. cost / 1K | Checks passed | Invalid citations |
|---|---|---|---|---|---|
| [`20261002T112326Z_cloud`](results/smoke/20261002T112326Z_cloud.json) | 2.6 s | 26.0 s | $4.59 | 11 / 12 ¹ | 0 |
| [`20261002T112745Z_cloud`](results/smoke/20261002T112745Z_cloud.json) | 5.5 s | 18.3 s | $4.54 | 11 / 11 | 0 |

- **¹ The one failed check is an ambiguous question.** For a metric ExxonMobil doesn't report
  under that name, the model answered with correctly labelled related measures instead of
  refusing. It's faithful either way, and the question is now marked "either acceptable".
- **Vector Search verified.** The same 5 questions through the deployed API with
  `backend=vertex_vector_search` and with `backend=pgvector` returned identical top-6 chunks and
  the same figures ([`20261002T130420Z_backend_ab_cloud`](results/smoke/20261002T130420Z_backend_ab_cloud.json)).
  The index was undeployed afterwards to stop its $0.104/hr cost.
- **Tail latency is Gemini queueing** on the shared global endpoint: embedding takes about
  0.1–0.4 s and retrieval about 0.01–0.15 s inside GCP. See ADR-0007 for the analysis and the
  options (Provisioned Throughput or a regional model).

## Cost

_See [`docs/cost_model.md`](docs/cost_model.md)._ Project budget target: under ~$120 of GCP credits.
Vector Search endpoints and Cloud SQL bill while idle — tear down between sessions.

## Teardown

```bash
make down
```

Destroys all Terraform-managed resources in the dev environment. See
[`docs/runbook.md`](docs/runbook.md) for verification steps.

## Project Structure

```
.
├── api/                    # FastAPI service (Cloud Run)
├── common/                 # Settings, JSON logging, SQL migrations
├── bench/                  # Vector Search vs pgvector benchmark
├── CONTRIBUTING.md         # Conventions and guardrails
├── docs/
│   ├── adr/                # Architecture decision records
│   ├── cost_model.md       # Cost per 1K queries
│   ├── exec_brief.md       # One-page brief for non-technical stakeholders
│   └── runbook.md          # apply → ingest → eval → teardown
├── eval/
│   ├── golden_set/         # Golden questions + expected sources
│   └── pipelines/          # Vertex AI Pipelines (KFP v2) definitions
├── infra/terraform/
│   ├── envs/dev/           # Dev environment composition
│   └── modules/            # Reusable Terraform modules
├── ingest/                 # Async download → chunk → embed → upsert pipeline
├── rag/                    # Retrieval (LlamaIndex, hybrid) + generation (Gemini) + agent (LangChain)
├── results/                # Saved outputs from real runs only
├── scripts/                # Operational helper scripts
└── tests/                  # Unit tests (no cloud credentials required)
```

## License

[MIT](LICENSE)
