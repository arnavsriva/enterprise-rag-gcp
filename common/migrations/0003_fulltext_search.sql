-- Full-text search over chunks, for hybrid retrieval (vector + keyword, fused with RRF).
-- Keyword search catches exact terms embeddings blur: line-item names ("net interest income"),
-- product names, tickers in text. Generated column: always in sync, no ingest changes needed.

ALTER TABLE chunks
    ADD COLUMN tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(item_title, '') || ' ' || content)
    ) STORED;

CREATE INDEX chunks_tsv_gin_idx ON chunks USING gin (tsv);
