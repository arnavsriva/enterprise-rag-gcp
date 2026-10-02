-- Core schema: filings and their embedded chunks.
-- Embedding dimension is fixed at 768 (text-embedding-005). Changing the model's
-- dimension requires a new migration and a full re-embed (see docs/adr/0003).

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE documents (
    id              text PRIMARY KEY,               -- SEC accession number, e.g. 0000320193-25-000079
    ticker          text NOT NULL,
    cik             text NOT NULL,
    company_name    text NOT NULL,
    form_type       text NOT NULL DEFAULT '10-K',
    fiscal_year     integer NOT NULL,
    filed_at        date NOT NULL,
    source_url      text NOT NULL,
    content_sha256  text NOT NULL,                  -- detects changed source on re-ingest
    ingested_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX documents_ticker_year_idx ON documents (ticker, fiscal_year);

CREATE TABLE chunks (
    id              text PRIMARY KEY,               -- stable: {document_id}:{chunk_index}; same id in Vector Search
    document_id     text NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    chunk_index     integer NOT NULL,
    item            text,                           -- 10-K section, e.g. '1A', '7'
    item_title      text,
    content         text NOT NULL,
    token_count     integer NOT NULL,
    embedding       vector(768) NOT NULL,
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);

-- Cosine distance; embeddings are L2-normalised so this matches Vector Search's DOT_PRODUCT ranking.
CREATE INDEX chunks_embedding_hnsw_idx ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_document_item_idx ON chunks (document_id, item);
