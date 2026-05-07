-- ═══════════════════════════════════════════════════════════
-- Stream A1 — Vector Search (pgvector)
-- File: 015_vector_search.sql
-- Idempotent migration: enable pgvector, create vector_collections registry.
-- Per-collection table được tạo dynamically bởi service: public.vec_<ws>_<name>
-- ═══════════════════════════════════════════════════════════

-- 1. Enable pgvector extension (Cloud SQL Postgres 16 đã hỗ trợ sẵn)
CREATE EXTENSION IF NOT EXISTS vector;

-- 2. Registry table: liệt kê tất cả collection theo workspace
CREATE TABLE IF NOT EXISTS public.vector_collections (
  id          BIGSERIAL    PRIMARY KEY,
  workspace_id TEXT        NOT NULL,
  name        TEXT         NOT NULL,
  dim         INT          NOT NULL CHECK (dim > 0 AND dim <= 4096),
  metric      TEXT         NOT NULL DEFAULT 'cosine'
                            CHECK (metric IN ('cosine','l2','ip')),
  row_count   BIGINT       NOT NULL DEFAULT 0,
  table_name  TEXT         NOT NULL,
  created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  UNIQUE (workspace_id, name)
);

-- 3. Index cho query path phổ biến (list collections theo workspace)
CREATE INDEX IF NOT EXISTS idx_vector_collections_ws
  ON public.vector_collections(workspace_id);

-- 4. Grant cho app user
GRANT ALL PRIVILEGES ON public.vector_collections TO zeni_app;
GRANT USAGE, SELECT ON public.vector_collections_id_seq TO zeni_app;
