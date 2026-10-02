"""Application settings, loaded from environment variables (and `.env` for local dev)."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
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
    embedding_model: str = "text-embedding-005"
    embedding_dim: int = Field(default=768, gt=0)
    generation_model: str = "gemini-2.5-flash"
    judge_model: str = "gemini-2.5-pro"

    # --- Retrieval
    retrieval_backend: RetrievalBackend = RetrievalBackend.PGVECTOR
    retrieval_top_k: int = Field(default=5, gt=0, le=50)

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

    # --- SEC EDGAR (fair-access policy requires "Name email")
    sec_user_agent: str = ""

    # --- Ingestion
    ingest_max_concurrency: int = Field(default=8, gt=0, le=64)
    ingest_max_retries: int = Field(default=5, ge=0)

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
