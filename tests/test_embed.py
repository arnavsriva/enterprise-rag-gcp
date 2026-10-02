import math
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import errors as genai_errors

from common.pricing import embedding_cost_usd
from ingest.embed import FakeEmbedder, VertexEmbedder, l2_normalise, make_batches


class FakeGenaiClient:
    """Mimics client.aio.models.embed_content; returns un-normalised 4-d vectors."""

    def __init__(self, fail_first: int = 0, status: int = 429) -> None:
        self.calls: list[int] = []
        self._fail_first = fail_first
        self._status = status
        self.aio = SimpleNamespace(models=SimpleNamespace(embed_content=self._embed))

    async def _embed(self, *, model: str, contents: list[str], config: Any) -> Any:
        self.calls.append(len(contents))
        if len(self.calls) <= self._fail_first:
            raise genai_errors.APIError(self._status, {"error": {"message": "quota"}})
        assert config.output_dimensionality == 4
        return SimpleNamespace(
            embeddings=[
                SimpleNamespace(
                    values=[3.0, 4.0, 0.0, 0.0],
                    statistics=SimpleNamespace(token_count=len(t.split()), truncated=False),
                )
                for t in contents
            ],
            metadata=SimpleNamespace(billable_character_count=sum(len(t) for t in contents)),
        )


def embedder(fake: FakeGenaiClient, **kw: Any) -> VertexEmbedder:
    return VertexEmbedder(
        project="p", location="l", model="gemini-embedding-001", dim=4, client=fake, **kw
    )


def test_l2_normalise() -> None:
    assert l2_normalise([3.0, 4.0]) == [0.6, 0.8]
    with pytest.raises(ValueError):
        l2_normalise([0.0, 0.0])


def test_batches_respect_text_and_token_limits() -> None:
    texts = ["1" * 100] * 10  # exactly 100 estimated tokens each (digits count singly)
    assert [len(b) for b in make_batches(texts, max_texts=4, max_tokens=10_000)] == [4, 4, 2]
    assert [len(b) for b in make_batches(texts, max_texts=100, max_tokens=250)] == [2, 2, 2, 2, 2]
    assert [i for b in make_batches(texts, 3, 1000) for i in b] == list(range(10))


async def test_vectors_normalised_tokens_counted_order_kept() -> None:
    fake = FakeGenaiClient()
    result = await embedder(fake, max_texts=2).embed_documents(["a b", "c", "d e f"])
    assert fake.calls == [2, 1]
    assert result.token_counts == [2, 1, 3]
    assert result.requests == 2
    assert all(math.isclose(sum(x * x for x in v), 1.0) for v in result.vectors)


async def test_retries_retryable_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ingest.embed.wait_random_exponential", lambda **_: lambda _rs: 0)
    fake = FakeGenaiClient(fail_first=2, status=429)
    result = await embedder(fake).embed_documents(["a"])
    assert len(fake.calls) == 3
    assert result.retries == 2


async def test_does_not_retry_client_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ingest.embed.wait_random_exponential", lambda **_: lambda _rs: 0)
    fake = FakeGenaiClient(fail_first=1, status=400)
    with pytest.raises(genai_errors.APIError):
        await embedder(fake).embed_documents(["a"])
    assert len(fake.calls) == 1


async def test_fake_embedder_is_deterministic_and_unit_length() -> None:
    fe = FakeEmbedder(dim=8)
    a, b = (
        (await fe.embed_documents(["same text"])).vectors[0],
        (await fe.embed_documents(["same text"])).vectors[0],
    )
    assert a == b and math.isclose(sum(x * x for x in a), 1.0)


def test_cost_estimates_by_billing_unit() -> None:
    assert embedding_cost_usd("gemini-embedding-001", tokens=1_000_000, chars=0) == pytest.approx(
        0.15
    )
    assert embedding_cost_usd("text-embedding-005", tokens=0, chars=1_000_000) == pytest.approx(
        0.025
    )
    assert embedding_cost_usd("unknown-model", tokens=1, chars=1) is None
