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
`make ingest` — _TBD (Phase 2 locally, Phase 4 on GCP)_

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
- **Local DB port conflict:** the project uses 5433. Change `PG_PORT` in `.env` if that's taken too.
