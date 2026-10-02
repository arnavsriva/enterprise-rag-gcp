"""Embeddings via the Vertex AI (Gemini Enterprise Agent Platform) API, using google-genai.

- Batches respect the API limits (texts and tokens per request).
- Bounded concurrency + retries with exponential backoff and jitter on 429/5xx.
- Vectors are L2-normalised here: gemini-embedding-001 only returns unit vectors at its full
  3072 dimensions, and truncated 768-d outputs are not (measured norm ~0.59). Normalising
  keeps cosine (pgvector) and dot product (Vector Search) rankings identical.
- Token counts come from the API's per-embedding statistics, so cost logs use real numbers.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from google.genai import types

from common import genai as genai_helpers
from ingest.chunk import estimate_tokens

log = logging.getLogger(__name__)


@dataclass
class EmbedResult:
    vectors: list[list[float]]
    token_counts: list[int]
    billable_chars: int = 0
    truncated: int = 0
    requests: int = 0
    retries: int = field(default=0)


class Embedder(Protocol):
    model: str
    dim: int

    async def embed_documents(self, texts: list[str]) -> EmbedResult: ...


def l2_normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0:
        raise ValueError("cannot normalise a zero vector")
    return [x / norm for x in vector]


def make_batches(texts: list[str], max_texts: int, max_tokens: int) -> list[list[int]]:
    """Group text indices so each batch stays within both request limits (by estimate)."""
    batches: list[list[int]] = []
    current: list[int] = []
    tokens = 0
    for i, text in enumerate(texts):
        t = estimate_tokens(text)
        if current and (len(current) >= max_texts or tokens + t > max_tokens):
            batches.append(current)
            current, tokens = [], 0
        current.append(i)
        tokens += t
    if current:
        batches.append(current)
    return batches


class VertexEmbedder:
    def __init__(
        self,
        *,
        project: str,
        location: str,
        model: str,
        dim: int,
        max_texts: int = 100,
        max_tokens: int = 15_000,
        max_concurrency: int = 4,
        max_retries: int = 5,
        task_type: str = "RETRIEVAL_DOCUMENT",
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.dim = dim
        self._client = client or genai_helpers.client(project, location)
        self._max_texts = max_texts
        self._max_tokens = max_tokens
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_retries = max_retries
        self._task_type = task_type

    async def _embed_batch(
        self, texts: list[str]
    ) -> tuple[list[list[float]], list[int], int, int, int]:
        attempts = 0
        async with self._semaphore:
            async for attempt in genai_helpers.retrying(self._max_retries, max_wait=60):
                with attempt:
                    attempts += 1
                    response = await self._client.aio.models.embed_content(
                        model=self.model,
                        contents=cast(Any, texts),  # list[str] is valid; stubs are narrower
                        config=types.EmbedContentConfig(
                            task_type=self._task_type,
                            output_dimensionality=self.dim,
                            auto_truncate=False,  # fail loudly rather than silently cut text
                        ),
                    )
        embeddings = response.embeddings or []
        if len(embeddings) != len(texts):
            raise RuntimeError(f"expected {len(texts)} embeddings, got {len(embeddings)}")
        vectors, tokens, truncated = [], [], 0
        for e in embeddings:
            values = list(e.values or [])
            if len(values) != self.dim:
                raise RuntimeError(f"expected dim {self.dim}, got {len(values)}")
            vectors.append(l2_normalise(values))
            stats = e.statistics
            tokens.append(int(stats.token_count or 0) if stats else 0)
            truncated += int(bool(stats and stats.truncated))
        chars = int(getattr(response.metadata, "billable_character_count", 0) or 0)
        return vectors, tokens, chars, truncated, attempts - 1

    async def embed_documents(self, texts: list[str]) -> EmbedResult:
        batches = make_batches(texts, self._max_texts, self._max_tokens)
        results = await asyncio.gather(*(self._embed_batch([texts[i] for i in b]) for b in batches))
        out = EmbedResult(vectors=[], token_counts=[], requests=len(batches))
        for vectors, tokens, chars, truncated, retries in results:
            out.vectors += vectors
            out.token_counts += tokens
            out.billable_chars += chars
            out.truncated += truncated
            out.retries += retries
        return out


class FakeEmbedder:
    """Deterministic, offline embedder for tests and local plumbing checks (no API calls)."""

    def __init__(self, dim: int = 768, model: str = "fake-embedding") -> None:
        self.model = model
        self.dim = dim

    async def embed_documents(self, texts: list[str]) -> EmbedResult:
        vectors = []
        for text in texts:
            seed = hashlib.sha256(text.encode()).digest()
            raw = [((seed[i % len(seed)] + i) % 255) - 127.0 for i in range(self.dim)]
            vectors.append(l2_normalise(raw))
        return EmbedResult(
            vectors=vectors,
            token_counts=[estimate_tokens(t) for t in texts],
            billable_chars=sum(len(t) for t in texts),
            requests=1,
        )
