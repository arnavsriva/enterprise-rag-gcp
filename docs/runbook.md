# Deployment Runbook

_Stub — completed as each phase lands._

## 0. Prerequisites
- gcloud CLI authenticated (`gcloud auth application-default login`), Terraform >= 1.6, Python 3.11.
- Required APIs enabled (list TBD in foundations phase).
- Remote-state bucket bootstrapped once (procedure TBD).

## 1. Apply
`make tf-init` → `make tf-plan` → review plan + cost → `make up`

## 2. Ingest
`make ingest` — TBD

## 3. Evaluate
`make eval` — TBD

## 4. Teardown
`make down` — then verify no billable resources remain (checklist TBD:
Vector Search endpoints, Cloud SQL instances, Cloud Run services, buckets).

## Troubleshooting
TBD
