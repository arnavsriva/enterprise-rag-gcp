from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from common.pricing import generation_cost_usd
from rag.generate import NOT_FOUND, Generator, build_prompt, is_refusal, parse_citations
from rag.types import RetrievedChunk


def chunk(n: int, ticker: str = "AAPL") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"doc:{n}",
        document_id="doc",
        ticker=ticker,
        company_name="Apple Inc.",
        fiscal_year=2025,
        period_end=date(2025, 9, 27),
        item="7",
        item_title="MD&A",
        content=f"content {n}",
        source_url="https://sec.gov/x",
        score=0.5,
    )


class FakeClient:
    def __init__(self, text: str, thoughts: int = 0) -> None:
        self.configs: list[Any] = []
        self._text, self._thoughts = text, thoughts
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._gen))

    async def _gen(self, *, model: str, contents: str, config: Any) -> Any:
        self.configs.append(config)
        return SimpleNamespace(
            text=self._text,
            usage_metadata=SimpleNamespace(
                prompt_token_count=1000,
                candidates_token_count=50,
                thoughts_token_count=self._thoughts,
            ),
            candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))],
            model_version="gemini-3.8-flash",
        )


def test_prompt_numbers_sources_with_provenance() -> None:
    prompt = build_prompt("What were sales?", [chunk(1), chunk(2)])
    assert (
        "[1] Apple Inc. (AAPL) | 10-K FY2025 (period ended 2025-09-27) | Item 7. MD&A\ncontent 1"
        in prompt
    )
    assert "[2] Apple Inc." in prompt
    assert prompt.rstrip().endswith("Question: What were sales?")


@pytest.mark.parametrize(
    ("text", "valid", "invalid"),
    [
        ("Sales rose [1].", [1], []),
        ("See [1][3] and [2, 3].", [1, 3, 2], []),
        (f"Range [2{chr(0x2013)}4] and [2-3]", [2, 3, 4], []),
        ("Made up [9] and [0].", [], [9, 0]),
        ("No citations at all.", [], []),
    ],
)
def test_parse_citations(text: str, valid: list[int], invalid: list[int]) -> None:
    assert parse_citations(text, n_sources=5) == (valid, invalid)


def test_refusal_detection() -> None:
    assert is_refusal(f"{NOT_FOUND} The filings discuss revenue but not churn.")
    assert not is_refusal("Revenue was $10 million [1].")


async def test_generator_maps_usage_citations_and_config() -> None:
    fake = FakeClient("Net sales were $416,161 million [1]; see also [7].", thoughts=12)
    gen = await Generator(fake, model="gemini-3.8-flash", thinking_level="LOW").generate(
        "q", [chunk(1), chunk(2)]
    )
    assert gen.answered and gen.cited == [1] and gen.invalid_citations == [7]
    assert (gen.usage.input_tokens, gen.usage.output_tokens, gen.usage.thinking_tokens) == (
        1000,
        50,
        12,
    )
    assert gen.finish_reason == "STOP"
    config = fake.configs[0]
    assert config.thinking_config.thinking_level.value == "LOW"
    assert config.temperature is None  # Gemini 3: keep the default temperature
    assert "ONLY the numbered sources" in config.system_instruction


async def test_generator_marks_refusals_unanswered() -> None:
    gen = await Generator(FakeClient(f"{NOT_FOUND} Nothing on churn."), model="m").generate(
        "q", [chunk(1)]
    )
    assert not gen.answered


def test_generation_cost_includes_thinking_and_promo_window() -> None:
    kw: dict[str, Any] = {
        "input_tokens": 1_000_000,
        "output_tokens": 500_000,
        "thinking_tokens": 500_000,
    }
    assert generation_cost_usd("gemini-3.8-flash", on=date(2026, 10, 2), **kw) == pytest.approx(
        0.75 + 3.75
    )
    assert generation_cost_usd("gemini-3.8-flash", on=date(2027, 1, 1), **kw) == pytest.approx(
        1.50 + 7.50
    )
    assert generation_cost_usd("unknown", **kw) is None


async def test_generation_attempt_times_out_and_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    monkeypatch.setattr("common.genai.wait_random_exponential", lambda **_: lambda _rs: 0)
    fake = FakeClient("Answer [1].")
    real = fake._gen
    calls = 0

    async def sometimes_stuck(**kw: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(10)  # stuck in the queue
        return await real(**kw)

    fake.aio.models.generate_content = sometimes_stuck
    gen = await Generator(fake, model="m", timeout_s=0.05).generate("q", [chunk(1)])
    assert gen.answered and calls == 2
