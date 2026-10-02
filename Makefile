# Enterprise RAG Platform on GCP
# Targets marked [BILLABLE] create or modify paid GCP resources — review the plan + cost first.

SHELL      := /bin/bash
VENV       := .venv
BIN        := $(VENV)/bin
TF_DIR     := infra/terraform/envs/dev
TF_VARS    := terraform.tfvars
TF_PLAN    := tfplan
PY_SRC     := common ingest rag api eval tests

# Read only what `make status` needs from .env (not the whole file: quoting differs from make's).
env_var = $(shell test -f .env && sed -n 's/^$(1)=//p' .env | tr -d '"')
GCP_PROJECT_ID ?= $(call env_var,GCP_PROJECT_ID)
GCP_REGION     ?= $(or $(call env_var,GCP_REGION),us-central1)

.DEFAULT_GOAL := help
.PHONY: help setup lock lint fmt test test-db db-up db-down migrate \
        tf-init tf-validate tf-plan up down status corpus ingest-dry ingest \
        serve ask smoke smoke-cloud image deploy ingest-cloud cloud-ask eval bench

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- local dev

setup: ## Create Python 3.11 venv (via uv), install locked deps, install pre-commit hooks
	uv venv --python 3.11 --allow-existing $(VENV)
	uv pip install --python $(BIN)/python -r requirements-dev.txt -e .
	$(BIN)/pre-commit install

lock: ## Re-resolve requirements*.in into pinned requirements*.txt
	uv pip compile requirements.in --universal --python-version 3.11 -o requirements.txt
	uv pip compile requirements-dev.in --universal --python-version 3.11 -o requirements-dev.txt

lint: ## Ruff lint + format check + mypy
	$(BIN)/ruff check $(PY_SRC)
	$(BIN)/ruff format --check $(PY_SRC)
	$(BIN)/mypy $(PY_SRC)

fmt: ## Auto-format Python and Terraform
	$(BIN)/ruff check --fix $(PY_SRC)
	$(BIN)/ruff format $(PY_SRC)
	terraform fmt -recursive infra/terraform

test: ## Unit tests (no cloud credentials, no database)
	$(BIN)/pytest

test-db: ## Tests against local Postgres (run `make db-up` first)
	$(BIN)/pytest -m db

db-up: ## Start local Postgres + pgvector in Docker and wait until healthy
	docker compose up -d --wait

db-down: ## Stop local Postgres (data kept in the `pgdata` volume)
	docker compose down

migrate: ## Apply SQL migrations to the database in .env
	$(BIN)/python -m common.migrate

# ---------------------------------------------------------------- infrastructure

tf-init: ## terraform init with the GCS remote-state backend (needs backend.hcl)
	@test -f $(TF_DIR)/backend.hcl || { echo "Missing $(TF_DIR)/backend.hcl (copy backend.hcl.example)"; exit 1; }
	terraform -chdir=$(TF_DIR) init -backend-config=backend.hcl

tf-validate: ## terraform fmt check + validate, no backend, no credentials (same as CI)
	terraform fmt -check -recursive infra/terraform
	terraform -chdir=$(TF_DIR) init -backend=false -input=false >/dev/null
	terraform -chdir=$(TF_DIR) validate

tf-plan: ## terraform plan -> tfplan (no changes applied)
	@test -f $(TF_DIR)/$(TF_VARS) || { echo "Missing $(TF_DIR)/$(TF_VARS) (copy terraform.tfvars.example)"; exit 1; }
	terraform -chdir=$(TF_DIR) plan -var-file=$(TF_VARS) -out=$(TF_PLAN)

up: ## [BILLABLE] Apply the saved plan from `make tf-plan`
	@test -f $(TF_DIR)/$(TF_PLAN) || { echo "No saved plan. Run 'make tf-plan' and review it first."; exit 1; }
	@read -r -p "This creates BILLABLE GCP resources. Type 'apply' to continue: " ans; [ "$$ans" = "apply" ]
	terraform -chdir=$(TF_DIR) apply $(TF_PLAN)
	@rm -f $(TF_DIR)/$(TF_PLAN)

down: ## Destroy ALL dev resources (stops idle billing)
	@test -f $(TF_DIR)/$(TF_VARS) || { echo "Missing $(TF_DIR)/$(TF_VARS)"; exit 1; }
	terraform -chdir=$(TF_DIR) destroy -var-file=$(TF_VARS)

status: ## List billable resources still running in the project (read-only)
	@test -n "$(GCP_PROJECT_ID)" || { echo "Set GCP_PROJECT_ID in .env"; exit 1; }
	@echo "== Vertex AI index endpoints (bill per node-hour while an index is deployed)"
	@gcloud ai index-endpoints list --project=$(GCP_PROJECT_ID) --region=$(GCP_REGION) \
		--format="table(displayName,deployedIndexes[].id)" 2>/dev/null || true
	@echo "== Cloud SQL instances"
	@gcloud sql instances list --project=$(GCP_PROJECT_ID) --format="table(name,state,settings.tier)" 2>/dev/null || true
	@echo "== Cloud Run services"
	@gcloud run services list --project=$(GCP_PROJECT_ID) --region=$(GCP_REGION) --format="table(metadata.name,status.url)" 2>/dev/null || true
	@echo "== PSC forwarding rules"
	@gcloud compute forwarding-rules list --project=$(GCP_PROJECT_ID) --format="table(name,region,IPAddress)" 2>/dev/null || true

# ---------------------------------------------------------------- query

serve: ## Run the API locally on :8080 (needs make db-up + ingested data + gcloud ADC)
	$(BIN)/uvicorn api.main:app --host 127.0.0.1 --port 8080 --reload

ask: ## [BILLABLE, <$0.01] Ask one question. Q="..." ARGS="--agent | --tickers JPM | --json"
	@test -n "$(Q)" || { echo 'usage: make ask Q="What were Apple net sales in fiscal 2025?"'; exit 1; }
	$(BIN)/python -m rag.ask $(ARGS) "$(Q)"

smoke: ## [BILLABLE, ~$0.07] Fixed question set -> results/smoke/<ts>.json (latency, cost, refusals)
	$(BIN)/python scripts/smoke_queries.py

smoke-cloud: ## [BILLABLE, ~$0.07] Same question set against the deployed API -> results/smoke/<ts>_cloud.json
	$(BIN)/python scripts/smoke_queries.py --api $$($(TF_OUT) api_url)

# ---------------------------------------------------------------- cloud (after `make up`)

TF_OUT  = terraform -chdir=$(TF_DIR) output -raw
GIT_SHA = $(shell git log -1 --format=%h 2>/dev/null)$(shell git diff --quiet HEAD 2>/dev/null || echo -dirty)

image: ## [BILLABLE, ~free tier] Build + push the image with Cloud Build (as the build SA)
	@REPO=$$($(TF_OUT) artifact_registry) && SA=$$($(TF_OUT) build_service_account) && \
	BUCKET=$$($(TF_OUT) bucket) && IMAGE=$$REPO/rag:$(GIT_SHA) && echo "building $$IMAGE" && \
	gcloud builds submit --project $(GCP_PROJECT_ID) --region $(GCP_REGION) \
	  --config cloudbuild.yaml --substitutions _IMAGE=$$IMAGE \
	  --service-account projects/$(GCP_PROJECT_ID)/serviceAccounts/$$SA \
	  --gcs-source-staging-dir gs://$$BUCKET/cloudbuild-source . && \
	echo $$IMAGE > .last-image

deploy: ## Roll the ingest job, then the API, to the image from `make image` (first time: run ingest-cloud in between)
	@test -f .last-image || { echo "run 'make image' first"; exit 1; }
	gcloud run jobs update $$($(TF_OUT) ingest_job) --image $$(cat .last-image) \
	  --region $(GCP_REGION) --project $(GCP_PROJECT_ID)
	gcloud run services update $$($(TF_OUT) api_service) --image $$(cat .last-image) \
	  --region $(GCP_REGION) --project $(GCP_PROJECT_ID)

ingest-cloud: ## [BILLABLE, ~$0.45 first run] Run the ingest job in the VPC; copy its report to results/
	gcloud run jobs execute $$($(TF_OUT) ingest_job) --region $(GCP_REGION) --project $(GCP_PROJECT_ID) --wait
	gcloud storage cp -n "gs://$$($(TF_OUT) bucket)/results/ingest/*.json" results/ingest/

cloud-ask: ## [BILLABLE, <$0.01] Ask the deployed API. Q="..." (uses your identity token)
	@test -n "$(Q)" || { echo 'usage: make cloud-ask Q="..."'; exit 1; }
	@curl -sS "$$($(TF_OUT) api_url)/query" -H "Authorization: Bearer $$(gcloud auth print-identity-token)" \
	  -H 'content-type: application/json' -d "$$(python3 -c 'import json,sys; print(json.dumps({"question": sys.argv[1]}))' "$(Q)")" \
	  | python3 -m json.tool

# ---------------------------------------------------------------- workloads (later phases)

corpus: ## Show the pinned 10-K corpus (re-pin: python -m ingest.corpus resolve)
	$(BIN)/python -m ingest.corpus show

ingest-dry: ## Download + parse + chunk; estimate tokens/cost (no API calls, no DB writes)
	$(BIN)/python -m ingest.run --dry-run $(ARGS)

ingest: ## [BILLABLE, ~$0.45 full corpus] Embed + store filings; unchanged ones are skipped. ARGS="--tickers AAPL"
	$(BIN)/python -m ingest.run $(ARGS)

eval: ## [BILLABLE] Run the evaluation pipeline against the golden set
	@echo "eval: not implemented yet (Phase 5)"; exit 1

bench: ## [BILLABLE] Benchmark Vector Search vs pgvector
	@echo "bench: not implemented yet (Phase 6)"; exit 1
