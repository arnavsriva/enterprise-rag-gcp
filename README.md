# Enterprise RAG Platform on GCP

> Document Q&A over SEC 10-K filings, provisioned end-to-end with Terraform on Google Cloud,
> with automated evaluation gating deployments.

**Status:** 🚧 Phase 2 (ingestion) complete locally. Terraform written and validated, not yet applied.

## Overview

An enterprise-style Retrieval-Augmented Generation (RAG) system deployed the way a Forward
Deployed Engineer would deploy it inside a client's Google Cloud environment:

- **Corpus:** the latest 10-K for 20 large US companies across sectors, pinned by accession
  number ([`ingest/corpus.toml`](ingest/corpus.toml)): FY2025, plus FY2026 where already filed.
- **Retrieval:** LlamaIndex over two switchable vector backends — Vertex AI Vector Search and
  Cloud SQL Postgres + pgvector.
- **Generation:** Gemini on Vertex AI, with a small LangChain agent layer
  (tools: search filings, compare two companies).
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
```

## Deploy

_See [`docs/runbook.md`](docs/runbook.md)._ Outline: `scripts/bootstrap_state.sh` → `make tf-init` →
`make tf-plan` → `make up` → `make ingest` → `make eval`. **Every apply creates billable resources** — review the plan and
cost estimate first.

## Evaluation

_Golden set (~100 questions), metrics, and gating logic — documented in the eval phase._

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

_Retrieval and answer quality results will be added from the Phase 5 evaluation runs._

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
├── rag/                    # Retrieval (LlamaIndex) + agent (LangChain) + Gemini
├── results/                # Saved outputs from real runs only
├── scripts/                # Operational helper scripts
└── tests/                  # Unit tests (no cloud credentials required)
```

## License

[MIT](LICENSE)
