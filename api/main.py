"""FastAPI service (Cloud Run): grounded Q&A over the 10-K corpus.

Endpoints: POST /query (retrieve -> generate), POST /agent (tool-using agent), GET /health.
Every request emits one structured log line with latency, tokens and estimated cost, which
Cloud Logging indexes (jsonPayload.*) for dashboards and log-based metrics. Question text is
not logged (only its length and a hash): user prompts can contain sensitive information.

Run locally: make serve   (uvicorn api.main:app --port 8080)
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from google.genai import errors as genai_errors

from api.schemas import (
    AgentRequest,
    AgentResponse,
    Health,
    QueryRequest,
    QueryResponse,
    Source,
    UsageOut,
)
from common.config import Settings, get_settings
from common.logging import configure_logging
from rag.factory import Services, build_services
from rag.service import QueryOptions
from rag.types import Filters

log = logging.getLogger("api")

ServicesFactory = Callable[[Settings], Awaitable[Services]]


def _question_fields(question: str) -> dict[str, Any]:
    return {
        "question_chars": len(question),
        "question_sha256": hashlib.sha256(question.encode()).hexdigest()[:16],
    }


def create_app(
    factory: ServicesFactory = build_services, settings: Settings | None = None
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        app.state.services = await factory(settings)
        log.info("api started", extra={"generation_model": settings.generation_model})
        try:
            yield
        finally:
            await app.state.services.close()

    app = FastAPI(
        title="Enterprise RAG over SEC 10-K filings",
        version="0.3.0",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        request.state.log_fields = {}
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["x-request-id"] = request_id
            return response
        finally:
            log.info(
                "request served",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": status,
                    "latency_ms": round((time.perf_counter() - start) * 1000, 1),
                    **request.state.log_fields,
                },
            )

    @app.exception_handler(genai_errors.APIError)
    async def model_error(request: Request, exc: genai_errors.APIError) -> JSONResponse:
        log.warning(
            "model API error", extra={"request_id": request.state.request_id, "code": exc.code}
        )
        return JSONResponse(
            status_code=502, content={"detail": f"upstream model error ({exc.code})"}
        )

    @app.exception_handler(NotImplementedError)
    async def not_implemented(request: Request, exc: NotImplementedError) -> JSONResponse:
        return JSONResponse(status_code=501, content={"detail": str(exc)})

    def services(request: Request) -> Services:
        svc: Services = request.app.state.services
        return svc

    def check_tickers(tickers: list[str], known: frozenset[str]) -> tuple[str, ...]:
        upper = tuple(t.strip().upper() for t in tickers)
        unknown = sorted(set(upper) - known)
        if unknown:
            raise HTTPException(422, f"unknown tickers {unknown}; available: {sorted(known)}")
        return upper

    @app.get("/health", response_model=Health)
    async def health(request: Request) -> Health:
        pool = services(request).pool
        docs = chunks = 0
        if pool is not None:
            docs = await pool.fetchval("SELECT count(*) FROM documents")
            chunks = await pool.fetchval("SELECT count(*) FROM chunks")
        return Health(status="ok", documents=docs, chunks=chunks)

    @app.post("/query", response_model=QueryResponse)
    async def query(body: QueryRequest, request: Request) -> QueryResponse:
        svc = services(request)
        filters = Filters(
            tickers=check_tickers(body.tickers, svc.known_tickers),
            fiscal_years=tuple(body.fiscal_years),
            items=tuple(i.strip().upper() for i in body.items),
        )
        result = await svc.service.answer(
            body.question, QueryOptions(filters=filters, top_k=body.top_k, mode=body.mode)
        )
        request.state.log_fields = {
            "endpoint": "query",
            **_question_fields(body.question),
            "answered": result.answered,
            "sources": len(result.chunks),
            "cited": len(result.cited),
            "invalid_citations": len(result.invalid_citations),
            **{f"tokens_{k}": v for k, v in vars(result.usage).items()},
            "stage_ms": result.timings.stages,
            "estimated_cost_usd": result.estimated_cost_usd,
            "model": result.generation_model,
            "retrieval_mode": result.mode,
            "backend": result.backend,
        }
        cited = set(result.cited)
        return QueryResponse(
            request_id=request.state.request_id,
            answer=result.answer,
            answered=result.answered,
            sources=[
                Source.from_chunk(i, c, i in cited) for i, c in enumerate(result.chunks, start=1)
            ],
            invalid_citations=result.invalid_citations,
            usage=UsageOut.from_usage(result.usage),
            latency_ms=result.timings.stages,
            estimated_cost_usd=result.estimated_cost_usd,
            model=result.generation_model,
            retrieval={"backend": result.backend, "mode": result.mode, "top_k": result.top_k},
        )

    @app.post("/agent", response_model=AgentResponse)
    async def agent(body: AgentRequest, request: Request) -> AgentResponse:
        svc = services(request)
        result = await svc.agent.run(body.question)
        request.state.log_fields = {
            "endpoint": "agent",
            **_question_fields(body.question),
            "answered": result.answered,
            "sources": len(result.sources),
            "cited": len(result.cited),
            "invalid_citations": len(result.invalid_citations),
            "tool_calls": len(result.tool_calls),
            **{f"tokens_{k}": v for k, v in vars(result.usage).items()},
            "stage_ms": result.timings.stages,
            "estimated_cost_usd": result.estimated_cost_usd,
            "model": result.model,
        }
        cited = set(result.cited)
        return AgentResponse(
            request_id=request.state.request_id,
            answer=result.answer,
            answered=result.answered,
            sources=[
                Source.from_chunk(i, c, i in cited) for i, c in enumerate(result.sources, start=1)
            ],
            invalid_citations=result.invalid_citations,
            usage=UsageOut.from_usage(result.usage),
            latency_ms=result.timings.stages,
            estimated_cost_usd=result.estimated_cost_usd,
            model=result.model,
            retrieval={"backend": str(svc.service.settings.retrieval_backend), "mode": "agent"},
            tool_calls=result.tool_calls,
        )

    return app


app = create_app()
