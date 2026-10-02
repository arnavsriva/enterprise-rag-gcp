"""Application settings, loaded from environment variables (and `.env` for local dev)."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class RetrievalBackend(StrEnum):
    PGVECTOR = "pgvector"
    VERTEX_VECTOR_SEARCH = "vertex_vector_search"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- GCP (optional for purely local work; required once Vertex AI is called)
    gcp_project_id: str | None = None
    gcp_region: str = "us-central1"
    gcs_bucket: str | None = None

    # --- Models
    embedding_model: str = "gemini-embedding-001"
    embedding_dim: int = Field(default=768, gt=0)
    # Gemini 3.x is served from the global endpoint only (404 in us-central1, checked
    # 2026-10-02). Clients needing regional processing: set GENAI_LOCATION=us-central1 and a
    # regional model such as gemini-2.5-flash. Embeddings stay regional (gcp_region).
    genai_location: str = "global"
    generation_model: str = "gemini-3.8-flash"
    generation_thinking_level: Literal["LOW", "MEDIUM", "HIGH"] = "LOW"
    generation_max_output_tokens: int = Field(default=2048, gt=0)
    agent_thinking_budget: int = Field(default=512, ge=0)
    # Per-attempt timeout: a call stuck in the shared global endpoint's queue is abandoned and
    # retried (usually ~2 s) instead of waiting 25 s+. See ADR-0007.
    generation_timeout_s: float = Field(default=15.0, gt=0)
    judge_model: str = "gemini-3.1-pro-preview"

    # --- Retrieval
    retrieval_backend: RetrievalBackend = RetrievalBackend.PGVECTOR
    retrieval_mode: Literal["vector", "hybrid"] = "hybrid"
    retrieval_top_k: int = Field(default=6, gt=0, le=50)
    retrieval_candidates: int = Field(default=30, gt=0, le=200)  # per retriever, before fusion
    # Hybrid fusion weight for the vector retriever (keyword gets 1 - this). See ADR-0006.
    retrieval_vector_weight: float = Field(default=0.7, ge=0.0, le=1.0)

    # --- Vertex AI Vector Search (from terraform outputs)
    vector_search_index_id: str | None = None
    vector_search_index_endpoint_id: str | None = None
    vector_search_deployed_index_id: str | None = None
    vector_search_psc_ip: str | None = None

    # --- Postgres + pgvector
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_database: str = "rag"
    pg_user: str = "rag"
    pg_password: SecretStr = SecretStr("")
    pg_sslmode: Literal["disable", "prefer", "require", "verify-ca", "verify-full"] = "prefer"

    # --- SEC EDGAR (fair-access policy requires "Name email"; limit is 10 requests/s)
    sec_user_agent: str = ""
    sec_requests_per_second: float = Field(default=5.0, gt=0, le=10)

    # --- Ingestion
    data_dir: Path = Path("data")
    ingest_max_concurrency: int = Field(default=8, gt=0, le=64)
    ingest_max_retries: int = Field(default=5, ge=0)
    # Embedding API limits: 250 texts and 20,000 tokens per request, 2,048 tokens per text.
    embed_batch_max_texts: int = Field(default=100, gt=0, le=250)
    embed_batch_max_tokens: int = Field(default=15_000, gt=0, le=20_000)
    embed_max_concurrency: int = Field(default=4, gt=0, le=32)
    # Cloud Run Job: cache raw filings in / upload reports to GCS_BUCKET (local disk is ephemeral)
    ingest_use_gcs: bool = False
    # Also upsert chunks to Vertex AI Vector Search (needs VECTOR_SEARCH_INDEX_ID)
    ingest_vector_search: bool = False

    # --- Logging
    log_level: str = "INFO"

    def pg_dsn(self) -> str:
        """libpq-style DSN. Contains the password: never log the return value."""
        password = self.pg_password.get_secret_value()
        auth = f"{self.pg_user}:{password}" if password else self.pg_user
        return (
            f"postgresql://{auth}@{self.pg_host}:{self.pg_port}/{self.pg_database}"
            f"?sslmode={self.pg_sslmode}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
