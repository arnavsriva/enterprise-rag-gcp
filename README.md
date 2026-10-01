# Enterprise RAG Platform on GCP

> Document Q&A over SEC 10-K filings, provisioned end-to-end with Terraform on Google Cloud,
> with automated evaluation gating deployments.

**Status:** 🚧 Scaffold only (Phase 0). Sections below are placeholders until each phase lands.

## Overview

An enterprise-style Retrieval-Augmented Generation (RAG) system deployed the way a Forward
Deployed Engineer would deploy it inside a client's Google Cloud environment:

- **Corpus:** public SEC 10-K filings (EDGAR) for ~20 large companies.
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

_Local development (no cloud resources) — filled in during the foundations/ingestion phases._

```bash
cp .env.example .env          # fill in placeholders
make setup                    # create venv, install deps, install pre-commit hooks
make lint test
```

## Deploy

_See [`docs/runbook.md`](docs/runbook.md)._ Outline: `make tf-init` → `make tf-plan` → `make up`
→ `make ingest` → `make eval`. **Every apply creates billable resources** — review the plan and
cost estimate first.

## Evaluation

_Golden set (~100 questions), metrics, and gating logic — documented in the eval phase._

## Results

_No results yet._ Every number in this section will be copied from a saved run in
[`results/`](results/), with the command and timestamp that produced it. No metric here is
estimated or invented.

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
