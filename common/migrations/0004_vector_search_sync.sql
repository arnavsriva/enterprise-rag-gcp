-- Which Vertex AI Vector Search index (if any) each document's chunks have been upserted to.
-- Lets ingestion sync Vector Search from vectors already stored in Postgres, without
-- re-embedding, and skip documents that are already in sync.

ALTER TABLE documents ADD COLUMN vector_search_index text;
