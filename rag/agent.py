"""A small LangChain agent over the RAG service, with two tools:

- search_filings(query, ticker?, fiscal_year?)  -> relevant 10-K excerpts
- compare_companies(ticker_a, ticker_b, topic)   -> excerpts for both companies, side by side

Every excerpt a tool returns gets a stable label [S1], [S2], ... in a per-request registry, so
citations in the final answer map back to exact chunks. Usage (incl. reasoning tokens) is
summed over every model call in the loop, so cost logging covers the whole agent run.
The direct /query path is cheaper and more predictable; the agent is for multi-step questions.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool, ToolException

from rag.generate import NOT_FOUND
from rag.service import QueryOptions, RAGService, estimate_cost
from rag.types import Filters, RetrievedChunk, Timings, Usage

AGENT_PROMPT = f"""You are a financial research assistant for SEC Form 10-K filings of 20 large US
companies. Use the tools to find evidence before answering; never answer from memory.

- Use search_filings for questions about one company or general questions. Pass the ticker
  when the question names a company.
- Use compare_companies when the question compares two companies.
- Cite every factual claim with the source labels returned by the tools, e.g. [S3].
- Report figures exactly as written with units and fiscal year; never guess units.
- If the tools return nothing relevant, answer exactly: "{NOT_FOUND}"
- Be concise: direct answer first, then brief supporting detail."""

# Matches [S3] and grouped forms like [S1, S5].
_SOURCE_GROUP = re.compile(r"\[(S\d+(?:\s*,\s*S\d+)*)\]")


@dataclass
class SourceRegistry:
    chunks: list[RetrievedChunk] = field(default_factory=list)
    _index: dict[str, int] = field(default_factory=dict)

    def label(self, chunk: RetrievedChunk) -> str:
        if chunk.chunk_id not in self._index:
            self.chunks.append(chunk)
            self._index[chunk.chunk_id] = len(self.chunks)
        return f"S{self._index[chunk.chunk_id]}"

    def render(self, chunks: list[RetrievedChunk]) -> str:
        if not chunks:
            return "No matching excerpts found."
        return "\n\n".join(f"[{self.label(c)}] {c.label}\n{c.content}" for c in chunks)


@dataclass
class AgentAnswer:
    question: str
    answer: str
    answered: bool
    sources: list[RetrievedChunk]
    cited: list[int]
    invalid_citations: list[int]
    tool_calls: list[dict[str, Any]]
    usage: Usage
    timings: Timings
    estimated_cost_usd: float | None
    model: str


def message_text(message: AIMessage) -> str:
    """Gemini 3 returns content as blocks (text + thought signatures); keep the text."""
    if isinstance(message.content, str):
        return message.content
    return "".join(
        b.get("text", "")
        for b in message.content
        if isinstance(b, dict) and b.get("type") == "text"
    )


def usage_from_messages(messages: list[Any]) -> Usage:
    """Sum usage over every model call. LangChain's output_tokens *includes* reasoning tokens
    (output_token_details.reasoning), so split them out to avoid double-counting thinking."""
    usage = Usage()
    for m in messages:
        meta = getattr(m, "usage_metadata", None)
        if isinstance(m, AIMessage) and meta:
            reasoning = int((meta.get("output_token_details") or {}).get("reasoning", 0))
            usage.input_tokens += int(meta.get("input_tokens", 0))
            usage.output_tokens += max(0, int(meta.get("output_tokens", 0)) - reasoning)
            usage.thinking_tokens += reasoning
            usage.model_calls += 1
    return usage


def parse_source_citations(text: str, n_sources: int) -> tuple[list[int], list[int]]:
    numbers = (int(n) for group in _SOURCE_GROUP.findall(text) for n in re.findall(r"\d+", group))
    seen = list(dict.fromkeys(numbers))
    return [n for n in seen if 1 <= n <= n_sources], [n for n in seen if not 1 <= n <= n_sources]


def build_tools(
    service: RAGService,
    registry: SourceRegistry,
    usage: Usage,
    timings: Timings,
    *,
    top_k: int,
    known_tickers: frozenset[str],
) -> list[StructuredTool]:

    async def _search(query: str, filters: Filters, k: int) -> list[RetrievedChunk]:
        return await service.retrieve(query, QueryOptions(filters=filters, top_k=k), usage, timings)

    def _ticker(t: str | None) -> tuple[str, ...]:
        if not t:
            return ()
        t = t.strip().upper()
        if t not in known_tickers:
            # Returned to the model as a tool error, so it can correct itself.
            raise ToolException(f"unknown ticker {t}; available: {sorted(known_tickers)}")
        return (t,)

    async def search_filings(
        query: str, ticker: str | None = None, fiscal_year: int | None = None
    ) -> str:
        """Search the 10-K filings. Optionally restrict to one company ticker (e.g. AAPL) and/or fiscal year."""
        filters = Filters(
            tickers=_ticker(ticker), fiscal_years=(fiscal_year,) if fiscal_year else ()
        )
        return registry.render(await _search(query, filters, top_k))

    async def compare_companies(ticker_a: str, ticker_b: str, topic: str) -> str:
        """Retrieve excerpts on the same topic from two companies' 10-Ks for side-by-side comparison."""
        per = max(3, top_k // 2 + 1)
        a = await _search(topic, Filters(tickers=_ticker(ticker_a)), per)
        b = await _search(topic, Filters(tickers=_ticker(ticker_b)), per)
        return (
            f"=== {ticker_a.upper()} ===\n{registry.render(a)}\n\n"
            f"=== {ticker_b.upper()} ===\n{registry.render(b)}"
        )

    return [
        StructuredTool.from_function(
            coroutine=search_filings, name="search_filings", handle_tool_error=True
        ),
        StructuredTool.from_function(
            coroutine=compare_companies, name="compare_companies", handle_tool_error=True
        ),
    ]


class FilingsAgent:
    def __init__(
        self,
        service: RAGService,
        model: Any,
        *,
        model_name: str,
        known_tickers: frozenset[str],
        top_k: int = 6,
    ) -> None:
        self._service = service
        self._known_tickers = known_tickers
        self._model = model  # a LangChain chat model with tool calling (ChatVertexAI in prod)
        self.model_name = model_name
        self._top_k = top_k

    async def run(self, question: str, max_steps: int = 6) -> AgentAnswer:
        registry, usage, timings = SourceRegistry(), Usage(), Timings()
        start = time.perf_counter()
        tools = build_tools(
            self._service,
            registry,
            usage,
            timings,
            top_k=self._top_k,
            known_tickers=self._known_tickers,
        )
        agent = create_agent(self._model, tools, system_prompt=AGENT_PROMPT)
        result = await agent.ainvoke(
            {"messages": [HumanMessage(question)]},
            config={"recursion_limit": 2 * max_steps + 1},
        )
        messages = result["messages"]
        usage.add(usage_from_messages(messages))
        final = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        text = message_text(final).strip() if final else ""
        cited, invalid = parse_source_citations(text, len(registry.chunks))
        tool_calls = [
            {"tool": c["name"], "args": c["args"]}
            for m in messages
            if isinstance(m, AIMessage)
            for c in (m.tool_calls or [])
        ]
        timings.record("total", (time.perf_counter() - start) * 1000)
        return AgentAnswer(
            question=question,
            answer=text,
            answered=bool(text) and not text.startswith(NOT_FOUND),
            sources=registry.chunks,
            cited=cited,
            invalid_citations=invalid,
            tool_calls=tool_calls,
            usage=usage,
            timings=timings,
            estimated_cost_usd=estimate_cost(
                usage, embedding_model=self._service.encoder.model, generation_model=self.model_name
            ),
            model=self.model_name,
        )
