# Enterprise RAG Platform on GCP
# Targets marked [BILLABLE] create or modify paid GCP resources — review the plan + cost first.

SHELL      := /bin/bash
PYTHON     ?= python3.11
VENV       := .venv
BIN        := $(VENV)/bin
TF_DIR     := infra/terraform/envs/dev
TF_VARS    := terraform.tfvars
TF_PLAN    := tfplan
PY_SRC     := ingest rag api eval tests

.DEFAULT_GOAL := help
.PHONY: help setup lint fmt test tf-init tf-plan up ingest eval bench down

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- local dev

setup: ## Create venv, install dev deps, install pre-commit hooks
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements-dev.txt
	$(BIN)/pip install -e .
	$(BIN)/pre-commit install

lint: ## Ruff lint + format check + mypy
	$(BIN)/ruff check $(PY_SRC)
	$(BIN)/ruff format --check $(PY_SRC)
	$(BIN)/mypy $(PY_SRC)

fmt: ## Auto-format Python and Terraform
	$(BIN)/ruff check --fix $(PY_SRC)
	$(BIN)/ruff format $(PY_SRC)
	terraform fmt -recursive infra/terraform

test: ## Run unit tests (no cloud credentials needed)
	$(BIN)/pytest

# ---------------------------------------------------------------- infrastructure

tf-init: ## terraform init with the GCS remote-state backend (needs backend.hcl)
	@test -f $(TF_DIR)/backend.hcl || { echo "Missing $(TF_DIR)/backend.hcl (copy backend.hcl.example)"; exit 1; }
	terraform -chdir=$(TF_DIR) init -backend-config=backend.hcl

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

# ---------------------------------------------------------------- workloads (later phases)

ingest: ## [BILLABLE] Download, chunk, embed, upsert filings
	@echo "ingest: not implemented yet (ingestion phase)"; exit 1

eval: ## [BILLABLE] Run the evaluation pipeline against the golden set
	@echo "eval: not implemented yet (eval phase)"; exit 1

bench: ## [BILLABLE] Benchmark Vector Search vs pgvector
	@echo "bench: not implemented yet (comparison phase)"; exit 1
