-- ============================================================================
-- Migration 033 — Zeni Studio (visual no-code app builder)
--
-- Zeni Studio is the visual builder layer of Zeni Cloud — Bubble.io / Webflow /
-- Retool, but VN-native and integrated with the rest of the platform:
--   * Customers drag-drop components (web/mobile/agent) onto a canvas
--   * Studio stores a JSON tree of components/pages/data sources/actions
--   * The renderer turns that tree into Next.js / React / Vue / Svelte source
--   * Render output deploys via Zeni Cloud Compute (Cloud Run)
--   * AI assist (ZeniRouter) generates trees from natural-language prompts,
--     suggests improvements, or designs a theme from a one-line description
--
-- Tables (9):
--   1. studio_projects       — top-level project (web/mobile/agent app)
--   2. studio_components     — DAG of components (the canvas tree)
--   3. studio_pages          — pages within a project (paths + root component)
--   4. studio_data_sources   — API / SQL / static / zeni-router data bindings
--   5. studio_actions        — JS / API / workflow event handlers
--   6. studio_assets         — uploaded media (image / font / icon)
--   7. studio_themes         — design tokens (colors, fonts, spacing) per ws
--   8. studio_templates      — public templates marketplace (5 seeded)
--   9. studio_versions       — version snapshots for rollback / publish history
-- ============================================================================

-- ─── 1. Projects ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS studio_projects (
    id              BIGSERIAL PRIMARY KEY,
    workspace_id    VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name            VARCHAR(160) NOT NULL,
    slug            VARCHAR(120) NOT NULL,
    description     TEXT,
    type            VARCHAR(20) NOT NULL DEFAULT 'web',          -- 'web','mobile','agent'
    framework       VARCHAR(20) NOT NULL DEFAULT 'next',         -- 'next','react','vue','svelte'
    canvas_tree     JSONB NOT NULL DEFAULT '{}'::jsonb,           -- denormalised tree (cache)
    theme           JSONB NOT NULL DEFAULT '{}'::jsonb,           -- inline theme override
    version         INT NOT NULL DEFAULT 1,
    published_at    TIMESTAMPTZ,
    preview_url     TEXT,
    publish_url     TEXT,
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_studio_projects_ws  ON studio_projects(workspace_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_studio_projects_typ ON studio_projects(workspace_id, type);

-- ─── 2. Components (DAG) ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS studio_components (
    id              BIGSERIAL PRIMARY KEY,
    project_id      BIGINT NOT NULL REFERENCES studio_projects(id) ON DELETE CASCADE,
    parent_id       BIGINT REFERENCES studio_components(id) ON DELETE CASCADE,
    type            VARCHAR(60) NOT NULL,                          -- 'container','text','button','image','input','grid',...
    name            VARCHAR(160),
    props           JSONB NOT NULL DEFAULT '{}'::jsonb,            -- arbitrary props (text, src, href, ...)
    style           JSONB NOT NULL DEFAULT '{}'::jsonb,            -- inline style + tailwind classes
    events          JSONB NOT NULL DEFAULT '{}'::jsonb,            -- { onClick: action_id, onMount: ... }
    children_order  BIGINT[] DEFAULT ARRAY[]::BIGINT[],            -- explicit ordering of child component ids
    locked          BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_studio_components_proj   ON studio_components(project_id);
CREATE INDEX IF NOT EXISTS idx_studio_components_parent ON studio_components(parent_id);
CREATE INDEX IF NOT EXISTS idx_studio_components_type   ON studio_components(project_id, type);

-- ─── 3. Pages ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS studio_pages (
    id                  BIGSERIAL PRIMARY KEY,
    project_id          BIGINT NOT NULL REFERENCES studio_projects(id) ON DELETE CASCADE,
    path                VARCHAR(200) NOT NULL,                     -- '/', '/shop', '/blog/[slug]'
    title               VARCHAR(200) NOT NULL,
    layout_id           BIGINT REFERENCES studio_components(id) ON DELETE SET NULL,
    root_component_id   BIGINT REFERENCES studio_components(id) ON DELETE SET NULL,
    meta                JSONB NOT NULL DEFAULT '{}'::jsonb,        -- {description, og:image, ...}
    is_default          BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, path)
);
CREATE INDEX IF NOT EXISTS idx_studio_pages_proj ON studio_pages(project_id);

-- ─── 4. Data sources ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS studio_data_sources (
    id          BIGSERIAL PRIMARY KEY,
    project_id  BIGINT NOT NULL REFERENCES studio_projects(id) ON DELETE CASCADE,
    name        VARCHAR(160) NOT NULL,
    type        VARCHAR(20) NOT NULL,                              -- 'api','sql','static','zeni-router'
    config      JSONB NOT NULL DEFAULT '{}'::jsonb,                -- { url, method, headers, query, ... }
    schema      JSONB NOT NULL DEFAULT '{}'::jsonb,                -- declared response schema
    cache_ttl_s INT DEFAULT 60,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_studio_data_proj ON studio_data_sources(project_id);

-- ─── 5. Actions (event handlers) ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS studio_actions (
    id          BIGSERIAL PRIMARY KEY,
    project_id  BIGINT NOT NULL REFERENCES studio_projects(id) ON DELETE CASCADE,
    name        VARCHAR(160) NOT NULL,
    type        VARCHAR(20) NOT NULL,                              -- 'js','api','workflow'
    code        TEXT NOT NULL DEFAULT '',                          -- inline JS / serialised graph
    params      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_studio_actions_proj ON studio_actions(project_id);

-- ─── 6. Assets ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS studio_assets (
    id          BIGSERIAL PRIMARY KEY,
    project_id  BIGINT NOT NULL REFERENCES studio_projects(id) ON DELETE CASCADE,
    name        VARCHAR(200) NOT NULL,
    type        VARCHAR(20) NOT NULL,                              -- 'image','font','icon'
    url         TEXT NOT NULL,
    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,                -- {width, height, mime, bytes, ...}
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_studio_assets_proj ON studio_assets(project_id);

-- ─── 7. Themes (design tokens) ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS studio_themes (
    id              BIGSERIAL PRIMARY KEY,
    workspace_id    VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name            VARCHAR(160) NOT NULL,
    tokens          JSONB NOT NULL DEFAULT '{}'::jsonb,            -- { colors:{}, fonts:{}, spacing:{}, radius:{} }
    is_default      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, name)
);
CREATE INDEX IF NOT EXISTS idx_studio_themes_ws ON studio_themes(workspace_id);

-- ─── 8. Templates marketplace ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS studio_templates (
    id              BIGSERIAL PRIMARY KEY,
    name            VARCHAR(160) NOT NULL UNIQUE,
    category        VARCHAR(60) NOT NULL,                          -- 'landing','ecommerce','blog','dashboard','form'
    description     TEXT,
    tree            JSONB NOT NULL DEFAULT '{}'::jsonb,            -- snapshot to clone on install
    preview_url     TEXT,
    is_public       BOOLEAN NOT NULL DEFAULT TRUE,
    install_count   BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_studio_templates_cat ON studio_templates(category, is_public);

-- ─── 9. Versions (snapshots) ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS studio_versions (
    id              BIGSERIAL PRIMARY KEY,
    project_id      BIGINT NOT NULL REFERENCES studio_projects(id) ON DELETE CASCADE,
    version_number  INT NOT NULL,
    snapshot        JSONB NOT NULL,                                -- full project tree (components + pages + sources + actions + theme)
    note            TEXT,
    deployed        BOOLEAN NOT NULL DEFAULT FALSE,
    deployed_url    TEXT,
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, version_number)
);
CREATE INDEX IF NOT EXISTS idx_studio_versions_proj ON studio_versions(project_id, version_number DESC);


-- ─── 10. Seed templates (5 starter templates) ───────────────────────────────
INSERT INTO studio_templates (name, category, description, tree, preview_url, is_public)
VALUES
(
    'Landing - Anima Hero',
    'landing',
    'Single-page landing với hero, features 3 cột, testimonials và CTA cuối trang. Phù hợp cho startup ra mắt sản phẩm.',
    '{
      "framework": "next",
      "pages": [{
        "path": "/", "title": "Welcome",
        "tree": {
          "type": "container", "name": "page-root",
          "style": {"className": "min-h-screen bg-white"},
          "children": [
            {"type": "section", "name": "hero",
             "style": {"className": "py-24 px-6 text-center bg-gradient-to-br from-orange-50 to-pink-50"},
             "children": [
               {"type": "heading", "props": {"level": 1, "text": "Sản phẩm tuyệt vời"}, "style": {"className": "text-5xl font-bold mb-4"}},
               {"type": "text", "props": {"text": "Mô tả ngắn gọn về sản phẩm của bạn"}, "style": {"className": "text-xl text-gray-600 mb-8"}},
               {"type": "button", "props": {"label": "Bắt đầu ngay", "href": "#cta"}, "style": {"className": "bg-orange-500 text-white px-8 py-3 rounded-lg"}}
             ]},
            {"type": "section", "name": "features",
             "style": {"className": "py-20 px-6 max-w-6xl mx-auto"},
             "children": [
               {"type": "grid", "props": {"cols": 3, "gap": 8},
                "children": [
                  {"type": "card", "props": {"title": "Nhanh", "body": "Tốc độ phản hồi <100ms"}},
                  {"type": "card", "props": {"title": "An toàn", "body": "Bảo mật end-to-end"}},
                  {"type": "card", "props": {"title": "Dễ dùng", "body": "Không cần code"}}
                ]}
             ]}
          ]
        }
      }],
      "theme": {"colors": {"primary": "#ff6b35", "bg": "#ffffff", "text": "#0f172a"}}
    }'::jsonb,
    NULL,
    TRUE
),
(
    'E-commerce Storefront',
    'ecommerce',
    'Storefront với header, product grid, giỏ hàng và footer. Tích hợp sẵn data source kiểu zeni-router để load product list.',
    '{
      "framework": "next",
      "pages": [{
        "path": "/", "title": "Shop",
        "tree": {
          "type": "container", "name": "page-root",
          "style": {"className": "min-h-screen bg-neutral-50"},
          "children": [
            {"type": "navbar", "props": {"brand": "Shop", "links": ["Home","Products","Cart"]}},
            {"type": "section", "name": "products",
             "style": {"className": "py-12 px-6 max-w-7xl mx-auto"},
             "children": [
               {"type": "heading", "props": {"level": 2, "text": "Sản phẩm nổi bật"}, "style": {"className": "text-3xl font-bold mb-8"}},
               {"type": "product_grid", "props": {"cols": 4, "data_source": "products"}, "style": {"className": "gap-6"}}
             ]},
            {"type": "footer", "props": {"text": "© Shop"}}
          ]
        }
      }],
      "data_sources": [{"name":"products","type":"zeni-router","config":{"task":"products.list"}}],
      "theme": {"colors": {"primary": "#0ea5e9", "bg": "#fafafa", "text": "#020617"}}
    }'::jsonb,
    NULL,
    TRUE
),
(
    'Blog - Editorial',
    'blog',
    'Blog với danh sách bài viết, trang chi tiết, tìm kiếm và phân loại theo tag. Layout 2 cột (sidebar + content).',
    '{
      "framework": "next",
      "pages": [
        {"path": "/", "title": "Blog",
         "tree": {"type":"container","name":"page-root","style":{"className":"max-w-5xl mx-auto p-6"},
           "children":[
             {"type":"navbar","props":{"brand":"Blog","links":["Home","About","Tags"]}},
             {"type":"heading","props":{"level":1,"text":"Bài viết mới"},"style":{"className":"text-4xl font-bold my-8"}},
             {"type":"post_list","props":{"data_source":"posts","layout":"vertical"}}
           ]}},
        {"path": "/post/[slug]", "title": "Post detail",
         "tree": {"type":"container","name":"post-root","style":{"className":"max-w-3xl mx-auto p-6 prose"},
           "children":[
             {"type":"post_detail","props":{"data_source":"post"}}
           ]}}
      ],
      "data_sources": [
        {"name":"posts","type":"zeni-router","config":{"task":"posts.list"}},
        {"name":"post","type":"zeni-router","config":{"task":"posts.detail"}}
      ],
      "theme": {"colors": {"primary": "#7c3aed", "bg": "#ffffff", "text": "#111827"}}
    }'::jsonb,
    NULL,
    TRUE
),
(
    'Dashboard - Admin',
    'dashboard',
    'Admin dashboard với sidebar, stats cards, charts và bảng dữ liệu. Phù hợp cho internal tools.',
    '{
      "framework": "next",
      "pages": [{
        "path": "/", "title": "Dashboard",
        "tree": {
          "type": "layout", "name": "shell",
          "style": {"className": "flex h-screen"},
          "children": [
            {"type": "sidebar", "props": {"brand": "Admin", "items": ["Overview","Users","Orders","Settings"]}, "style": {"className": "w-64 bg-slate-900 text-white"}},
            {"type": "main", "name": "content",
             "style": {"className": "flex-1 p-8 overflow-auto"},
             "children": [
               {"type": "heading", "props": {"level": 1, "text": "Overview"}, "style": {"className": "text-3xl font-bold mb-6"}},
               {"type": "grid", "props": {"cols": 4, "gap": 4},
                "children": [
                  {"type": "stat_card", "props": {"label": "Doanh thu", "value": "₫ 12,4M"}},
                  {"type": "stat_card", "props": {"label": "Đơn hàng", "value": "248"}},
                  {"type": "stat_card", "props": {"label": "Khách mới", "value": "47"}},
                  {"type": "stat_card", "props": {"label": "Tỷ lệ chuyển đổi", "value": "3.2%"}}
                ]},
               {"type": "chart", "props": {"kind": "line", "data_source": "revenue"}, "style": {"className": "mt-8"}},
               {"type": "table", "props": {"data_source": "orders"}, "style": {"className": "mt-8"}}
             ]}
          ]
        }
      }],
      "data_sources": [
        {"name":"revenue","type":"zeni-router","config":{"task":"analytics.revenue"}},
        {"name":"orders","type":"zeni-router","config":{"task":"orders.list"}}
      ],
      "theme": {"colors": {"primary": "#0f172a", "bg": "#f8fafc", "text": "#0f172a"}}
    }'::jsonb,
    NULL,
    TRUE
),
(
    'Form - Contact',
    'form',
    'Form đơn giản với validate, submit qua API và thông báo thành công. Phù hợp cho contact / lead capture.',
    '{
      "framework": "next",
      "pages": [{
        "path": "/", "title": "Liên hệ",
        "tree": {
          "type": "container", "name": "page-root",
          "style": {"className": "min-h-screen flex items-center justify-center bg-slate-50 p-6"},
          "children": [
            {"type": "form", "name": "contact-form",
             "style": {"className": "w-full max-w-md bg-white p-8 rounded-xl shadow"},
             "events": {"onSubmit": "submit_contact"},
             "children": [
               {"type": "heading", "props": {"level": 2, "text": "Liên hệ chúng tôi"}, "style": {"className": "text-2xl font-bold mb-6"}},
               {"type": "input", "props": {"name":"name","label":"Họ tên","placeholder":"Nguyễn Văn A","required":true}, "style": {"className": "mb-4"}},
               {"type": "input", "props": {"name":"email","label":"Email","type":"email","required":true}, "style": {"className": "mb-4"}},
               {"type": "textarea", "props": {"name":"message","label":"Lời nhắn","rows":4,"required":true}, "style": {"className": "mb-6"}},
               {"type": "button", "props": {"label":"Gửi","type":"submit"}, "style": {"className": "w-full bg-blue-600 text-white py-3 rounded-lg font-medium"}}
             ]}
          ]
        }
      }],
      "actions": [{"name":"submit_contact","type":"api","code":"","params":{"method":"POST","url":"/api/contact"}}],
      "theme": {"colors": {"primary": "#2563eb", "bg": "#f8fafc", "text": "#0f172a"}}
    }'::jsonb,
    NULL,
    TRUE
)
ON CONFLICT (name) DO NOTHING;
