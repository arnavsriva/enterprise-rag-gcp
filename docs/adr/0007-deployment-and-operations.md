# ADR-0007: Deployment and operations on GCP

- **Status:** Accepted
- **Date:** 2026-10-02

## Context
Phase 4 moves the system from a laptop into the client-style GCP environment designed in
ADR-0002 and ADR-0004:
- private Cloud SQL,
- Vector Search behind Private Service Connect,
- Cloud Run with IAM-only access,

all provisioned by Terraform and destroyable with `make down`.

## Decisions

### One container image for the API and the jobs
- **Base and install:** `python:3.11-slim`, dependencies installed with `uv` from the pinned
  lockfile in their own layer. About 645 MB; a full rebuild takes about 60 s on Cloud Build.
- **Runtime:** a non-root user; only `/tmp` is writable (Cloud Run's filesystem is in-memory).
  uvicorn's access log is off because our middleware writes one structured line per request.
- **Commands:** the API is the default command. The ingest job overrides it in Terraform
  (`python -m ingest.job`); the benchmark job will do the same in Phase 6.

### Cloud Build as a dedicated least-privilege service account
- **`rag-dev-build` can only:**
  - push to this one Artifact Registry repo (a repository-level binding),
  - write logs,
  - read the build source from the project bucket.
- **Why not the default service account:** it is broadly privileged in many projects.
- **Upload is an allow-list:** `.gcloudignore` sends only the files the Dockerfile needs
  (42 files, about 140 KB). `.env`, Terraform state and data can never be uploaded.
- **Build settings:** regional build in `us-central1`; `CLOUD_LOGGING_ONLY` (required with a
  custom build service account).

### Rollout order: job first, then API
- **Why:** the API's connection setup needs the pgvector extension and schema, and on a fresh
  Cloud SQL instance only the ingest job (which runs the migrations) creates them.
- **First deployment:** image → update job → run ingest → deploy API. `make deploy` updates the
  job first.
- **Lesson from the first deploy:** the API hung in startup until Cloud Run's timeout. It now
  fails fast with "pgvector extension/schema missing: run migrations first".

### Ingestion as a Cloud Run Job inside the VPC
- **Startup:** applies migrations (idempotent, advisory-locked), then runs the same pipeline as
  locally, with two sinks: Cloud SQL and Vector Search.
- **Durable outputs, because the job's disk is ephemeral:**
  - raw filings are archived to `gs://<bucket>/raw/` and read back on later runs;
  - the run report is uploaded to `gs://<bucket>/results/ingest/` **before** any local write.
  - The first cloud run crashed writing the report into the read-only app directory, after all
    data had been written. Its numbers were recovered from Cloud Logging, which is why the
    pipeline logs its summary before any file I/O.
- **Idempotency** comes from (content hash, chunker version, embedding model) plus the
  Vector Search index ID per document:
  - Filings already in Cloud SQL but not in the index are synced from the stored vectors, with
    no re-embedding.
  - Shrinking a filing's chunk count removes the orphaned datapoints.
  - Re-running the job: 20/20 skipped, $0, 3.5 s.
- **Retries:** Cloud Run retried the crashed task automatically (`max_retries = 1`). Because of
  idempotency the retry skipped everything, so no double embedding cost.

### Vertex AI Vector Search integration
- **Writes:** `IndexServiceAsyncClient.upsert_datapoints` / `remove_datapoints` in batches of
  250, against the regional API. Writes don't need the VPC; queries do.
- **Datapoint IDs** equal the Postgres chunk IDs.
- **Token restricts** on `ticker`, `item` and `fiscal_year`. The year is a token, not a numeric,
  so several years can be OR-ed in one filter.
- **Queries:** the SDK's `MatchingEngineIndexEndpoint.match()` with
  `private_service_connect_ip_address` set, called through `asyncio.to_thread` (it's blocking
  gRPC).
- **Context:** results are hydrated from Postgres in the index's order, so both backends give the
  generator identical context.
- **Cost control:** the index and its endpoint exist permanently (no idle cost); only the
  *deployed index* bills. It is deployed for verification, evaluation and benchmark runs only.

## Operational findings (measured)
- **Ingestion in Cloud Run vs laptop:** same output (4,340 chunks, 2,907,624 tokens, so
  chunking is deterministic). Wall time 109 s vs 50 s.
  - HTML parsing totalled 547 s of stage time on 2 vCPUs vs 80 s on the laptop: it's CPU-bound
    and the concurrent parse threads contend.
  - Options if this mattered: more vCPUs, a process pool, or lower parse concurrency.
- **Cloud Logging can drop lines.** Per-filing log lines exist for 17 of 20 filings in the first
  cloud run, while the summary line and the database (`/health`: 20 documents, 4,340 chunks)
  confirm all 20. Durable evidence belongs in the GCS report and the database, not in log lines.
- **Generation latency on the shared global endpoint varies by time window.**
  - Two cloud smoke runs, 4 minutes apart, from `results/smoke/*_cloud.json`:
    - first: server p50 2.6 s / p95 26.0 s;
    - second: p50 5.5 s / p95 18.3 s, max 48 s.
  - Embedding and retrieval inside GCP are fast: about 120–400 ms and 10–150 ms respectively.
  - **Mitigation added:** a 15 s per-attempt timeout plus retry (logged). It recovered one stuck
    call in about 3 s, but can't fix endpoint-wide congestion: one request timed out 3 times
    before succeeding at 48 s.
  - **Real options for a client SLA:** Provisioned Throughput for Gemini, or a regional model
    (`GENAI_LOCATION=us-central1`, `gemini-2.5-flash`). The latter is a config change, and its
    latency is worth measuring in Phase 6.

## Vector Search verification (measured)
- **Deploying the index** (`vector_search_deployed = true`, one e2-standard-2 node, PSC) took
  **31 min 14 s**. Undeploying took about 1 min.
  - The provider returns as soon as the undeploy is accepted. Confirm with `make status`, which
    should show no deployed index IDs.
- **A/B through the deployed API** (`results/smoke/20261002T130420Z_backend_ab_cloud.json`):
  - Setup: 5 questions, vector mode, `backend` override per request.
  - **Retrieval:** both backends returned the **same 6 chunks with the same #1 chunk on all 5
    questions**, and the answers carry the same figures.
  - **Latency:** Vector Search retrieval (PSC query plus Postgres hydration) took 11–28 ms after a
    one-time 7.4 s first call, which creates the endpoint client and gRPC channel. This is
    worth warming at startup in production.
  - This is a correctness check (n=5), not a benchmark; Phase 6 measures recall@k and latency
    distributions.
- **The API exposes a per-request `backend` override,** so one deployment can be evaluated on
  both stores. While the index is undeployed, `vertex_vector_search` returns 501 with an
  explanation.

## Consequences
- `make up` → `make image` → (first time: update job, `make ingest-cloud`) → `make deploy` is the
  full path from zero to a working API. `make down` removes everything except the state bucket.
- Durable artefacts of every cloud run land in GCS and are copied into `results/`.
