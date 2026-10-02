"""Vertex AI Vector Search: streaming upserts (ingestion) and PSC queries (retrieval).

- Writes go to the Index via the regional Vertex AI API (works from anywhere with IAM).
- Queries go to the *deployed* index through the Private Service Connect IP, which is only
  reachable from inside the VPC (ADR-0002), so retrieval with this backend runs on Cloud Run.
- Datapoint IDs are the same chunk IDs used in Postgres; Postgres stays the system of record
  for text and metadata, so results are hydrated from it (identical context for both backends).
- Filters use token restricts: ticker, item and fiscal_year (as a string, so several years
  can be OR-ed in one allow list).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from google.api_core.client_options import ClientOptions
from google.cloud import aiplatform_v1

from common.config import Settings
from rag.types import Filters

log = logging.getLogger(__name__)

UPSERT_BATCH = 250


@dataclass(frozen=True)
class VectorSearchConfig:
    project: str
    region: str
    index_id: str
    endpoint_id: str | None = None
    deployed_index_id: str | None = None
    psc_ip: str | None = None

    @property
    def index_name(self) -> str:
        return f"projects/{self.project}/locations/{self.region}/indexes/{self.index_id}"

    @property
    def queryable(self) -> bool:
        return bool(self.endpoint_id and self.deployed_index_id and self.psc_ip)


def config_from_settings(s: Settings) -> VectorSearchConfig | None:
    if not (s.gcp_project_id and s.vector_search_index_id):
        return None
    return VectorSearchConfig(
        project=s.gcp_project_id,
        region=s.gcp_region,
        index_id=s.vector_search_index_id,
        endpoint_id=s.vector_search_index_endpoint_id or None,
        deployed_index_id=s.vector_search_deployed_index_id or None,
        psc_ip=s.vector_search_psc_ip or None,
    )


@dataclass(frozen=True)
class DatapointSpec:
    chunk_id: str
    vector: list[float]
    ticker: str
    item: str | None
    fiscal_year: int


def to_datapoint(spec: DatapointSpec) -> aiplatform_v1.IndexDatapoint:
    restricts = [
        aiplatform_v1.IndexDatapoint.Restriction(namespace="ticker", allow_list=[spec.ticker]),
        aiplatform_v1.IndexDatapoint.Restriction(
            namespace="item", allow_list=[spec.item or "none"]
        ),
        aiplatform_v1.IndexDatapoint.Restriction(
            namespace="fiscal_year", allow_list=[str(spec.fiscal_year)]
        ),
    ]
    return aiplatform_v1.IndexDatapoint(
        datapoint_id=spec.chunk_id, feature_vector=spec.vector, restricts=restricts
    )


class VertexIndexWriter:
    def __init__(self, config: VectorSearchConfig, client: Any | None = None) -> None:
        self.config = config
        self._client = client or aiplatform_v1.IndexServiceAsyncClient(
            client_options=ClientOptions(api_endpoint=f"{config.region}-aiplatform.googleapis.com")
        )

    @property
    def index_id(self) -> str:
        return self.config.index_id

    async def upsert(self, specs: Sequence[DatapointSpec]) -> int:
        for start in range(0, len(specs), UPSERT_BATCH):
            batch = [to_datapoint(s) for s in specs[start : start + UPSERT_BATCH]]
            await self._client.upsert_datapoints(
                request=aiplatform_v1.UpsertDatapointsRequest(
                    index=self.config.index_name, datapoints=batch
                )
            )
        return len(specs)

    async def remove(self, chunk_ids: Sequence[str]) -> int:
        for start in range(0, len(chunk_ids), UPSERT_BATCH):
            await self._client.remove_datapoints(
                request=aiplatform_v1.RemoveDatapointsRequest(
                    index=self.config.index_name,
                    datapoint_ids=list(chunk_ids[start : start + UPSERT_BATCH]),
                )
            )
        return len(chunk_ids)


def to_namespaces(filters: Filters) -> list[Any]:
    from google.cloud.aiplatform.matching_engine.matching_engine_index_endpoint import Namespace

    namespaces = []
    if filters.tickers:
        namespaces.append(Namespace("ticker", allow_tokens=list(filters.tickers)))
    if filters.items:
        namespaces.append(Namespace("item", allow_tokens=list(filters.items)))
    if filters.fiscal_years:
        namespaces.append(
            Namespace("fiscal_year", allow_tokens=[str(y) for y in filters.fiscal_years])
        )
    return namespaces


class VertexMatcher:
    """Queries the deployed index over PSC. The SDK's match() is blocking gRPC: run in a thread."""

    def __init__(self, config: VectorSearchConfig, endpoint: Any | None = None) -> None:
        if not config.queryable:
            raise ValueError(
                "Vector Search is not deployed: endpoint, deployed index and PSC IP required"
            )
        self.config = config
        self._endpoint_id: str = config.endpoint_id or ""
        self._psc_ip: str = config.psc_ip or ""
        self._endpoint = endpoint
        self._lock = asyncio.Lock()

    async def _get_endpoint(self) -> Any:
        async with self._lock:
            if self._endpoint is None:
                from google.cloud import aiplatform

                def make() -> Any:
                    ep = aiplatform.MatchingEngineIndexEndpoint(
                        index_endpoint_name=self._endpoint_id,
                        project=self.config.project,
                        location=self.config.region,
                    )
                    ep.private_service_connect_ip_address = self._psc_ip
                    return ep

                self._endpoint = await asyncio.to_thread(make)
        return self._endpoint

    async def search(
        self, vector: list[float], filters: Filters, k: int
    ) -> list[tuple[str, float]]:
        endpoint = await self._get_endpoint()
        results = await asyncio.to_thread(
            endpoint.match,
            deployed_index_id=self.config.deployed_index_id,
            queries=[vector],
            num_neighbors=k,
            filter=to_namespaces(filters) or None,
        )
        # DOT_PRODUCT_DISTANCE on unit vectors: the returned "distance" is the dot product,
        # i.e. cosine similarity (higher = closer), comparable to pgvector's 1 - cosine distance.
        return [(n.id, float(n.distance)) for n in (results[0] if results else [])]
