"""Wires real dependencies (Postgres pool, Vertex AI clients) into the RAG service and agent.

Shared by the API, the CLI, the evaluation pipeline and the benchmark.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg
from langchain_google_genai import ChatGoogleGenerativeAI

from common import genai as genai_helpers
from common.config import Settings
from ingest.corpus import load_corpus
from ingest.embed import VertexEmbedder
from rag.agent import FilingsAgent
from rag.generate import Generator
from rag.retrieval import PgSearch, create_retrieval_pool
from rag.service import QueryEncoder, RAGService


@dataclass
class Services:
    service: RAGService
    agent: FilingsAgent
    known_tickers: frozenset[str]
    pool: asyncpg.Pool | None = None

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()


async def build_services(s: Settings) -> Services:
    if not s.gcp_project_id:
        raise RuntimeError("GCP_PROJECT_ID is not set")
    known = frozenset(e.ticker for e in load_corpus())
    pool = await create_retrieval_pool(s.pg_dsn())
    encoder = QueryEncoder(
        VertexEmbedder(
            project=s.gcp_project_id,
            location=s.gcp_region,
            model=s.embedding_model,
            dim=s.embedding_dim,
            task_type="RETRIEVAL_QUERY",
            max_retries=3,
        )
    )
    generator = Generator(
        genai_helpers.client(s.gcp_project_id, s.genai_location),
        model=s.generation_model,
        thinking_level=s.generation_thinking_level,
        max_output_tokens=s.generation_max_output_tokens,
    )
    service = RAGService(settings=s, search=PgSearch(pool), encoder=encoder, generator=generator)
    chat = ChatGoogleGenerativeAI(
        model=s.generation_model,
        vertexai=True,
        project=s.gcp_project_id,
        location=s.genai_location,
        thinking_budget=s.agent_thinking_budget,
        max_retries=3,
    )
    agent = FilingsAgent(
        service, chat, model_name=s.generation_model, known_tickers=known, top_k=s.retrieval_top_k
    )
    return Services(service=service, agent=agent, known_tickers=known, pool=pool)
