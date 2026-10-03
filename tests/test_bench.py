from typing import Any

import pytest

from bench.report import render
from bench.vector_bench import filters_for, latency_summary, monthly_costs, pct
from eval.golden import Evidence, GoldenItem
from rag.types import Filters


def test_percentiles_and_summary() -> None:
    values = [float(v) for v in range(1, 101)]
    assert pct(values, 50) == 51.0 and pct(values, 95) == 95.0 and pct(values, 100) == 100.0
    s = latency_summary([10.0, 20.0, 30.0])
    assert (s["n"], s["p50_ms"], s["mean_ms"]) == (3, 20.0, 20.0)


def test_filters_by_scenario() -> None:
    item = GoldenItem("x", "comparison", "q", True, "a", [Evidence("AAPL", "e")], ["AAPL", "MSFT"])
    assert filters_for(item, "unfiltered") == Filters()
    assert filters_for(item, "ticker_filtered") == Filters(tickers=("AAPL", "MSFT"))
    no_ticker = GoldenItem("u", "unanswerable", "q", False, "n/a", [], [])
    assert filters_for(no_ticker, "ticker_filtered") == Filters()


def test_monthly_costs_use_list_prices() -> None:
    c = monthly_costs(index_gib=0.0124)
    assert c["pgvector_cloud_sql_db_f1_micro_10gib_ssd"] == pytest.approx(
        0.0105 * 730 + 0.000232877 * 10 * 730, abs=0.01
    )
    assert c["vector_search_total_serving"] == pytest.approx(0.0938084 * 730 + 0.01 * 730, abs=0.01)
    assert c["vector_search_one_off_build_usd"] == pytest.approx(0.0124 * 3.0, abs=1e-4)


def test_report_renders_every_backend_and_scenario() -> None:
    lat = {"n": 3, "p50_ms": 1.0, "p95_ms": 2.0, "p99_ms": 3.0, "mean_ms": 1.5}
    entry: dict[str, Any] = {
        "evidence_recall@6": 0.85,
        "evidence_recall@10": 0.9,
        "latency_total": lat,
        "latency_ann_only": lat,
    }
    report = {
        "started_at": "t0",
        "finished_at": "t1",
        "environment": "test",
        "config": {
            "vectors": 4340,
            "dim": 768,
            "embedding_model": "m",
            "questions": 99,
            "answerable": 89,
            "rounds": 3,
            "pgvector": "hnsw",
            "cloud_sql": "f1",
            "vertex_vector_search": "ah",
            "ground_truth": "exact",
        },
        "results": {
            "pgvector": {
                "unfiltered": {**entry, "ann_overlap@10_vs_exact": 0.99},
                "ticker_filtered": entry,
            },
            "vertex_vector_search": {
                "unfiltered": {**entry, "ann_overlap@10_vs_exact": 0.97},
                "ticker_filtered": entry,
            },
        },
        "monthly_cost_usd": monthly_costs(0.0124),
    }
    md = render(report, "results/bench/x.json")
    assert md.count("| Cloud SQL pgvector |") == 2 and md.count("| Vertex AI Vector Search |") == 2
    assert "0.990" in md and "0.970" in md and "Vector Search incremental total" in md
