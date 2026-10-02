"""Turn per-question results + judgments into the evaluation metrics.

Retrieval (answerable questions only):
- recall@k: an item is a hit at k if, for EVERY company in its evidence, some source in the top k
  is from that company and contains that company's evidence quote (comparisons need both sides).
- mrr: mean reciprocal rank of that full-coverage rank (0 when never covered).

Answers:
- correctness_score: mean over answerable (correct=1, partially_correct=0.5, otherwise 0).
- accuracy: share of answerable judged fully correct.
- false_refusal_rate: share of answerable the system refused.
- refusal_accuracy: share of unanswerable appropriately refused, without fabrication.
- hallucination_rate: share of ALL items with an unsupported claim or a fabricated answer.
- faithfulness_rate: share of judged items whose claims are all supported by their sources.
"""

from __future__ import annotations

import statistics
from typing import Any

from eval.golden import GoldenItem, contains_evidence
from eval.judge import Judgment

CORRECTNESS_SCORE = {"correct": 1.0, "partially_correct": 0.5, "incorrect": 0.0, "refused": 0.0}


def coverage_rank(item: GoldenItem, sources: list[dict[str, Any]]) -> int | None:
    """1-based rank at which all of the item's evidence is covered, or None."""
    ranks = []
    for ev in item.evidence:
        rank = next(
            (
                i
                for i, s in enumerate(sources, start=1)
                if s["ticker"] == ev.ticker and contains_evidence(s.get("text") or "", ev.text)
            ),
            None,
        )
        if rank is None:
            return None
        ranks.append(rank)
    return max(ranks) if ranks else None


def _pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))]


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def retrieval_metrics(
    items: list[GoldenItem], rows: dict[str, dict[str, Any]], ks: tuple[int, ...]
) -> dict[str, Any]:
    answerable = [i for i in items if i.answerable and i.id in rows and not rows[i.id].get("error")]
    ranks = {i.id: coverage_rank(i, rows[i.id]["sources"]) for i in answerable}
    out: dict[str, Any] = {"n": len(answerable)}
    for k in ks:
        out[f"recall@{k}"] = _rate(
            sum(1 for r in ranks.values() if r is not None and r <= k), len(answerable)
        )
    out["mrr"] = (
        round(statistics.mean([1 / r if r else 0.0 for r in ranks.values()]), 4) if ranks else None
    )
    by_cat: dict[str, Any] = {}
    for cat in ("numeric", "narrative", "comparison"):
        sel = [ranks[i.id] for i in answerable if i.category == cat]
        k = max(ks)
        by_cat[cat] = {
            "n": len(sel),
            f"recall@{k}": _rate(sum(1 for r in sel if r and r <= k), len(sel)),
        }
    out["by_category"] = by_cat
    out["ranks"] = ranks
    return out


def answer_metrics(
    items: list[GoldenItem], rows: dict[str, dict[str, Any]], judgments: dict[str, Judgment]
) -> dict[str, Any]:
    ok = [
        i
        for i in items
        if i.id in rows
        and not rows[i.id].get("error")
        and i.id in judgments
        and not judgments[i.id].error
    ]
    answerable = [i for i in ok if i.answerable]
    unanswerable = [i for i in ok if not i.answerable]
    j = judgments
    scores = [CORRECTNESS_SCORE.get(j[i.id].correctness or "incorrect", 0.0) for i in answerable]
    by_cat: dict[str, Any] = {}
    for cat in ("numeric", "narrative", "comparison"):
        sel = [
            CORRECTNESS_SCORE.get(j[i.id].correctness or "incorrect", 0.0)
            for i in answerable
            if i.category == cat
        ]
        by_cat[cat] = {
            "n": len(sel),
            "correctness_score": round(statistics.mean(sel), 4) if sel else None,
        }
    return {
        "n_judged": len(ok),
        "correctness_score": round(statistics.mean(scores), 4) if scores else None,
        "accuracy": _rate(
            sum(j[i.id].correctness == "correct" for i in answerable), len(answerable)
        ),
        "false_refusal_rate": _rate(
            sum(j[i.id].correctness == "refused" for i in answerable), len(answerable)
        ),
        "refusal_accuracy": _rate(
            sum(bool(j[i.id].appropriate_refusal) and not j[i.id].fabricated for i in unanswerable),
            len(unanswerable),
        ),
        "hallucination_rate": _rate(sum(j[i.id].hallucinated for i in ok), len(ok)),
        "faithfulness_rate": _rate(
            sum(j[i.id].faithful is not False and not j[i.id].fabricated for i in ok), len(ok)
        ),
        "invalid_citations_total": sum(len(rows[i.id].get("invalid_citations") or []) for i in ok),
        "by_category": by_cat,
    }


def ops_metrics(rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    good = [r for r in rows.values() if not r.get("error")]
    server = [r["server_ms"] for r in good if r.get("server_ms") is not None]
    costs = [r.get("estimated_cost_usd") or 0.0 for r in good]
    return {
        "n": len(rows),
        "errors": sum(1 for r in rows.values() if r.get("error")),
        "retried_requests": sum(1 for r in rows.values() if (r.get("attempts") or 1) > 1),
        "latency_ms_p50": round(statistics.median(server), 1) if server else None,
        "latency_ms_p95": round(_pct(server, 95), 1) if server else None,
        "mean_cost_usd": round(statistics.mean(costs), 6) if costs else None,
        "cost_per_1k_queries_usd": round(1000 * statistics.mean(costs), 2) if costs else None,
    }
