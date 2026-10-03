"""Derive the cost model's inputs from saved runs (so docs/cost_model.md types no numbers by hand).

    python scripts/cost_inputs.py   # -> results/cost/cost_inputs.json

Sources:
- per-query cost: the two full golden-set evaluations against the deployed API (99 queries each)
- token mix: per-query usage in the cloud smoke runs (direct /query and /agent)
- one-off costs: ingestion, golden-set drafting and evaluation-run reports
- fixed monthly costs: list prices in common/pricing.py
"""

from __future__ import annotations

import glob
import json
import statistics
from pathlib import Path
from typing import Any

from common import pricing as p

EVAL_RUNS = [
    "results/eval/20261002T135259Z_api_pgvector_vector_baseline.json",
    "results/eval/20261002T170420Z_release-22331e5.json",
]
CLOUD_SMOKE = sorted(
    f for f in glob.glob("results/smoke/*Z_cloud.json")
)  # smoke runs, not the A/B file
CLOUD_INGEST = "results/ingest/20261002T110300Z_ingest_cloud.json"
GOLDEN_DRAFT = sorted(glob.glob("results/golden/*_draft.json"))[-1]


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q / 100 * (len(ordered) - 1)))]


def load(path: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(Path(path).read_text())
    return data


def main() -> None:
    costs = [
        i["estimated_cost_usd"]
        for f in EVAL_RUNS
        for i in load(f)["items"]
        if i.get("estimated_cost_usd")
    ]
    per_query = {
        "n": len(costs),
        "mean_usd": round(statistics.mean(costs), 6),
        "median_usd": round(statistics.median(costs), 6),
        "p95_usd": round(pct(costs, 95), 6),
        "per_1k_usd": round(1000 * statistics.mean(costs), 2),
        "sources": EVAL_RUNS,
    }

    def token_mix(kind: str) -> dict[str, Any]:
        rows = [q for f in CLOUD_SMOKE for q in load(f)["queries"] if q["kind"] == kind]
        mean = {
            k: round(statistics.mean(r["usage"][k] for r in rows), 1)
            for k in (
                "embedding_tokens",
                "input_tokens",
                "output_tokens",
                "thinking_tokens",
                "model_calls",
            )
        }
        cost = statistics.mean(r["estimated_cost_usd"] or 0 for r in rows)
        return {
            "n": len(rows),
            "mean_tokens": mean,
            "mean_cost_usd": round(cost, 6),
            "per_1k_usd": round(1000 * cost, 2),
            "sources": CLOUD_SMOKE,
        }

    direct, agent = token_mix("direct"), token_mix("agent")
    # Today's price (introductory through 2026-12-31), as used by the logged per-query costs.
    price = p.generation_price("gemini-3.8-flash") or p.GENERATION_PRICES["gemini-3.8-flash"]
    m = direct["mean_tokens"]
    split = {
        "input_usd_per_query": round(m["input_tokens"] * price.input_per_1m / 1e6, 6),
        "output_usd_per_query": round(
            (m["output_tokens"] + m["thinking_tokens"]) * price.output_per_1m / 1e6, 6
        ),
        "embedding_usd_per_query": round(
            m["embedding_tokens"] * p.EMBEDDING_USD_PER_1M_TOKENS["gemini-embedding-001"] / 1e6, 8
        ),
    }
    std = p.GENERATION_PRICES["gemini-3.8-flash"]
    after_promo = round(
        1000
        * (
            m["input_tokens"] * std.input_per_1m
            + (m["output_tokens"] + m["thinking_tokens"]) * std.output_per_1m
        )
        / 1e6,
        2,
    )

    evals = [load(f) for f in EVAL_RUNS]
    h = p.HOURS_PER_MONTH
    out = {
        "prices_checked": p.PRICES_CHECKED,
        "per_query_direct_eval": per_query,
        "direct_token_mix": direct,
        "agent_token_mix": agent,
        "direct_cost_split_promo_prices": split,
        "direct_per_1k_after_promo_usd": after_promo,
        "one_off": {
            "ingest_full_corpus_usd": load(CLOUD_INGEST)["summary"]["estimated_cost_usd"],
            "golden_set_draft_usd": load(GOLDEN_DRAFT)["estimated_cost_usd"],
            "eval_run_usd": [
                round(e["cost_usd"]["system_estimated"] + e["cost_usd"]["judge_estimated"], 4)
                for e in evals
            ],
        },
        "fixed_monthly_usd": {
            "cloud_sql_db_f1_micro_10gib": round(
                p.CLOUD_SQL_DB_F1_MICRO_PER_HOUR * h + p.CLOUD_SQL_SSD_PER_GIB_HOUR * 10 * h, 2
            ),
            "vector_search_node_plus_psc_optional": round(
                (p.VECTOR_SEARCH_E2_STANDARD_2_PER_NODE_HOUR + p.PSC_ENDPOINT_PER_HOUR) * h, 2
            ),
        },
    }
    Path("results/cost").mkdir(parents=True, exist_ok=True)
    Path("results/cost/cost_inputs.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
