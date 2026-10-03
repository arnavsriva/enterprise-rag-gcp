# Deployment Runbook

From an empty GCP project to a deployed, evaluated system, and back to nothing. Costs are list
prices checked 2026-10-02 (see [`cost_model.md`](cost_model.md)). **Every `make up` creates or
changes billable resources: read the plan first.**

## 0. Prerequisites

| Tool | Version | Why |
|---|---|---|
| uv | any recent | Creates the Python 3.11 venv (`make setup`) |
| Docker | any recent | Local Postgres + pgvector (`make db-up`) |
| Terraform | ≥ 1.11 | Ephemeral values / write-only args |
| gcloud CLI | any recent | Auth, state bootstrap, Cloud Build, `make status` |

**Project setup** (console, once): create a project, link billing, and create a **budget alert**
with credits *excluded* (untick free-tier and promotional credits), so the alert tracks real usage
while credits last.

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
gcloud services enable aiplatform.googleapis.com   # embeddings/Gemini for local dev before `make up`
```

## 3. Apply (BILLABLE)

```bash
make tf-plan      # review every resource in the plan
make up           # applies the saved plan; asks you to type 'apply'
```

What gets created (about 55 resources, ~10 min; Cloud SQL alone ~7 min), and what bills while idle:
- **Always on after `make up`:**
  - Cloud SQL `db-f1-micro` + 10 GiB SSD: **$0.013/hr, about $9.37/month**.
  - Bucket, Artifact Registry, Secret Manager (cents).
- **Only when `vector_search_deployed = true`:**
  - Deployed index on one `e2-standard-2` node: $0.0938/hr.
  - The PSC forwarding rule: $0.01/hr.
  - About $75.78/month together. Deploying takes ~30 min.
- **Scale-to-zero (no idle cost):**
  - Cloud Run API.
  - Cloud Run ingest job.

To stop the largest idle cost without tearing everything down, set
`vector_search_deployed = false`, then run `make tf-plan` and `make up`.

## 4. Build and deploy images

```bash
make image          # Cloud Build -> Artifact Registry, tagged with the git commit (-dirty if uncommitted)
make deploy         # roll the API service and the ingest job to that image
```

- The build runs as the dedicated `rag-dev-build` service account. It can push only to this
  repository, read the build source from the bucket, and write logs.
- Terraform ignores image and traffic on Cloud Run, so `terraform apply` never rolls back a
  deployed image.
- First apply: Cloud Run starts on Google's placeholder images until `make deploy`.
- **First deployment order matters.** The API needs the pgvector extension and schema, which the
  ingest job's migrations create. On a fresh Cloud SQL instance:
  1. `make image`
  2. `gcloud run jobs update rag-dev-ingest --image $(cat .last-image) --region us-central1`
  3. `make ingest-cloud`
  4. `make deploy`

  Otherwise the API exits at startup with "pgvector extension/schema missing".

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

**On GCP:** run it as the `rag-dev-ingest` Cloud Run Job inside the VPC (it can reach
private-IP Cloud SQL):

```bash
make ingest-cloud   # execute the job and wait; copies its report into results/ingest/
```

- **What the job does:** applies migrations, ingests into Cloud SQL, and upserts to Vector Search.
  - Raw filings are archived to `gs://<bucket>/raw/` and read back from there on later runs.
  - Reports go to `gs://<bucket>/results/ingest/`.
- **First run:** re-embeds the corpus (~$0.44), because Cloud SQL starts empty.
- **Later runs:** skip unchanged filings and sync any that are missing from Vector Search from
  the stored vectors, without re-embedding.

**Query the deployed API** (IAM-only; your identity token is attached):

```bash
make cloud-ask Q="What were Apple's total net sales in fiscal 2025?"
```

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

## 5c. Vector Search: deploy only when needed (BILLABLE while deployed)

The index and its endpoint always exist at no cost. Only the *deployed* index bills:
$0.094/hr for the e2-standard-2 node plus $0.01/hr for the PSC endpoint, about $2.50/day.

```bash
# deploy (20-60 min): in terraform.tfvars set
vector_search_deployed = true
make tf-plan && make up        # adds the deployed index, PSC IP and forwarding rule;
                               # the API switches to RETRIEVAL_BACKEND=vertex_vector_search
make cloud-ask Q="..."         # answers now come from Vector Search (+ Postgres for text)

# undeploy as soon as you're done: set it back to false
make tf-plan && make up        # removes the 3 resources; the API goes back to pgvector
make status                    # the index endpoint should list no deployed indexes
```

Vectors stay in the index while undeployed (upserts work without a deployment), so redeploying
doesn't need re-ingestion.

## 5d. Benchmark the vector stores (needs the index deployed)

```bash
# terraform.tfvars: vector_search_deployed = true;  make tf-plan && make up   (~30 min)
make image && make deploy        # the image includes bench/
make bench                       # Cloud Run Job in the VPC -> results/bench/<ts>.json
                                 # -> results/vector_store_comparison.md (bench/interpretation.md appended)
# then UNDEPLOY: vector_search_deployed = false;  make tf-plan && make up;  make status
```

The job runs as the eval service account. If only pgvector is reachable it benchmarks pgvector
alone and says so.

## 6. Evaluate and release

The golden set is `eval/golden_set/golden_v1.jsonl` (99 items, approved; see `REVIEW.md`). The
baseline is `eval/baseline.json`.

```bash
make eval-retrieval                  # ~free: recall@k of local retrieval (ARGS="--mode hybrid" to compare)
make eval                            # ~$1.75: full eval of the live API from this machine -> results/eval/
make eval-pipeline                   # ~$1.80: same eval on Vertex AI Pipelines as the eval SA
make release                         # ~$2: build -> no-traffic candidate -> pipeline -> promote if the gate passes
```

- **Re-baseline deliberately**, and only from a clean run, after an intentional change (new
  model, new golden set): `make eval ARGS="--update-baseline"`. The runner refuses to baseline a
  run with request errors.
- **When the gate fails:** `results/eval/<ts>_<label>.json` lists each failed check, plus
  per-question judgments (`items[].judgment.reasoning`). The candidate stays reachable at its
  `candidate---…run.app` URL. Live traffic is unchanged.
- **Pipeline runs:** Vertex AI → Pipelines in the console. Step logs are under
  `resource.type="ml_job"` and arrive a minute or two after a step finishes.

## 7. Teardown

```bash
make down         # terraform destroy for everything in envs/dev (asks for confirmation)
make status       # should list no index endpoints, SQL instances, Cloud Run services, PSC rules
```

**Verify that nothing billable remains:**
- [ ] `make status`: every section is empty.
- [ ] `gcloud run jobs list --region us-central1`: no `rag-dev-*` jobs.
- [ ] `gcloud artifacts repositories list --location us-central1`: no `rag-dev`.
- [ ] `gcloud storage ls`: only `<project>-tfstate` (plus `<project>_cloudbuild`, if Cloud Build
      created one).
- [ ] Billing → Reports over the next 1–2 days: daily cost drops to ~$0.

**Kept on purpose:**
- the state bucket (a few KB), so the next `make up` works;
- enabled APIs (free).

**To remove the project entirely:** `gcloud projects delete <PROJECT_ID>`. That's irreversible
after the 30-day recovery window.

**Rebuilding later:**
1. `make tf-plan && make up`
2. `make image`
3. Update the ingest job image.
4. `make ingest-cloud` (re-embeds, ~$0.44).
5. `make deploy`

The Cloud SQL instance name gets a fresh random suffix, so the 7-day name-reuse block doesn't
apply.

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
- **API revision fails to start, "pgvector extension/schema missing":** migrations haven't run on
  this Cloud SQL instance yet. Run the ingest job first (`make ingest-cloud`), then `make deploy`.
- **Cloud ingest exited non-zero but data looks complete:** check the run report in
  `gs://<bucket>/results/ingest/` and `/health` counts. Cloud Run retries a failed task once, and
  ingestion is idempotent, so retries are safe.
- **Pipeline fails at creation with "does not have permission to access Artifact Registry":**
  the Vertex AI custom-code service agent needs `artifactregistry.reader` on the repository
  (Terraform grants it).
- **Pipeline step: "could not obtain an ID token":** Vertex custom jobs' metadata server doesn't
  issue ID tokens. The eval SA needs `roles/iam.serviceAccountOpenIdTokenCreator` on itself
  (Terraform grants it).
- **Eval run has request errors (502/504):** Gemini global-endpoint congestion. Re-run later.
  Errored runs are never used as a baseline.
- **`make deploy` succeeds but the new revision gets 0% traffic:** traffic was pinned to a tag.
  Run `gcloud run services update-traffic rag-dev-api --to-latest`. Since this fix,
  `scripts/release.sh` promotes with `--to-latest`.
- **Job fails with `No module named 'bench'`** (or any new package): add it to the `Dockerfile`,
  `.dockerignore` and `.gcloudignore` allow-lists.
- **Local DB port conflict:** the project uses 5433. Change `PG_PORT` in `.env` if that's taken too.
