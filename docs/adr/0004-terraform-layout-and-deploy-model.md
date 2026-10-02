# ADR-0004: Terraform layout, remote state, and eval-gated deploys

- **Status:** Accepted
- **Date:** 2026-10-02

## Context
Infrastructure must be reproducible inside a client project, fully destroyable to stop idle
billing, and deployable without giving CI cloud credentials.

## Decision
**Layout:**
- `infra/terraform/modules/` holds single-purpose modules: project_services, network, iam,
  storage, artifact_registry, cloud_sql, vector_search, cloud_run_service, cloud_run_job, budget.
- `infra/terraform/envs/dev/` composes them. Another environment is another folder with its own
  tfvars and state prefix.

**State:**
- A GCS bucket with versioning, created once by `scripts/bootstrap_state.sh`.
- The bucket is *outside* Terraform, so `make down` can never delete state.
- Backend config is partial (`backend.hcl`, git-ignored), so no project identifiers are committed.

**Destroyability:**
- `deletion_protection = false` on Cloud SQL and Cloud Run.
- `force_destroy` and zero soft-delete retention on the bucket.
- A random suffix on the Cloud SQL instance name, because names can't be reused for about a week.

**Cost switches:**
- `vector_search_deployed` (default `false`) controls the only large idle cost.
- An optional budget alert tracks spend with credits excluded.

**Deploy model:**
- Terraform owns service configuration (SA, VPC, env, secrets, scaling).
- Image rollouts happen outside Terraform; it ignores `image` and `traffic`. The flow is:
  1. Build the image with Cloud Build.
  2. Deploy a tagged revision with no traffic.
  3. Run the eval pipeline against the tagged URL.
  4. Shift traffic only if no metric regresses against the stored baseline.

**CI:**
- GitHub Actions runs lint, tests, migrations against a pgvector service container, and
  `terraform validate` with `-backend=false`.
- No cloud credentials and nothing billable.

## Consequences
- Running `terraform apply` won't roll back an image deployed by the deploy script. That is
  intentional.
- The first apply uses Google's public placeholder images until real images are pushed.
- APIs stay enabled after `make down` (free, and disabling them is slow and disruptive).
- Requires Terraform ≥ 1.11 (ephemeral values and write-only arguments).
