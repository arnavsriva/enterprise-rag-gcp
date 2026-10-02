from types import SimpleNamespace
from typing import Any

import pytest

import rag.vector_search as vs
from common.config import Settings
from rag.types import Filters
from rag.vector_search import (
    DatapointSpec,
    VectorSearchConfig,
    VertexIndexWriter,
    VertexMatcher,
    config_from_settings,
    to_datapoint,
    to_namespaces,
)

CFG = VectorSearchConfig("proj", "us-central1", "123", "456", "rag_dev_deployed", "10.10.0.5")


class FakeIndexClient:
    def __init__(self) -> None:
        self.upserts: list[Any] = []
        self.removes: list[Any] = []

    async def upsert_datapoints(self, request: Any) -> None:
        self.upserts.append(request)

    async def remove_datapoints(self, request: Any) -> None:
        self.removes.append(request)


def spec(i: int, item: str | None = "7") -> DatapointSpec:
    return DatapointSpec(f"doc:{i}", [0.1, 0.2], "AAPL", item, 2025)


def test_datapoint_has_token_restricts() -> None:
    dp = to_datapoint(spec(1, item=None))
    assert dp.datapoint_id == "doc:1"
    assert list(dp.feature_vector) == pytest.approx([0.1, 0.2])
    restricts = {r.namespace: list(r.allow_list) for r in dp.restricts}
    assert restricts == {"ticker": ["AAPL"], "item": ["none"], "fiscal_year": ["2025"]}


async def test_writer_batches_upserts_and_removes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vs, "UPSERT_BATCH", 2)
    client = FakeIndexClient()
    writer = VertexIndexWriter(CFG, client=client)
    assert await writer.upsert([spec(i) for i in range(5)]) == 5
    assert [len(r.datapoints) for r in client.upserts] == [2, 2, 1]
    assert client.upserts[0].index == "projects/proj/locations/us-central1/indexes/123"
    await writer.remove(["doc:7", "doc:8", "doc:9"])
    assert [list(r.datapoint_ids) for r in client.removes] == [["doc:7", "doc:8"], ["doc:9"]]


def test_filters_become_allow_token_namespaces() -> None:
    ns = to_namespaces(Filters(tickers=("AAPL", "MSFT"), fiscal_years=(2025, 2026), items=("7",)))
    assert {(n.name, tuple(n.allow_tokens)) for n in ns} == {
        ("ticker", ("AAPL", "MSFT")),
        ("item", ("7",)),
        ("fiscal_year", ("2025", "2026")),
    }
    assert to_namespaces(Filters()) == []


async def test_matcher_queries_deployed_index_and_returns_scores() -> None:
    calls: list[dict[str, Any]] = []

    def match(**kwargs: Any) -> list[list[Any]]:
        calls.append(kwargs)
        return [
            [SimpleNamespace(id="doc:1", distance=0.83), SimpleNamespace(id="doc:2", distance=0.71)]
        ]

    matcher = VertexMatcher(CFG, endpoint=SimpleNamespace(match=match))
    hits = await matcher.search([0.0, 1.0], Filters(tickers=("AAPL",)), k=2)
    assert hits == [("doc:1", 0.83), ("doc:2", 0.71)]
    assert calls[0]["deployed_index_id"] == "rag_dev_deployed" and calls[0]["num_neighbors"] == 2
    assert calls[0]["filter"][0].name == "ticker"


def test_matcher_requires_a_deployed_index() -> None:
    with pytest.raises(ValueError, match="not deployed"):
        VertexMatcher(VectorSearchConfig("p", "r", "123"))


def test_config_from_settings() -> None:
    base = {"_env_file": None, "gcp_project_id": "p"}
    assert config_from_settings(Settings(**base)) is None  # type: ignore[arg-type]
    cfg = config_from_settings(
        Settings(**base, vector_search_index_id="123", vector_search_psc_ip="")  # type: ignore[arg-type]
    )
    assert cfg is not None and cfg.index_id == "123" and not cfg.queryable
