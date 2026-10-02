-- Track how each document was processed, so re-ingestion can skip unchanged filings
-- and re-process only when the source, the chunker, or the embedding model changes.

ALTER TABLE documents
    ADD COLUMN period_end       date,
    ADD COLUMN chunker_version  text,
    ADD COLUMN embedding_model  text,
    ADD COLUMN chunk_count      integer;

-- Fiscal-year label follows the issuer's convention: calendar year in which the period ends.
COMMENT ON COLUMN documents.fiscal_year IS 'Year of period_end (issuer convention, e.g. NVDA FY2026 ends Jan 2026)';
COMMENT ON COLUMN chunks.token_count IS 'Tokens billed by the embedding API for this chunk (incl. context header)';
