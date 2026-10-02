"""Postgres + pgvector sink for documents and chunks.

Writes are idempotent per document: one transaction upserts the document row and replaces
all of its chunks, so a crash mid-document never leaves a half-ingested filing.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import asyncpg
from pgvector.asyncpg import register_vector

from ingest.chunk import Chunk
from ingest.corpus import CorpusEntry
from rag.vector_search import DatapointSpec


@dataclass(frozen=True)
class IngestState:
    content_sha256: str
    chunker_version: str | None
    embedding_model: str | None
    chunk_count: int | None = None
    vector_search_index: str | None = None


async def create_pool(dsn: str, **kwargs: Any) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, init=register_vector, min_size=1, max_size=4, **kwargs)


class PgVectorStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get_state(self, document_id: str) -> IngestState | None:
        row = await self._pool.fetchrow(
            """
            SELECT content_sha256, chunker_version, embedding_model, chunk_count, vector_search_index
            FROM documents WHERE id = $1
            """,
            document_id,
        )
        return IngestState(*row) if row else None

    async def load_datapoints(self, document_id: str) -> list[DatapointSpec]:
        """Stored vectors + metadata for one document, to sync Vector Search without re-embedding."""
        rows = await self._pool.fetch(
            """
            SELECT c.id, c.embedding, d.ticker, c.item, d.fiscal_year
            FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE c.document_id = $1 ORDER BY c.chunk_index
            """,
            document_id,
        )
        return [
            DatapointSpec(
                r["id"],
                [float(x) for x in r["embedding"].to_list()],  # pgvector codec returns Vector
                r["ticker"],
                r["item"],
                r["fiscal_year"],
            )
            for r in rows
        ]

    async def mark_synced(self, document_id: str, index_id: str | None) -> None:
        await self._pool.execute(
            "UPDATE documents SET vector_search_index = $2 WHERE id = $1", document_id, index_id
        )

    async def replace_document(
        self,
        entry: CorpusEntry,
        *,
        content_sha256: str,
        chunker_version: str,
        embedding_model: str,
        chunks: Sequence[Chunk],
        vectors: Sequence[list[float]],
        token_counts: Sequence[int],
    ) -> None:
        if not (len(chunks) == len(vectors) == len(token_counts)):
            raise ValueError("chunks, vectors and token_counts must have equal length")
        records = [
            (
                c.id,
                entry.document_id,
                c.chunk_index,
                c.item,
                c.item_title,
                c.content,
                tokens,
                vector,
                json.dumps({"ticker": entry.ticker, "fiscal_year": entry.fiscal_year}),
            )
            for c, vector, tokens in zip(chunks, vectors, token_counts, strict=True)
        ]
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO documents (id, ticker, cik, company_name, form_type, fiscal_year,
                    period_end, filed_at, source_url, content_sha256, chunker_version,
                    embedding_model, chunk_count, ingested_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, now())
                ON CONFLICT (id) DO UPDATE SET
                    ticker = EXCLUDED.ticker, cik = EXCLUDED.cik,
                    company_name = EXCLUDED.company_name, form_type = EXCLUDED.form_type,
                    fiscal_year = EXCLUDED.fiscal_year, period_end = EXCLUDED.period_end,
                    filed_at = EXCLUDED.filed_at, source_url = EXCLUDED.source_url,
                    content_sha256 = EXCLUDED.content_sha256,
                    chunker_version = EXCLUDED.chunker_version,
                    embedding_model = EXCLUDED.embedding_model,
                    chunk_count = EXCLUDED.chunk_count, ingested_at = now(),
                    vector_search_index = NULL
                """,
                entry.document_id,
                entry.ticker,
                str(entry.cik),
                entry.company_name,
                entry.form,
                entry.fiscal_year,
                entry.period_end,
                entry.filed,
                entry.url,
                content_sha256,
                chunker_version,
                embedding_model,
                len(chunks),
            )
            await conn.execute("DELETE FROM chunks WHERE document_id = $1", entry.document_id)
            await conn.executemany(
                """
                INSERT INTO chunks (id, document_id, chunk_index, item, item_title, content,
                    token_count, embedding, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                """,
                records,
            )
