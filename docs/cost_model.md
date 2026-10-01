# Cost Model

_Stub — populated from real usage logs in `results/` and published GCP pricing (with date checked)._

## Budget
- Project ceiling: ~$120 of GCP credits (region `us-central1`).

## Fixed (idle) costs
| Resource | Unit price (source, date) | Hours/month | Monthly |
|---|---|---|---|
| Vertex AI Vector Search endpoint | TBD | TBD | TBD |
| Cloud SQL Postgres | TBD | TBD | TBD |

## Variable costs — cost per 1K queries
| Component | Basis | Per 1K queries |
|---|---|---|
| Embedding (query) | TBD | TBD |
| Gemini generation (input + output tokens) | TBD | TBD |
| Cloud Run | TBD | TBD |

## One-off costs
| Item | Basis | Cost |
|---|---|---|
| Corpus embedding (ingestion) | TBD | TBD |
| Eval runs (LLM-as-judge) | TBD | TBD |
