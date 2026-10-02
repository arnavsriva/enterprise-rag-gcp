"""RAG service: embed query -> retrieve -> generate, with per-stage timings, usage and cost.

This is the single code path behind the API's /query endpoint, the CLI, the evaluation
pipeline and the benchmark, so all of them measure exactly what users get.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from common.config import RetrievalBackend, Settings
from common.pricing import embedding_cost_usd, generation_cost_usd
from ingest.embed import Embedder
from rag.generate import Generation, Generator
from rag.retrieval import PgSearch, build_retriever, retrieve
from rag.types import Filters, RetrievedChunk, Timings, Usage
from rag.vector_search import VertexMatcher


@dataclass
class QueryOptions:
    filters: Filters = field(default_factory=Filters)
    top_k: int | None = None
    mode: str | None = None  # "vector" | "hybrid"; None = settings default
    backend: RetrievalBackend | None = None


@dataclass
class RAGAnswer:
    question: str
    answer: str
    answered: bool
    chunks: list[RetrievedChunk]
    cited: list[int]
    invalid_citations: list[int]
    usage: Usage
    timings: Timings
    estimated_cost_usd: float | None
    generation_model: str
    embedding_model: str
    backend: str
    mode: str
    top_k: int
    finish_reason: str | None


def estimate_cost(usage: Usage, *, embedding_model: str, generation_model: str) -> float | None:
    embed = embedding_cost_usd(embedding_model, tokens=usage.embedding_tokens, chars=0)
    gen = generation_cost_usd(
        generation_model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        thinking_tokens=usage.thinking_tokens,
    )
    if embed is None or gen is None:
        return None
    return round(embed + gen, 8)


class QueryEncoder:
    """Embeds questions with task type RETRIEVAL_QUERY (documents used RETRIEVAL_DOCUMENT)."""

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder
        self.model = embedder.model

    async def encode(self, text: str) -> tuple[list[float], int]:
        result = await self._embedder.embed_documents([text])
        return result.vectors[0], result.token_counts[0]


class RAGService:
    def __init__(
        self,
        *,
        settings: Settings,
        search: PgSearch,
        encoder: QueryEncoder,
        generator: Generator,
        matcher: VertexMatcher | None = None,
    ) -> None:
        self.settings = settings
        self.search = search
        self.matcher = matcher
        self.encoder = encoder
        self.generator = generator

    async def retrieve(
        self, question: str, options: QueryOptions, usage: Usage, timings: Timings
    ) -> list[RetrievedChunk]:
        s = self.settings
        t = time.perf_counter()
        vector, tokens = await self.encoder.encode(question)
        timings.record("embed", (time.perf_counter() - t) * 1000)
        usage.embedding_tokens += tokens

        t = time.perf_counter()
        retriever = build_retriever(
            backend=options.backend or s.retrieval_backend,
            mode=options.mode or s.retrieval_mode,
            search=self.search,
            filters=options.filters,
            top_k=options.top_k or s.retrieval_top_k,
            candidates=s.retrieval_candidates,
            vector_weight=s.retrieval_vector_weight,
            matcher=self.matcher,
        )
        chunks = await retrieve(retriever, question, vector)
        timings.record("retrieve", (time.perf_counter() - t) * 1000)
        return chunks

    async def answer(self, question: str, options: QueryOptions | None = None) -> RAGAnswer:
        options = options or QueryOptions()
        s = self.settings
        usage, timings = Usage(), Timings()
        start = time.perf_counter()

        chunks = await self.retrieve(question, options, usage, timings)

        t = time.perf_counter()
        gen: Generation = await self.generator.generate(question, chunks)
        timings.record("generate", (time.perf_counter() - t) * 1000)
        usage.add(gen.usage)
        timings.record("total", (time.perf_counter() - start) * 1000)

        return RAGAnswer(
            question=question,
            answer=gen.text,
            answered=gen.answered,
            chunks=chunks,
            cited=gen.cited,
            invalid_citations=gen.invalid_citations,
            usage=usage,
            timings=timings,
            estimated_cost_usd=estimate_cost(
                usage, embedding_model=self.encoder.model, generation_model=self.generator.model
            ),
            generation_model=gen.model,
            embedding_model=self.encoder.model,
            backend=str(options.backend or s.retrieval_backend),
            mode=options.mode or s.retrieval_mode,
            top_k=options.top_k or s.retrieval_top_k,
            finish_reason=gen.finish_reason,
        )
