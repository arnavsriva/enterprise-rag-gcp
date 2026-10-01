# Contributing

Conventions and guardrails for working on this repository.

## Guardrails

- **No hardcoded credentials or project IDs.** Use `.env` and `terraform.tfvars` (both
  git-ignored); only `*.example` files with placeholder values are committed.
- **Never invent metrics.** Every number in the README, docs, or `results/` must come from a saved
  run in `results/`, recorded with the command, timestamp, and configuration that produced it.
- **Cost discipline.** Target region is `us-central1`. Vertex AI Vector Search endpoints and Cloud
  SQL bill while idle, so every resource must be cleanly destroyable with `make down`. Tear down
  between work sessions. Review `make tf-plan` output and the expected cost before `make up`.
- **Credential-free tests and CI.** Unit tests must not need cloud credentials. Tests that hit GCP
  are marked `@pytest.mark.integration` and are deselected by default. CI
  (`.github/workflows/ci.yml`) must stay credential-free and non-billable.
- **Local first.** Develop against Postgres + pgvector in Docker before deploying to GCP.

## Conventions

- **Python:** 3.11, fully type-hinted, `mypy --strict` clean. Lint and format with `ruff`
  (line length 100).
- **Config:** environment variables via pydantic-settings. See `.env.example`.
- **Logging:** structured (JSON). Never log secrets or full document text.
- **Terraform:** reusable modules in `infra/terraform/modules/`, composed in
  `infra/terraform/envs/dev/`. Run `terraform fmt` before committing. The partial GCS backend
  config lives in git-ignored `backend.hcl` (copy from `backend.hcl.example`).
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/):
  `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `ci`, with a scope such as `infra` or `ingest`.
- **Design decisions:** record significant ones as ADRs in `docs/adr/` (start from
  `0000-adr-template.md`).

## Common commands

```bash
make setup    # venv + deps + pre-commit hooks
make lint     # ruff + mypy
make fmt      # auto-format Python and Terraform
make test     # unit tests
```

Infrastructure: `make tf-init`, `make tf-plan`, `make up`, `make down`. Workloads:
`make ingest`, `make eval`, `make bench`. Run `make help` for the full list.
