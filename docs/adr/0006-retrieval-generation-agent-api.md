# ADR-0006: Retrieval, generation, agent and API

- **Status:** Accepted
- **Date:** 2026-10-02

## Context
Phase 3 turns the indexed corpus into answers. The design priorities, in order:
1. Grounded answers with verifiable citations.
2. Safe failure (refuse rather than invent).
3. Measurable latency and cost per request.
4. One code path shared by the API, CLI, evaluation and benchmark.

## Decisions

### Generation model: `gemini-3.8-flash` on the **global** endpoint, thinking level LOW
- **Current models:** checked on 2026-10-02. Vertex docs are now branded "Gemini Enterprise Agent
  Platform". Gemini 3.x models are listed; the 2.5 models still work.
- **Gemini 3.x is served only from the `global` endpoint for this project** (404 in
  `us-central1`). Embeddings stay regional.
  - **Trade-off:** the global endpoint doesn't guarantee where processing happens.
  - **Mitigation:** for clients with data-residency rules, set `GENAI_LOCATION=us-central1` and a
    regional model such as `gemini-2.5-flash`. The code needs no changes.
- **Price:** $0.75 / $3.75 per 1M input/output tokens, an introductory rate through 2026-12-31,
  then $1.50 / $7.50. Thinking tokens bill at the output rate.
- **Thinking set to LOW:** in tests, `LOW` produced 0 thinking tokens with correct answers, while
  `MEDIUM` and the default used about 230 per answer. `MINIMAL` is rejected by this model. The
  agent uses a 512-token thinking budget instead, because planning tool calls benefits from
  some reasoning.
- **Temperature stays at the Gemini 3 default (1.0):** Google recommends against lowering it
  (looping, degraded reasoning). Answers are therefore not word-for-word deterministic; see
  `docs/exec_brief.md`.

### Retrieval: hybrid (pgvector + Postgres full-text) behind LlamaIndex retrievers
- **Search:** `PgSearch` runs vector search (cosine, HNSW) and keyword search (`tsvector`,
  OR semantics, `ts_rank_cd`).
- **Filters:** company, fiscal year and Item are applied *inside* the SQL. pgvector's
  `hnsw.iterative_scan = relaxed_order` keeps filtered vector queries returning k rows, which a
  test verifies; `ef_search` is 100.
- **LlamaIndex:** the two searches are LlamaIndex `BaseRetriever`s, combined by
  `QueryFusionRetriever` (single query, so the LLM is never called for query rewriting). The
  Vertex AI Vector Search retriever plugs into the same interface in Phase 4.
- **Fusion: weighted relative-score fusion, vector 0.7 / keyword 0.3 (configurable).**
  - Equal-weight reciprocal rank fusion was implemented first. A 6-question diagnostic with
    verified answer passages showed it pushed correct vector hits down. One example: Walmart's
    head-count passage says "associates", the question said "employees", keyword search
    matched other chunks, and the correct #1 vector hit fell out of the top 6.
  - Weighted relative-score fusion kept the answer passage at rank 1 in all 6 cases, as did
    vector-only search.
  - **This is a diagnostic, not an evaluation.** Phase 5 will compare `vector` vs `hybrid` on
    recall@k over the golden set, and the default will follow that evidence.
- **Defaults:** `top_k = 6` context chunks; 30 candidates per retriever before fusion.

### Grounding and refusal policy (system prompt)
- **Sources only:** answer from the numbered sources, cite every claim (`[n]`), and copy
  figures exactly with units and fiscal year. Never guess units.
- **Wording differences are not grounds for refusal.** Answer when the filing uses different
  wording or an approximate figure, and say how the filing words it.
  - This fixed an over-refusal found in the smoke run: the model had refused "employees"
    because the filing says "associates".
  - If the exact measure isn't reported but a related one is, give it under the filing's own
    name and flag the difference.
- **Never substitute a different company or fiscal year** (e.g. Tesla isn't in the corpus;
  Apple FY2019 isn't either).
- **Fixed refusal sentence** (`"I could not find this in the provided filings."`), so refusals
  are machine-detectable and measurable.
- **Invalid citations are flagged:** numbers pointing at no source are a hallucination signal,
  surfaced in the API response and logs.

### Agent: LangChain `create_agent` with two tools
- **Tools:**
  - `search_filings(query, ticker?, fiscal_year?)`
  - `compare_companies(ticker_a, ticker_b, topic)`, which runs separate filtered retrievals so
    both companies are represented.
- **Citations:** each excerpt gets a stable `[S#]` label in a per-request registry, so citations
  map to exact chunks. Grouped citations like `[S1, S5]` are parsed.
- **Tool errors** (e.g. an unknown ticker) are returned to the model as `ToolException`s so it
  can correct itself, instead of crashing the run.
- **Model class:** `ChatGoogleGenerativeAI` (`langchain-google-genai`) with `vertexai=True`.
  `ChatVertexAI` is deprecated, to be removed in LangChain 4.0.
- **Usage accounting:** LangChain's `output_tokens` *includes* reasoning tokens. They're split
  out to avoid double-counting thinking cost.
- **When to use which path:** the agent costs more and is slower (2 model calls). Single-company
  factual questions should use `/query`; the agent is for comparisons and multi-step questions.

### API: FastAPI on Cloud Run
- **Endpoints:** `POST /query`, `POST /agent`, `GET /health`. Not `/healthz`: Cloud Run reserves
  paths ending in `z`.
- **One structured log line per request:**
  - request ID (accepted from `X-Request-ID` or generated), status and latency;
  - per-stage timings (embed, retrieve, generate);
  - token counts (embedding, input, output, thinking) and estimated cost;
  - answered/refused, citation counts and invalid citations.
- **Privacy:** the question text is *not* logged, only its length and a hash, because user
  prompts may contain sensitive information.
- **Error mapping:** model API errors → 502; unavailable backend → 501; bad tickers or
  parameters → 422.

### Reliability: one visible retry layer
- **The google-genai SDK's built-in retries are disabled** (`attempts=1`). They retried 429s
  silently and showed up as unexplained 10–40 s latencies.
- **All model retries go through one policy:** exponential backoff with jitter on 408/429/5xx,
  and every retry is logged.
- **Tail latency:** remaining spikes are service-side queueing on the shared-capacity global
  endpoint. For example, a 70-token answer once took 23 s with no thinking and no retries.
  - **Production fix:** Provisioned Throughput (reserved capacity), and/or a regional endpoint.
  - Noted in the cost model.

## Measured (saved runs)
**Smoke run** (`results/smoke/20261002T093608Z.json`): local laptop → Vertex AI, local pgvector,
12 direct and 2 agent questions, sequential.

| Path | p50 | p95 | Est. cost / 1K queries | Expected answer/refusal | Invalid citations |
|---|---|---|---|---|---|
| `/query` (n=12) | 2.7 s | 5.2 s | $4.48 | 12/12 | 0 |
| `/agent` (n=2) | 9.2 s | 9.4 s | $6.25 | 2/2 | 0 |

The earlier smoke runs, `20261002T093108Z` and `20261002T093345Z`, are kept as the record of the
fusion and refusal problems described above.

## Consequences
- The answer quality claims above are spot checks. The real measure is the Phase 5 golden set:
  recall@k, faithfulness, and refusal correctness.
- Cost per query is dominated by input tokens (about 4–7K context tokens), so `top_k` and chunk
  size are the main cost levers.
