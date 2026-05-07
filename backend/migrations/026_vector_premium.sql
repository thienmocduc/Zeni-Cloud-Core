-- ════════════════════════════════════════════════════════════════════════════
-- Migration 026 — Vector DB Premium (hybrid search + RAG pipelines)
-- Builds on Sprint A2 vector module (migrations/015_vector_search.sql).
--
-- Sprint A2 design: each collection has its own table `public.vec_<ws>_<name>`
-- with columns (id TEXT PK, vector VECTOR(dim), metadata JSONB, created_at).
--
-- Premium upgrades:
--   1. Helper function to add tsvector + namespace columns to existing per-
--      collection tables on demand (called by service layer when premium
--      hybrid features are first used on a collection).
--   2. Track per-collection premium status on the registry.
--   3. RAG pipeline saved templates (vector_rag_pipelines).
--   4. Embedding cache to avoid recomputing (vector_embedding_cache).
--   5. RAG query audit log (vector_rag_queries).
-- ════════════════════════════════════════════════════════════════════════════

-- ── 1. Track premium upgrade per collection ────────────────────────────────
-- Sprint A2 table; new columns are best-effort (idempotent ALTER).
ALTER TABLE public.vector_collections
    ADD COLUMN IF NOT EXISTS premium_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS default_namespace VARCHAR(64) NOT NULL DEFAULT 'default',
    ADD COLUMN IF NOT EXISTS embedding_model VARCHAR(80) DEFAULT 'text-embedding-004',
    ADD COLUMN IF NOT EXISTS premium_upgraded_at TIMESTAMPTZ;

-- ── 2. Helper function: enable hybrid columns on any per-collection table ──
-- Idempotent: adds content, content_tsv (auto-maintained), namespace,
-- metadata_indexed, plus indexes (BM25 GIN on tsv, GIN on metadata, btree
-- composite on (namespace) for fast filtering).
CREATE OR REPLACE FUNCTION public.vector_enable_hybrid(p_table TEXT)
RETURNS VOID
LANGUAGE plpgsql
AS $func$
DECLARE
    qident TEXT := quote_ident(p_table);
BEGIN
    -- Whitelist: only allow tables in vector_collections.table_name
    IF NOT EXISTS (
        SELECT 1 FROM public.vector_collections WHERE table_name = p_table
    ) THEN
        RAISE EXCEPTION 'Unknown collection table: %', p_table;
    END IF;

    -- Add columns (each idempotent)
    EXECUTE format(
        'ALTER TABLE public.%s '
        '  ADD COLUMN IF NOT EXISTS content TEXT, '
        '  ADD COLUMN IF NOT EXISTS content_tsv tsvector, '
        '  ADD COLUMN IF NOT EXISTS namespace VARCHAR(64) NOT NULL DEFAULT ''default'', '
        '  ADD COLUMN IF NOT EXISTS metadata_indexed JSONB',
        qident
    );

    -- Trigger to auto-maintain content_tsv from content (English-default; can be
    -- overridden via metadata.lang in app layer). Use 'simple' to avoid stemming
    -- bias for non-English content.
    EXECUTE format(
        'CREATE INDEX IF NOT EXISTS idx_%s_tsv ON public.%s USING GIN(content_tsv)',
        p_table, qident
    );
    EXECUTE format(
        'CREATE INDEX IF NOT EXISTS idx_%s_namespace ON public.%s (namespace)',
        p_table, qident
    );
    EXECUTE format(
        'CREATE INDEX IF NOT EXISTS idx_%s_metaidx ON public.%s USING GIN(metadata_indexed)',
        p_table, qident
    );

    -- Auto-update content_tsv whenever content changes.
    EXECUTE format(
        'DROP TRIGGER IF EXISTS trg_%s_tsv ON public.%s',
        p_table, qident
    );
    EXECUTE format(
        'CREATE TRIGGER trg_%s_tsv BEFORE INSERT OR UPDATE OF content '
        'ON public.%s FOR EACH ROW '
        'EXECUTE FUNCTION tsvector_update_trigger(content_tsv, ''pg_catalog.simple'', content)',
        p_table, qident
    );

    -- Mark collection premium-enabled.
    UPDATE public.vector_collections
    SET premium_enabled = TRUE, premium_upgraded_at = NOW()
    WHERE table_name = p_table;
END;
$func$;

-- ── 3. RAG pipelines (saved query templates) ───────────────────────────────
CREATE TABLE IF NOT EXISTS public.vector_rag_pipelines (
    id              BIGSERIAL PRIMARY KEY,
    workspace_id    VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name            VARCHAR(120) NOT NULL,
    description     TEXT,
    collection_id   BIGINT REFERENCES public.vector_collections(id) ON DELETE CASCADE,
    embedding_model VARCHAR(80) NOT NULL DEFAULT 'text-embedding-004',
    rerank_model    VARCHAR(80),
    top_k           INT NOT NULL DEFAULT 5 CHECK (top_k > 0 AND top_k <= 100),
    rerank_top_k    INT NOT NULL DEFAULT 3 CHECK (rerank_top_k > 0 AND rerank_top_k <= 50),
    hybrid_alpha    NUMERIC(3,2) NOT NULL DEFAULT 0.5
                    CHECK (hybrid_alpha >= 0 AND hybrid_alpha <= 1),
    namespace       VARCHAR(64) DEFAULT 'default',
    system_prompt   TEXT,
    llm_model       VARCHAR(60) NOT NULL DEFAULT 'gemini-2.5-flash',
    temperature     NUMERIC(3,2) NOT NULL DEFAULT 0.4
                    CHECK (temperature >= 0 AND temperature <= 2),
    max_tokens      INT NOT NULL DEFAULT 1024 CHECK (max_tokens > 0),
    created_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, name)
);
CREATE INDEX IF NOT EXISTS idx_rag_pipelines_ws ON public.vector_rag_pipelines(workspace_id);
CREATE INDEX IF NOT EXISTS idx_rag_pipelines_collection ON public.vector_rag_pipelines(collection_id);

-- ── 4. Embedding cache (avoid recompute for identical text) ────────────────
CREATE TABLE IF NOT EXISTS public.vector_embedding_cache (
    text_hash       VARCHAR(64) PRIMARY KEY,           -- SHA-256 hex of normalized text
    embedding_model VARCHAR(80) NOT NULL,
    dim             INT NOT NULL CHECK (dim > 0 AND dim <= 4096),
    embedding       vector(768),                       -- 768 = text-embedding-004 default
    text_preview    TEXT,                              -- first 200 chars for debugging
    hit_count       INT NOT NULL DEFAULT 0,
    last_hit_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_embed_cache_model ON public.vector_embedding_cache(embedding_model);
CREATE INDEX IF NOT EXISTS idx_embed_cache_created ON public.vector_embedding_cache(created_at DESC);

-- ── 5. RAG query audit log ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.vector_rag_queries (
    id                 BIGSERIAL PRIMARY KEY,
    workspace_id       VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    pipeline_id        BIGINT REFERENCES public.vector_rag_pipelines(id) ON DELETE SET NULL,
    collection_id      BIGINT REFERENCES public.vector_collections(id) ON DELETE SET NULL,
    actor              VARCHAR(255),
    query_text         TEXT NOT NULL,
    retrieved_chunks   JSONB,                          -- [{id, score, namespace, content_excerpt}]
    rerank_scores      JSONB,                          -- [{id, score}] post-rerank
    final_answer       TEXT,
    citations          JSONB,                          -- [{doc_id, snippet}]
    latency_ms         INT,
    embed_latency_ms   INT,
    retrieve_latency_ms INT,
    rerank_latency_ms  INT,
    llm_latency_ms     INT,
    cost_usd           NUMERIC(10,6) NOT NULL DEFAULT 0,
    cache_hit          BOOLEAN NOT NULL DEFAULT FALSE,
    error              TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rag_queries_ws       ON public.vector_rag_queries(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rag_queries_pipeline ON public.vector_rag_queries(pipeline_id, created_at DESC);

-- ── 6. Grants for app role (best-effort; dev envs may lack zeni_app) ───────
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zeni_app') THEN
        EXECUTE 'GRANT ALL PRIVILEGES ON public.vector_rag_pipelines        TO zeni_app';
        EXECUTE 'GRANT ALL PRIVILEGES ON public.vector_embedding_cache      TO zeni_app';
        EXECUTE 'GRANT ALL PRIVILEGES ON public.vector_rag_queries          TO zeni_app';
        EXECUTE 'GRANT USAGE, SELECT ON public.vector_rag_pipelines_id_seq  TO zeni_app';
        EXECUTE 'GRANT USAGE, SELECT ON public.vector_rag_queries_id_seq    TO zeni_app';
        EXECUTE 'GRANT EXECUTE ON FUNCTION public.vector_enable_hybrid(TEXT) TO zeni_app';
    END IF;
END
$$;
