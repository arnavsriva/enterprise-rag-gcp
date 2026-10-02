"""Retrieval: pgvector (vector + Postgres full-text) behind LlamaIndex retrievers.

- `PgSearch` runs the SQL. Metadata filters are applied *inside* the query, and pgvector's
  iterative HNSW scan is enabled so filtered vector queries still return k rows.
- `PgVectorRetriever` / `PgKeywordRetriever` adapt it to LlamaIndex's BaseRetriever.
- `build_retriever` returns a single retriever for the configured mode: "vector", or "hybrid"
  = both fused by LlamaIndex's QueryFusionRetriever using *weighted relative-score* fusion
  (min-max normalised scores, vector 0.7 / keyword 0.3 by default). Equal-weight reciprocal
  rank fusion was tried first and demoted correct vector hits when keyword search matched
  the wrong vocabulary ("employees" vs the filing's "associates"); see ADR-0006.
- The Vertex AI Vector Search backend plugs in here (Phase 4); it is only reachable from
  inside the VPC (ADR-0002).
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg
from llama_index.core.llms import MockLLM
from llama_index.core.retrievers import BaseRetriever, QueryFusionRetriever
from llama_index.core.retrievers.fusion_retriever import FUSION_MODES
from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode
from pgvector.asyncpg import register_vector

from common.config import RetrievalBackend
from rag.types import Filters, RetrievedChunk

log = logging.getLogger(__name__)

_COLUMNS = """
    c.id, c.document_id, d.ticker, d.company_name, d.fiscal_year, d.period_end,
    c.item, c.item_title, c.content, d.source_url
"""


async def _init_connection(conn: asyncpg.Connection) -> None:
    await register_vector(conn)
    # Higher ef_search = better recall for a few extra ms. Iterative scans (pgvector >= 0.8)
    # keep scanning the HNSW graph until enough rows pass the WHERE filters.
    await conn.execute("SET hnsw.ef_search = 100")
    try:
        await conn.execute("SET hnsw.iterative_scan = relaxed_order")
    except asyncpg.PostgresError:
        log.warning("pgvector < 0.8: filtered vector queries may return fewer than k rows")


async def create_retrieval_pool(dsn: str, **kwargs: Any) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, init=_init_connection, min_size=1, max_size=10, **kwargs)


def _where(filters: Filters, first_param: int) -> tuple[str, list[Any]]:
    clauses, args = [], []
    for column, values in (
        ("d.ticker", filters.tickers),
        ("d.fiscal_year", filters.fiscal_years),
        ("c.item", filters.items),
    ):
        if values:
            args.append(list(values))
            clauses.append(f"{column} = ANY(${first_param + len(args) - 1})")
    return (" AND ".join(clauses) or "TRUE"), args


def _row_to_chunk(row: asyncpg.Record) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=row["id"],
        document_id=row["document_id"],
        ticker=row["ticker"],
        company_name=row["company_name"],
        fiscal_year=row["fiscal_year"],
        period_end=row["period_end"],
        item=row["item"],
        item_title=row["item_title"],
        content=row["content"],
        source_url=row["source_url"],
        score=float(row["score"]),
    )


class PgSearch:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def vector(
        self, embedding: list[float], filters: Filters, k: int
    ) -> list[RetrievedChunk]:
        where, args = _where(filters, first_param=3)
        rows = await self._pool.fetch(
            f"""
            SELECT {_COLUMNS}, 1 - (c.embedding <=> $1) AS score
            FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE {where}
            ORDER BY c.embedding <=> $1
            LIMIT $2
            """,  # noqa: S608 - only placeholders are interpolated, values are bound
            embedding,
            k,
            *args,
        )
        return [_row_to_chunk(r) for r in rows]

    async def keyword(self, query: str, filters: Filters, k: int) -> list[RetrievedChunk]:
        """Full-text search with OR semantics (any query term may match), ranked by cover density.

        AND semantics would require e.g. the company name to appear in the chunk body, which it
        usually doesn't; company scoping is done with filters instead.
        """
        where, args = _where(filters, first_param=3)
        rows = await self._pool.fetch(
            f"""
            WITH q AS (
                SELECT NULLIF(replace(plainto_tsquery('english', $1)::text, '&', '|'), '')::tsquery AS tsq
            )
            SELECT {_COLUMNS}, ts_rank_cd(c.tsv, q.tsq) AS score
            FROM chunks c JOIN documents d ON d.id = c.document_id, q
            WHERE q.tsq IS NOT NULL AND c.tsv @@ q.tsq AND {where}
            ORDER BY score DESC
            LIMIT $2
            """,  # noqa: S608 - only placeholders are interpolated, values are bound
            query,
            k,
            *args,
        )
        return [_row_to_chunk(r) for r in rows]


# ---------------------------------------------------------------- LlamaIndex adapters


def to_node(chunk: RetrievedChunk) -> NodeWithScore:
    # Metadata must be identical whichever retriever produced the node: fusion de-duplicates on
    # node.hash, which covers text + metadata. Retriever-specific scores go in .score only.
    node = TextNode(
        id_=chunk.chunk_id,
        text=chunk.content,
        metadata={
            "document_id": chunk.document_id,
            "ticker": chunk.ticker,
            "company_name": chunk.company_name,
            "fiscal_year": chunk.fiscal_year,
            "period_end": chunk.period_end.isoformat() if chunk.period_end else None,
            "item": chunk.item,
            "item_title": chunk.item_title,
            "source_url": chunk.source_url,
        },
    )
    return NodeWithScore(node=node, score=chunk.score)


def from_node(nws: NodeWithScore) -> RetrievedChunk:
    from datetime import date

    m = nws.node.metadata
    return RetrievedChunk(
        chunk_id=nws.node.node_id,
        document_id=m["document_id"],
        ticker=m["ticker"],
        company_name=m["company_name"],
        fiscal_year=m["fiscal_year"],
        period_end=date.fromisoformat(m["period_end"]) if m.get("period_end") else None,
        item=m.get("item"),
        item_title=m.get("item_title"),
        content=nws.node.get_content(),
        source_url=m["source_url"],
        score=float(nws.score or 0.0),
    )


class _AsyncOnlyRetriever(BaseRetriever):
    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        raise NotImplementedError("use aretrieve(); this retriever is async-only")


class PgVectorRetriever(_AsyncOnlyRetriever):
    """Expects the query embedding precomputed on the QueryBundle (embedded once per request)."""

    def __init__(self, search: PgSearch, filters: Filters, k: int) -> None:
        super().__init__()
        self._search, self._filters, self._k = search, filters, k

    async def _aretrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        if query_bundle.embedding is None:
            raise ValueError("QueryBundle.embedding is required")
        rows = await self._search.vector(list(query_bundle.embedding), self._filters, self._k)
        return [to_node(r) for r in rows]


class PgKeywordRetriever(_AsyncOnlyRetriever):
    def __init__(self, search: PgSearch, filters: Filters, k: int) -> None:
        super().__init__()
        self._search, self._filters, self._k = search, filters, k

    async def _aretrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        rows = await self._search.keyword(query_bundle.query_str, self._filters, self._k)
        return [to_node(r) for r in rows]


def build_retriever(
    *,
    backend: RetrievalBackend,
    mode: str,
    search: PgSearch,
    filters: Filters,
    top_k: int,
    candidates: int,
    vector_weight: float = 0.7,
) -> BaseRetriever:
    if backend is RetrievalBackend.VERTEX_VECTOR_SEARCH:
        raise NotImplementedError(
            "vertex_vector_search backend is added in Phase 4 (only reachable inside the VPC)"
        )
    if mode == "vector":
        return PgVectorRetriever(search, filters, top_k)
    return QueryFusionRetriever(
        [
            PgVectorRetriever(search, filters, candidates),
            PgKeywordRetriever(search, filters, candidates),
        ],
        llm=MockLLM(),  # never called: num_queries=1 disables LLM query rewriting
        mode=FUSION_MODES.RELATIVE_SCORE,
        retriever_weights=[vector_weight, 1.0 - vector_weight],
        similarity_top_k=top_k,
        num_queries=1,
        use_async=True,
    )


async def retrieve(
    retriever: BaseRetriever, question: str, embedding: list[float]
) -> list[RetrievedChunk]:
    nodes = await retriever.aretrieve(QueryBundle(query_str=question, embedding=embedding))
    return [from_node(n) for n in nodes]
