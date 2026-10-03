# Architecture decision records

| # | Decision | Status |
|---|---|---|
| [0000](0000-adr-template.md) | Template | — |
| [0001](0001-dual-vector-backends.md) | Two switchable vector backends behind one retrieval interface | Accepted |
| [0002](0002-private-networking.md) | Private networking without NAT or VPC connectors (PSA, PSC, Direct VPC egress) | Accepted |
| [0003](0003-postgres-schema-and-auth.md) | Own Postgres schema, SQL migrations, password in Secret Manager (never in state) | Accepted |
| [0004](0004-terraform-layout-and-deploy-model.md) | Terraform layout, remote state, eval-gated deploys | Accepted |
| [0005](0005-ingestion-parsing-chunking-embeddings.md) | Pinned corpus, 10-K parsing, token-aware chunking, Gemini embeddings | Accepted |
| [0006](0006-retrieval-generation-agent-api.md) | Retrieval, grounded generation, agent, API | Accepted (retrieval default revised by 0008) |
| [0007](0007-deployment-and-operations.md) | Deployment and operations on GCP | Accepted |
| [0008](0008-evaluation-and-release-gate.md) | Golden-set evaluation, LLM judge, release gate | Accepted |
| [0009](0009-vector-store-choice.md) | Vector store choice: pgvector by default, Vector Search for scale | Accepted |
