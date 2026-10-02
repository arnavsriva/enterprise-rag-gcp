"""Published list prices used for *estimated* cost logging.

Estimates only: real spend comes from Cloud Billing. Re-check when models or prices change.
Source: https://cloud.google.com/vertex-ai/generative-ai/pricing (checked 2026-10-02, USD,
online requests, global endpoint, prompts <= 200K tokens; non-global endpoints are ~10%
higher). Gemini Embedding is billed per input token; older text embedding models per input
character. Gemini "thinking" tokens are billed at the output rate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

PRICES_CHECKED = "2026-10-02"

EMBEDDING_USD_PER_1M_TOKENS: dict[str, float] = {
    "gemini-embedding-001": 0.15,
}

EMBEDDING_USD_PER_1K_CHARS: dict[str, float] = {
    "text-embedding-005": 0.000025,
    "text-multilingual-embedding-002": 0.000025,
}


@dataclass(frozen=True)
class TokenPrice:
    input_per_1m: float
    output_per_1m: float  # also applies to thinking tokens


# Gemini 3.8 Flash has introductory pricing through 2026-12-31.
_PROMO_END = date(2026, 12, 31)
_GENERATION_PROMO: dict[str, TokenPrice] = {
    "gemini-3.8-flash": TokenPrice(0.75, 3.75),
}
GENERATION_PRICES: dict[str, TokenPrice] = {
    "gemini-3.8-flash": TokenPrice(1.50, 7.50),
    "gemini-3.5-flash": TokenPrice(1.50, 9.00),
    "gemini-3.5-flash-lite": TokenPrice(0.30, 2.50),
    "gemini-3.1-pro-preview": TokenPrice(2.00, 12.00),
}


def embedding_cost_usd(model: str, *, tokens: int, chars: int) -> float | None:
    """Estimated cost of embedding inputs, or None if the model's price is unknown."""
    if model in EMBEDDING_USD_PER_1M_TOKENS:
        return tokens / 1_000_000 * EMBEDDING_USD_PER_1M_TOKENS[model]
    if model in EMBEDDING_USD_PER_1K_CHARS:
        return chars / 1_000 * EMBEDDING_USD_PER_1K_CHARS[model]
    return None


def generation_price(model: str, on: date | None = None) -> TokenPrice | None:
    on = on or date.today()
    if model in _GENERATION_PROMO and on <= _PROMO_END:
        return _GENERATION_PROMO[model]
    return GENERATION_PRICES.get(model)


def generation_cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    thinking_tokens: int,
    on: date | None = None,
) -> float | None:
    price = generation_price(model, on)
    if price is None:
        return None
    return (
        input_tokens * price.input_per_1m + (output_tokens + thinking_tokens) * price.output_per_1m
    ) / 1_000_000
