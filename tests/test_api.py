import json
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from google.genai import errors as genai_errors

from api.main import create_app
from common.config import Settings
from rag.agent import AgentAnswer
from rag.factory import Services
from rag.service import QueryOptions, RAGAnswer
from rag.types import RetrievedChunk, Timings, Usage

KNOWN = frozenset({"AAPL", "JPM"})


def chunk(n: int) -> RetrievedChunk:
    return RetrievedChunk(
        f"d:{n}",
        "d",
        "AAPL",
        "Apple Inc.",
        2025,
        date(2025, 9, 27),
        "7",
        "MD&A",
        "x " * 200,
        "https://sec.gov/a",
        0.03,
    )


class FakeService:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.options: QueryOptions | None = None
        self.settings = Settings(_env_file=None)  # type: ignore[call-arg]

    async def answer(self, question: str, options: QueryOptions) -> RAGAnswer:
        self.options = options
        if self.error:
            raise self.error
        return RAGAnswer(
            question=question,
            answer="Net sales were $416,161 million [1].",
            answered=True,
            chunks=[chunk(1), chunk(2)],
            cited=[1],
            invalid_citations=[],
            usage=Usage(
                embedding_tokens=10,
                input_tokens=6000,
                output_tokens=60,
                thinking_tokens=0,
                model_calls=1,
            ),
            timings=Timings(
                {"embed": 900.0, "retrieve": 50.0, "generate": 2500.0, "total": 3450.0}
            ),
            estimated_cost_usd=0.0047,
            generation_model="gemini-3.8-flash",
            embedding_model="gemini-embedding-001",
            backend="pgvector",
            mode="hybrid",
            top_k=6,
            finish_reason="STOP",
        )


class FakeAgent:
    async def run(self, question: str) -> AgentAnswer:
        return AgentAnswer(
            question=question,
            answer="Alphabet spent more [S2].",
            answered=True,
            sources=[chunk(1), chunk(2)],
            cited=[2],
            invalid_citations=[],
            tool_calls=[
                {
                    "tool": "compare_companies",
                    "args": {"ticker_a": "MSFT", "ticker_b": "GOOGL", "topic": "R&D"},
                }
            ],
            usage=Usage(input_tokens=6000, output_tokens=400, model_calls=2),
            timings=Timings({"total": 7000.0}),
            estimated_cost_usd=0.006,
            model="gemini-3.8-flash",
        )


def client_for(service: FakeService) -> TestClient:
    async def factory(_: Settings) -> Services:
        return Services(service=service, agent=FakeAgent(), known_tickers=KNOWN, pool=None)  # type: ignore[arg-type]

    return TestClient(create_app(factory=factory, settings=Settings(_env_file=None)))  # type: ignore[call-arg]


def log_lines(out: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


def test_health() -> None:
    with client_for(FakeService()) as c:
        assert c.get("/health").json() == {"status": "ok", "documents": 0, "chunks": 0}


def test_query_response_and_structured_cost_log(capsys: pytest.CaptureFixture[str]) -> None:
    svc = FakeService()
    with client_for(svc) as c:
        r = c.post(
            "/query",
            json={
                "question": "What were Apple's net sales?",
                "tickers": ["aapl"],
                "items": ["7"],
                "top_k": 4,
            },
            headers={"x-request-id": "req-123"},
        )
    assert r.status_code == 200 and r.headers["x-request-id"] == "req-123"
    body = r.json()
    assert body["answered"] and body["request_id"] == "req-123"
    assert [s["cited"] for s in body["sources"]] == [True, False]
    assert len(body["sources"][0]["excerpt"]) <= 301
    assert body["usage"]["input_tokens"] == 6000 and body["estimated_cost_usd"] == 0.0047
    assert (
        svc.options is not None
        and svc.options.filters.tickers == ("AAPL",)
        and svc.options.top_k == 4
    )

    served = [e for e in log_lines(capsys.readouterr().out) if e["message"] == "request served"]
    assert served, "expected a structured request log line"
    line = served[-1]
    assert line["request_id"] == "req-123" and line["status"] == 200 and line["latency_ms"] >= 0
    assert line["tokens_input_tokens"] == 6000 and line["estimated_cost_usd"] == 0.0047
    assert "question" not in line and line["question_chars"] == len("What were Apple's net sales?")


def test_agent_endpoint() -> None:
    with client_for(FakeService()) as c:
        body = c.post("/agent", json={"question": "Compare R&D at MSFT and GOOGL"}).json()
    assert body["tool_calls"][0]["tool"] == "compare_companies"
    assert [s["cited"] for s in body["sources"]] == [False, True]


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        ({"question": "Sales?", "tickers": ["ZZZZ"]}, "unknown tickers"),
        ({"question": "hi"}, None),
        ({"question": "What were sales?", "top_k": 99}, None),
        ({"question": "What were sales?", "mode": "magic"}, None),
    ],
)
def test_validation_errors(payload: dict[str, Any], detail: str | None) -> None:
    with client_for(FakeService()) as c:
        r = c.post("/query", json=payload)
    assert r.status_code == 422
    if detail:
        assert detail in r.json()["detail"]


def test_upstream_model_error_maps_to_502() -> None:
    err = genai_errors.APIError(503, {"error": {"message": "overloaded"}})
    with client_for(FakeService(error=err)) as c:
        r = c.post("/query", json={"question": "What were sales?"})
    assert r.status_code == 502 and "503" in r.json()["detail"]


def test_unavailable_backend_maps_to_501() -> None:
    with client_for(FakeService(error=NotImplementedError("vertex backend is Phase 4"))) as c:
        r = c.post("/query", json={"question": "What were sales?"})
    assert r.status_code == 501


def test_model_timeout_maps_to_504() -> None:
    with client_for(FakeService(error=TimeoutError())) as c:
        r = c.post("/query", json={"question": "What were sales?"})
    assert r.status_code == 504 and "timed out" in r.json()["detail"]
