-- ============================================================================
-- Migration 035 — Zeni Workspace (Notion-like Docs + Tasks + Databases)
--
-- Purpose: Native replacement for Notion. Tree of pages, blocks, tasks,
--   inline databases, comments, collaborators, and version history.
--
-- Tables:
--   ws_pages          — tree of pages (parent_id self FK), title/icon/cover
--   ws_blocks         — content blocks (paragraph/heading/list/code/table/embed/...)
--   ws_tasks          — kanban / list tasks; can live inside a page or workspace
--   ws_databases      — Notion-style inline databases (schema in JSONB)
--   ws_database_rows  — rows of an inline database
--   ws_comments       — page-level / block-level threaded comments
--   ws_collaborators  — per-page sharing (view/comment/edit/admin)
--   ws_page_history   — version history snapshots
--
-- Search:
--   - ws_pages.search_tsv (title) + ws_blocks.search_tsv (content) — GIN indexed
--   - Triggers auto-maintain tsvector columns
-- ============================================================================

-- ─── 1. Pages (tree) ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ws_pages (
    id            BIGSERIAL PRIMARY KEY,
    workspace_id  VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    parent_id     BIGINT REFERENCES ws_pages(id) ON DELETE CASCADE,    -- self FK (tree)
    title         VARCHAR(500) NOT NULL DEFAULT 'Untitled',
    icon          VARCHAR(40),                                         -- emoji or short tag
    cover_url     TEXT,
    slug          VARCHAR(120),                                        -- url-friendly, optional
    is_archived   BOOLEAN NOT NULL DEFAULT FALSE,
    position      DOUBLE PRECISION NOT NULL DEFAULT 0,                 -- sibling order
    created_by    VARCHAR(255),                                        -- author email
    updated_by    VARCHAR(255),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    search_tsv    tsvector
);
CREATE INDEX IF NOT EXISTS idx_ws_pages_ws       ON ws_pages(workspace_id, is_archived);
CREATE INDEX IF NOT EXISTS idx_ws_pages_parent   ON ws_pages(workspace_id, parent_id, position);
CREATE INDEX IF NOT EXISTS idx_ws_pages_updated  ON ws_pages(workspace_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ws_pages_tsv      ON ws_pages USING GIN(search_tsv);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ws_pages_slug
    ON ws_pages(workspace_id, slug) WHERE slug IS NOT NULL;

-- Trigger keeping search_tsv synced from title.
CREATE OR REPLACE FUNCTION ws_pages_tsv_update() RETURNS TRIGGER AS $$
BEGIN
    NEW.search_tsv := to_tsvector('simple', COALESCE(NEW.title, ''));
    NEW.updated_at := NOW();
    RETURN NEW;
END
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_ws_pages_tsv ON ws_pages;
CREATE TRIGGER trg_ws_pages_tsv
    BEFORE INSERT OR UPDATE OF title ON ws_pages
    FOR EACH ROW EXECUTE FUNCTION ws_pages_tsv_update();


-- ─── 2. Blocks ──────────────────────────────────────────────────────────────
-- type ∈ paragraph|heading1|heading2|heading3|bulletlist|numberlist|todo|toggle|
--        code|table|embed|divider|quote|callout|image|file|database
CREATE TABLE IF NOT EXISTS ws_blocks (
    id              BIGSERIAL PRIMARY KEY,
    page_id         BIGINT NOT NULL REFERENCES ws_pages(id) ON DELETE CASCADE,
    parent_block_id BIGINT REFERENCES ws_blocks(id) ON DELETE CASCADE,   -- nesting (toggle, list)
    type            VARCHAR(20) NOT NULL DEFAULT 'paragraph',
    content         TEXT,                                                -- plain text
    properties      JSONB NOT NULL DEFAULT '{}'::jsonb,                  -- rich text, language, checked, ...
    position        DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    search_tsv      tsvector
);
CREATE INDEX IF NOT EXISTS idx_ws_blocks_page    ON ws_blocks(page_id, position);
CREATE INDEX IF NOT EXISTS idx_ws_blocks_parent  ON ws_blocks(parent_block_id, position);
CREATE INDEX IF NOT EXISTS idx_ws_blocks_type    ON ws_blocks(type);
CREATE INDEX IF NOT EXISTS idx_ws_blocks_tsv     ON ws_blocks USING GIN(search_tsv);
CREATE INDEX IF NOT EXISTS idx_ws_blocks_props   ON ws_blocks USING GIN(properties);

CREATE OR REPLACE FUNCTION ws_blocks_tsv_update() RETURNS TRIGGER AS $$
BEGIN
    NEW.search_tsv := to_tsvector('simple', COALESCE(NEW.content, ''));
    NEW.updated_at := NOW();
    RETURN NEW;
END
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_ws_blocks_tsv ON ws_blocks;
CREATE TRIGGER trg_ws_blocks_tsv
    BEFORE INSERT OR UPDATE OF content ON ws_blocks
    FOR EACH ROW EXECUTE FUNCTION ws_blocks_tsv_update();


-- ─── 3. Tasks ───────────────────────────────────────────────────────────────
-- status   ∈ backlog|todo|inprogress|review|done|cancelled
-- priority ∈ low|medium|high|urgent
CREATE TABLE IF NOT EXISTS ws_tasks (
    id              BIGSERIAL PRIMARY KEY,
    workspace_id    VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    page_id         BIGINT REFERENCES ws_pages(id) ON DELETE SET NULL,    -- optional page link
    parent_task_id  BIGINT REFERENCES ws_tasks(id) ON DELETE CASCADE,     -- subtasks
    title           VARCHAR(500) NOT NULL,
    description     TEXT,
    status          VARCHAR(20) NOT NULL DEFAULT 'todo',
    priority        VARCHAR(10) NOT NULL DEFAULT 'medium',
    assignee_email  VARCHAR(255),
    due_date        TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    position        DOUBLE PRECISION NOT NULL DEFAULT 0,
    tags            TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    created_by      VARCHAR(255),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ws_tasks_ws         ON ws_tasks(workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_ws_tasks_assignee   ON ws_tasks(workspace_id, assignee_email, status);
CREATE INDEX IF NOT EXISTS idx_ws_tasks_due        ON ws_tasks(workspace_id, due_date) WHERE due_date IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ws_tasks_page       ON ws_tasks(page_id) WHERE page_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ws_tasks_parent     ON ws_tasks(parent_task_id) WHERE parent_task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ws_tasks_tags       ON ws_tasks USING GIN(tags);

CREATE OR REPLACE FUNCTION ws_tasks_touch_update() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_ws_tasks_touch ON ws_tasks;
CREATE TRIGGER trg_ws_tasks_touch
    BEFORE UPDATE ON ws_tasks
    FOR EACH ROW EXECUTE FUNCTION ws_tasks_touch_update();


-- ─── 4. Inline databases (Notion-like) ─────────────────────────────────────
-- properties is the column schema:
--   { "fields":[ {"key":"name","label":"Name","type":"text"},
--                {"key":"status","label":"Status","type":"select",
--                 "options":["todo","done"]} ] }
CREATE TABLE IF NOT EXISTS ws_databases (
    id            BIGSERIAL PRIMARY KEY,
    workspace_id  VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    page_id       BIGINT REFERENCES ws_pages(id) ON DELETE CASCADE,
    name          VARCHAR(200) NOT NULL,
    properties    JSONB NOT NULL DEFAULT '{"fields":[]}'::jsonb,
    created_by    VARCHAR(255),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ws_databases_ws    ON ws_databases(workspace_id);
CREATE INDEX IF NOT EXISTS idx_ws_databases_page  ON ws_databases(page_id) WHERE page_id IS NOT NULL;

-- Database rows — properties is a free-form key→value dict matching the schema.
CREATE TABLE IF NOT EXISTS ws_database_rows (
    id            BIGSERIAL PRIMARY KEY,
    database_id   BIGINT NOT NULL REFERENCES ws_databases(id) ON DELETE CASCADE,
    properties    JSONB NOT NULL DEFAULT '{}'::jsonb,
    position      DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ws_db_rows_db     ON ws_database_rows(database_id, position);
CREATE INDEX IF NOT EXISTS idx_ws_db_rows_props  ON ws_database_rows USING GIN(properties);


-- ─── 5. Comments (page-level + block-level threaded) ───────────────────────
CREATE TABLE IF NOT EXISTS ws_comments (
    id                 BIGSERIAL PRIMARY KEY,
    page_id            BIGINT NOT NULL REFERENCES ws_pages(id) ON DELETE CASCADE,
    block_id           BIGINT REFERENCES ws_blocks(id) ON DELETE CASCADE,    -- NULL = page-level
    parent_comment_id  BIGINT REFERENCES ws_comments(id) ON DELETE CASCADE,  -- thread
    author_email       VARCHAR(255) NOT NULL,
    content            TEXT NOT NULL,
    resolved           BOOLEAN NOT NULL DEFAULT FALSE,
    resolved_at        TIMESTAMPTZ,
    resolved_by        VARCHAR(255),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ws_comments_page    ON ws_comments(page_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ws_comments_block   ON ws_comments(block_id) WHERE block_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ws_comments_thread  ON ws_comments(parent_comment_id) WHERE parent_comment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ws_comments_open    ON ws_comments(page_id, resolved) WHERE resolved = FALSE;


-- ─── 6. Collaborators (per-page sharing) ───────────────────────────────────
-- permission ∈ view|comment|edit|admin
CREATE TABLE IF NOT EXISTS ws_collaborators (
    page_id      BIGINT NOT NULL REFERENCES ws_pages(id) ON DELETE CASCADE,
    user_email   VARCHAR(255) NOT NULL,
    permission   VARCHAR(10) NOT NULL DEFAULT 'view',
    invited_by   VARCHAR(255),
    invited_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (page_id, user_email)
);
CREATE INDEX IF NOT EXISTS idx_ws_collab_user ON ws_collaborators(user_email);


-- ─── 7. Page version history ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ws_page_history (
    id          BIGSERIAL PRIMARY KEY,
    page_id     BIGINT NOT NULL REFERENCES ws_pages(id) ON DELETE CASCADE,
    snapshot    JSONB NOT NULL,                          -- {page:{...}, blocks:[...]}
    edited_by   VARCHAR(255),
    note        VARCHAR(200),                            -- optional ("manual save", "auto", ...)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ws_history_page  ON ws_page_history(page_id, created_at DESC);
