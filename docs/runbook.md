# Deployment Runbook

Sections marked _TBD_ are completed in later phases.

## 0. Prerequisites

| Tool | Version | Why |
|---|---|---|
| uv | any recent | Creates the Python 3.11 venv (`make setup`) |
| Docker | any recent | Local Postgres + pgvector (`make db-up`) |
| Terraform | ≥ 1.11 | Ephemeral values / write-only args |
| gcloud CLI | any recent | Auth, state bootstrap, `make status` |

## 1. Local development (no GCP cost)

```bash
cp .env.example .env
make setup        # Python 3.11 venv, locked deps, pre-commit hooks
make db-up        # Postgres 16 + pgvector on localhost:5433
make migrate      # apply common/migrations
make lint test test-db
```

## 2. One-time GCP bootstrap

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project <PROJECT_ID>
scripts/bootstrap_state.sh <PROJECT_ID>          # versioned state bucket + backend.hcl
cp infra/terraform/envs/dev/terraform.tfvars.example infra/terraform/envs/dev/terraform.tfvars
# edit terraform.tfvars
make tf-init
```

## 3. Apply (BILLABLE)

```bash
make tf-plan      # review every resource in the plan
make up           # applies the saved plan; asks you to type 'apply'
```

What gets created, and what bills while idle:
- **Always on after `make up`:**
  - Cloud SQL `db-f1-micro`.
  - Bucket, Artifact Registry, Secret Manager (cents).
- **Only when `vector_search_deployed = true`:**
  - Deployed index on one `e2-standard-2` node, billed per node-hour.
  - The PSC forwarding rule.
- **Scale-to-zero (no idle cost):**
  - Cloud Run API.
  - Cloud Run ingest job.

To stop the largest idle cost without tearing everything down, set
`vector_search_deployed = false`, then run `make tf-plan` and `make up`.

## 4. Build and deploy images
_TBD (Phase 4)_

## 5. Ingest

**Locally** (into Docker pgvector). Needs `GCP_PROJECT_ID`, `SEC_USER_AGENT`, and application
default credentials for the embedding API.

```bash
make corpus                       # the pinned filings (ingest/corpus.toml)
make ingest-dry                   # free: download + parse + chunk, check Item coverage, estimate cost
make ingest ARGS="--tickers AAPL" # small real run first
make ingest                       # full corpus (~$0.45); unchanged filings are skipped
make ingest ARGS="--force"        # re-embed everything
```

- Raw filings are cached in `data/raw/` (git-ignored) and never re-downloaded.
- Every run writes `results/ingest/<UTC timestamp>_<mode>.json`, with per-filing status,
  chunk/token counts and stage timings.
- The exit code is non-zero if any filing failed. The others are still ingested, and re-running
  retries only the missing or changed ones.

**On GCP:** _TBD (Phase 4)_. It runs as the `rag-dev-ingest` Cloud Run Job inside the VPC.

## 5b. Query locally

```bash
make ask Q="What were Apple's total net sales in fiscal 2025?"
make ask Q="Compare R&D spending at Microsoft and Alphabet" ARGS="--agent"
make serve                     # http://127.0.0.1:8080/docs
make smoke                     # ~$0.07: latency/cost/refusal checks -> results/smoke/
```

- Generation uses Gemini on the **global** endpoint (`GENAI_LOCATION=global`), because Gemini 3.x
  isn't served regionally in this project. For data-residency requirements, set
  `GENAI_LOCATION=us-central1` and `GENERATION_MODEL=gemini-2.5-flash`.
- Logs are one JSON line per request (`message: "request served"`), with latency, tokens and
  `estimated_cost_usd`. Question text is never logged.

## 6. Evaluate
`make eval` — _TBD (Phase 5)_

## 7. Teardown

```bash
make down         # terraform destroy for everything in envs/dev
make status       # should list no index endpoints, SQL instances, Cloud Run services, PSC rules
```

These are intentionally kept after `make down`:
- **The state bucket `<PROJECT_ID>-tfstate`:** a few KB, so the next `make up` works.
- **Enabled APIs:** free.

## Troubleshooting

- **`make up` says "No saved plan":** run `make tf-plan` first. The apply step only applies a
  reviewed plan.
- **Cloud SQL instance name already exists:** names are reserved for about a week after deletion.
  The module adds a random suffix, so this shouldn't happen. If it does, run
  `terraform -chdir=infra/terraform/envs/dev apply -replace=module.cloud_sql.random_id.suffix`.
- **Embedding error "longer than the maximum number of tokens (2048)":** a chunk exceeded the
  model limit. The chunker's token cap (ADR-0005) should prevent this. If it happens, lower
  `MAX_TOKENS` in `ingest/chunk.py` and bump `CHUNKER_VERSION`.
- **Slow answers (10 s+) with low token counts:** these are service-side queueing on the
  shared-capacity global endpoint. Look for `"model call retry"` log lines. If there are none,
  it's queueing rather than retries. The production fix is Provisioned Throughput.
- **`/query` returns 501:** `RETRIEVAL_BACKEND=vertex_vector_search` only works inside the VPC
  (Phase 4). Use `pgvector` locally.
- **SEC 403 errors:** `SEC_USER_AGENT` must be `"Name email"`. SEC blocks anonymous or
  generic agents.
- **Local DB port conflict:** the project uses 5433. Change `PG_PORT` in `.env` if that's taken too.
