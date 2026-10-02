"""Shared helpers for Vertex AI (google-genai) calls: client construction and retry policy."""

from __future__ import annotations

import logging
from functools import lru_cache

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def is_retryable(exc: BaseException) -> bool:
    """Quota (429), timeouts and server errors are retried; other 4xx client errors are not."""
    if isinstance(exc, genai_errors.APIError):
        return exc.code in RETRYABLE_STATUS
    return isinstance(exc, (TimeoutError, ConnectionError))


def _log_retry(state: RetryCallState) -> None:
    exc = state.outcome.exception() if state.outcome else None
    log.warning(
        "model call retry",
        extra={
            "attempt": state.attempt_number,
            "sleep_s": round(state.next_action.sleep, 2) if state.next_action else None,
            "error": f"{type(exc).__name__}: {exc}"[:300] if exc else None,
        },
    )


def retrying(max_retries: int, max_wait: float = 30.0) -> AsyncRetrying:
    """Exponential backoff with full jitter; logs each retry; re-raises the last error."""
    return AsyncRetrying(
        retry=retry_if_exception(is_retryable),
        stop=stop_after_attempt(max_retries + 1),
        wait=wait_random_exponential(multiplier=1, max=max_wait),
        before_sleep=_log_retry,
        reraise=True,
    )


@lru_cache(maxsize=4)
def client(project: str, location: str) -> genai.Client:
    """One client per (project, location): it holds connection pools, so reuse it.

    The SDK's own retry loop is disabled (attempts=1): it retries 429s silently, which showed
    up as unexplained 10-40 s latencies. All retries go through `retrying()`, which logs them.
    """
    return genai.Client(
        vertexai=True,
        project=project,
        location=location,
        http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)),
    )
