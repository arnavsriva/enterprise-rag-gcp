from collections.abc import Iterator, Sequence
from datetime import date
from typing import Any, cast

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage

from rag.agent import (
    FilingsAgent,
    SourceRegistry,
    build_tools,
    message_text,
    parse_source_citations,
    usage_from_messages,
)
from rag.service import QueryOptions, RAGService
from rag.types import RetrievedChunk, Timings, Usage

KNOWN = frozenset({"AAPL", "MSFT"})


def chunk(cid: str, ticker: str) -> RetrievedChunk:
    return RetrievedChunk(
        cid,
        "doc",
        ticker,
        f"{ticker} Inc",
        2025,
        date(2025, 12, 31),
        "7",
        "MD&A",
        f"text {cid}",
        "u",
        0.4,
    )


class FakeService:
    """Stands in for RAGService: returns two chunks per ticker filter."""

    def __init__(self) -> None:
        self.calls: list[QueryOptions] = []
        self.encoder = type("E", (), {"model": "gemini-embedding-001"})()

    async def retrieve(
        self, question: str, options: QueryOptions, usage: Usage, timings: Timings
    ) -> list[RetrievedChunk]:
        self.calls.append(options)
        usage.embedding_tokens += 5
        t = options.filters.tickers[0] if options.filters.tickers else "AAPL"
        return [chunk(f"{t}:1", t), chunk(f"{t}:2", t)]


class ToolCallingFake(GenericFakeChatModel):
    """GenericFakeChatModel plus bind_tools, so create_agent accepts it."""

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ToolCallingFake":
        return self


def test_registry_labels_are_stable_and_deduplicated() -> None:
    reg = SourceRegistry()
    a, b = chunk("x:1", "AAPL"), chunk("x:2", "AAPL")
    assert [reg.label(a), reg.label(b), reg.label(a)] == ["S1", "S2", "S1"]
    assert len(reg.chunks) == 2


def test_source_citations_single_and_grouped() -> None:
    assert parse_source_citations("A [S1, S3] and [S2][S9] [S1]", n_sources=3) == ([1, 3, 2], [9])


def test_usage_splits_reasoning_out_of_output_tokens() -> None:
    msgs = [
        AIMessage(
            "",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 75,
                "total_tokens": 175,
                "output_token_details": {"reasoning": 61},
            },
        ),
        ToolMessage("result", tool_call_id="1"),
        AIMessage(
            "done", usage_metadata={"input_tokens": 200, "output_tokens": 20, "total_tokens": 220}
        ),
    ]
    u = usage_from_messages(msgs)
    assert (u.input_tokens, u.output_tokens, u.thinking_tokens, u.model_calls) == (300, 34, 61, 2)


def test_message_text_from_content_blocks() -> None:
    blocks: list[str | dict[Any, Any]] = [
        {"type": "text", "text": "Answer "},
        {"type": "thinking", "x": 1},
        {"type": "text", "text": "[S1]"},
    ]
    assert message_text(AIMessage(blocks)) == "Answer [S1]"


async def test_tools_filter_by_ticker_and_report_unknown_tickers() -> None:
    svc, reg = FakeService(), SourceRegistry()
    tools = {
        t.name: t
        for t in build_tools(
            cast(RAGService, svc), reg, Usage(), Timings(), top_k=4, known_tickers=KNOWN
        )
    }

    out = await tools["compare_companies"].ainvoke(
        {"ticker_a": "aapl", "ticker_b": "MSFT", "topic": "R&D"}
    )
    assert "=== AAPL ===" in out and "=== MSFT ===" in out and "[S3]" in out
    assert [o.filters.tickers for o in svc.calls] == [("AAPL",), ("MSFT",)]

    err = await tools["search_filings"].ainvoke({"query": "x", "ticker": "ZZZZ"})
    assert "unknown ticker ZZZZ" in err  # returned to the model, not raised


async def test_agent_loop_end_to_end_with_scripted_model() -> None:
    script: Iterator[AIMessage] = iter(
        [
            AIMessage(
                "",
                tool_calls=[
                    {
                        "name": "search_filings",
                        "args": {"query": "net sales", "ticker": "AAPL"},
                        "id": "c1",
                    }
                ],
                usage_metadata={"input_tokens": 50, "output_tokens": 10, "total_tokens": 60},
            ),
            AIMessage(
                [{"type": "text", "text": "Net sales were $10 [S1]."}],
                usage_metadata={"input_tokens": 400, "output_tokens": 30, "total_tokens": 430},
            ),
        ]
    )
    agent = FilingsAgent(
        cast(RAGService, FakeService()),
        ToolCallingFake(messages=script),
        model_name="gemini-3.8-flash",
        known_tickers=KNOWN,
    )
    result = await agent.run("What were Apple's net sales?")
    assert result.answer == "Net sales were $10 [S1]." and result.answered
    assert result.cited == [1] and result.sources[0].chunk_id == "AAPL:1"
    assert result.tool_calls == [
        {"tool": "search_filings", "args": {"query": "net sales", "ticker": "AAPL"}}
    ]
    assert (result.usage.input_tokens, result.usage.embedding_tokens, result.usage.model_calls) == (
        450,
        5,
        2,
    )
    assert result.estimated_cost_usd is not None and result.estimated_cost_usd > 0
