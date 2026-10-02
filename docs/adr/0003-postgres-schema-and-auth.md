# ADR-0003: Own Postgres schema, SQL migrations, and password auth via Secret Manager

- **Status:** Accepted
- **Date:** 2026-10-02

## Context
LlamaIndex's `PGVectorStore` creates and owns its own table layout. We need a schema shared with
the Vector Search ingestion path, filterable 10-K metadata (company, fiscal year, Item), and
predictable migrations across local Docker, CI and Cloud SQL.

## Decision
- **Schema:** we own it (`common/migrations/`).
  - `documents`: one row per filing, keyed by SEC accession number.
  - `chunks`: content, Item, token count, `vector(768)`, jsonb metadata. HNSW index with cosine ops.
- **Retrieval:** LlamaIndex reads this schema through custom retrievers, instead of the default
  `PGVectorStore` tables.
- **Migrations:** a tiny forward-only runner (`python -m common.migrate`):
  - checksums detect edited migrations;
  - a Postgres advisory lock makes concurrent runs safe.
- **Local parity:** the `pgvector/pgvector:pg16` image matches Cloud SQL `POSTGRES_16`.
- **Auth:** a built-in Postgres user whose password lives in Secret Manager and is mounted into
  Cloud Run as an env var.
- **Password handling:** Terraform generates the password as an *ephemeral* value and writes it
  only to write-only arguments (`password_wo`, `secret_data_wo`). It never appears in state or
  plan output.

## Options not chosen
- **Cloud SQL IAM database authentication:** it removes the password entirely, but needs grants for
  the IAM principal on schema objects and token refresh in the connection pool. This is the
  recommended hardening step for production.
- **Alembic:** too heavy for a handful of raw-SQL migrations with no ORM models.

## Consequences
- The embedding dimension (768) is fixed in SQL. Changing the embedding model to a different
  dimension needs a new migration and a full re-embed.
- The Cloud SQL tier is `db-f1-micro`: shared-core with no SLA, which is fine for a few thousand
  vectors. Benchmark latency numbers are reported with the tier noted.
