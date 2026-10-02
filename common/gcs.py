"""Minimal async wrappers around Cloud Storage (the client library is blocking)."""

from __future__ import annotations

import asyncio
from functools import lru_cache

from google.cloud import storage  # type: ignore[attr-defined]  # no type stubs


@lru_cache(maxsize=1)
def _client() -> storage.Client:
    return storage.Client()


async def download_if_exists(bucket: str, name: str) -> bytes | None:
    def run() -> bytes | None:
        blob = _client().bucket(bucket).blob(name)
        return blob.download_as_bytes() if blob.exists() else None

    return await asyncio.to_thread(run)


async def upload(bucket: str, name: str, data: bytes, content_type: str) -> str:
    def run() -> None:
        _client().bucket(bucket).blob(name).upload_from_string(data, content_type=content_type)

    await asyncio.to_thread(run)
    return f"gs://{bucket}/{name}"
