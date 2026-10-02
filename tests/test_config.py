from typing import Any

import pytest
from pydantic import ValidationError

from common.config import RetrievalBackend, Settings


def make(**overrides: Any) -> Settings:
    # _env_file=None: tests must not depend on a developer's local .env
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_defaults_are_local_and_safe() -> None:
    s = make()
    assert s.retrieval_backend is RetrievalBackend.PGVECTOR
    assert s.gcp_region == "us-central1"
    assert s.gcp_project_id is None


def test_backend_parsed_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETRIEVAL_BACKEND", "vertex_vector_search")
    assert make().retrieval_backend is RetrievalBackend.VERTEX_VECTOR_SEARCH


def test_invalid_backend_rejected() -> None:
    with pytest.raises(ValidationError):
        make(retrieval_backend="faiss")


def test_dsn_includes_password_but_repr_does_not() -> None:
    s = make(pg_password="s3cret", pg_sslmode="require")
    assert s.pg_dsn() == "postgresql://rag:s3cret@localhost:5432/rag?sslmode=require"
    assert "s3cret" not in repr(s)


def test_dsn_without_password() -> None:
    assert make().pg_dsn().startswith("postgresql://rag@localhost")
