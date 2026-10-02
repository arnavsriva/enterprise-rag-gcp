"""Published list prices used for *estimated* cost logging.

Estimates only: real spend comes from Cloud Billing. Re-check when models or prices change.
Source: https://cloud.google.com/vertex-ai/generative-ai/pricing (checked 2026-10-02, USD,
online requests). Gemini Embedding is billed per input token; the older text embedding
models are billed per input character.
"""

from __future__ import annotations

PRICES_CHECKED = "2026-10-02"

EMBEDDING_USD_PER_1M_TOKENS: dict[str, float] = {
    "gemini-embedding-001": 0.15,
}

EMBEDDING_USD_PER_1K_CHARS: dict[str, float] = {
    "text-embedding-005": 0.000025,
    "text-multilingual-embedding-002": 0.000025,
}


def embedding_cost_usd(model: str, *, tokens: int, chars: int) -> float | None:
    """Estimated cost of embedding inputs, or None if the model's price is unknown."""
    if model in EMBEDDING_USD_PER_1M_TOKENS:
        return tokens / 1_000_000 * EMBEDDING_USD_PER_1M_TOKENS[model]
    if model in EMBEDDING_USD_PER_1K_CHARS:
        return chars / 1_000 * EMBEDDING_USD_PER_1K_CHARS[model]
    return None
