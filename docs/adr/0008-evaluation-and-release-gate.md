# ADR-0008: Golden-set evaluation, LLM judge and the release gate

- **Status:** Accepted
- **Date:** 2026-10-02

## Context
LLM output is non-deterministic: Gemini 3 runs at temperature 1.0, and the judge is an LLM too. So
quality has to be measured statistically, over a fixed question set, and a release should be
blocked when quality regresses. The design has to work in the client's GCP project with no CI
credentials (ADR-0004).

## Decisions

### Golden set v1 (`eval/golden_set/golden_v1.jsonl`, 99 items, frozen)
- **Composition:**
  - 60 numeric and 20 narrative single-company questions, from randomly sampled chunks
    (seed 20261002);
  - 9 two-company comparisons;
  - 10 hand-written unanswerable questions (out-of-corpus companies or years, forecasts, trivia).
- **Drafting:** Gemini 3.1 Pro drafted questions, reference answers and **verbatim evidence
  quotes**. Automatic checks rejected any quote that wasn't literally in the chunk (99/99 pass).
- **Human review:** the project owner reviewed and approved the set; `REVIEW.md` is the record.
  Pre-review checks found and fixed 2 comparisons where the drafter had used a JPMorgan
  *segment* row instead of the firm total.
- **Relevance is labelled by evidence quote, not chunk ID.** A retrieved chunk is relevant if it's
  from the right company and contains the quote (whitespace and case normalised). Labels survive
  re-chunking, and both vector backends are scored identically.
- **Known bias:** generated questions may echo source wording. The drafter was told to paraphrase,
  which in turn handicaps keyword search.

### Metrics (`eval/metrics.py`)
- **Retrieval:**
  - recall@1/3/6 and MRR of the rank at which *all* of an item's evidence is covered
    (comparisons need both companies);
  - reported per category.
- **Answers (LLM judge):**
  - correctness score against the reference (correct 1, partially correct 0.5);
  - faithfulness: every claim supported by the *retrieved sources*, judged independently of the
    reference;
  - hallucination rate: an unsupported claim or a fabricated answer;
  - refusal accuracy on unanswerable questions, and false-refusal rate on answerable ones;
  - invalid citations.
- **Ops:** errors, retried requests, server latency p50/p95, cost per 1K queries.

### Judge: Gemini 3.1 Pro (preview), a different model from the generator
- **Why a different model:** it reduces self-preference bias. Structured JSON output, thinking
  LOW.
- **Cost:** about $1.32 per full run (about $0.013 per judgment), roughly 3× the system's own
  cost.

### Retrieval default: vector-only (reversing Phase 3's hybrid default)
Retrieval-only runs on the full golden set (`results/eval/*_retrieval.json`):

| mode | recall@1 | recall@6 | MRR |
|---|---|---|---|
| vector | 0.596 | 0.843 | 0.683 |
| hybrid, vector weight 0.7 | 0.573 | 0.843 | 0.671 |
| hybrid 0.5 | 0.494 | 0.798 | 0.600 |
| hybrid 0.3 | 0.101 | 0.360 | 0.182 |

- Keyword weight never helped. Hybrid stays available (`RETRIEVAL_MODE=hybrid`).
- **Caveat:** paraphrased questions disadvantage keyword search; users typing the filing's exact
  terms may differ.

### Regression gate (`eval/gate.py`, baseline `eval/baseline.json`)
- **Tolerances** absorb noise: about 1.1 points per answerable item and 10 points per
  unanswerable item.
  - recall@6: −0.03
  - MRR: −0.05
  - correctness: −0.05
  - hallucination rate: +0.03
  - false refusals: +0.05
  - refusal accuracy: −0.10
  - invalid citations: +2
- **Hard limits** regardless of the baseline: hallucination rate ≤ 0.10, refusal accuracy ≥ 0.80,
  request errors ≤ 2.
- **Errored runs never become a baseline.** The first full run had 5 request errors during Gemini
  endpoint congestion and was rejected as a baseline.
  - The API now maps exhausted model timeouts to **504** (they were 500s).
  - The runner retries transient 429/5xx up to twice and counts what still fails as errors.

### Vertex AI Pipelines (KFP v2)
- **Steps:** three **container components** running the *application image itself*
  (`python -m eval.steps answer|judge|score`), exchanging JSON artifacts via the pipeline root
  in GCS.
  - Not lightweight Python components: those `pip install kfp` into the image at start-up, which
    fails for the non-root image and would add a moving dependency.
  - No `from __future__ import annotations` in the pipeline module, because KFP reads type
    annotations at runtime.
- **Identity:** runs as `rag-dev-eval` and calls the API over HTTPS with an **ID token minted by
  the IAM Credentials API**. Vertex custom jobs' metadata server returns 404 for ID tokens, so the
  eval SA has `roles/iam.serviceAccountOpenIdTokenCreator` on itself only (OIDC tokens, not
  access tokens).
- **Image pulls:** the Vertex AI custom-code service agent got repository-level
  `artifactregistry.reader`.
- **Gate failure:** the score step exits non-zero when the gate fails, which fails the run.

### Evaluation-gated release (`scripts/release.sh`, `make release`)
Build → `gcloud run deploy --no-traffic --tag candidate` → pipeline against the candidate's tag
URL → `update-traffic --to-tags candidate=100` only if the run succeeded. On failure, live traffic
is untouched and the candidate stays reachable for debugging. No CI credentials are involved.

## Measured
- **Baseline:** `results/eval/20261002T135259Z_api_pgvector_vector_baseline.json`.
  - recall@1/3/6: 0.596 / 0.742 / 0.854; MRR 0.686.
  - correctness 0.938; hallucination 0%; faithfulness 100%; refusal accuracy 10/10; false
    refusals 4.5%; 0 invalid citations; $4.33 per 1K queries.
  - All 5 wrong answers were retrieval misses that ended in refusals or faithful answers.
- **First release attempt: blocked.** All 99 requests got 401 (ID token minted for the tag URL).
  The gate failed closed as designed.
- **Second release:** `results/eval/20261002T170420Z_release-22331e5.json`.
  - Gate passed; the candidate was promoted.
  - Same code, second evaluation: retrieval identical; correctness 0.927 (−0.011); false refusals
    5.6% (+1 item). This is the observed run-to-run noise, consistent with the tolerances.

## Consequences
- **Each full evaluation costs about $1.75** (system $0.43 + judge $1.32) and takes about 10–15
  minutes on the shared Gemini endpoint.
- **Judge drift:** changing the judge model invalidates the baseline. Re-baseline deliberately
  with `--update-baseline`, and record why.
- **Comparisons are the weak spot** (recall@6 0.44). One `/query` retrieval rarely surfaces both
  companies. The agent's `compare_companies` tool does, and query decomposition is the next
  improvement to measure.
