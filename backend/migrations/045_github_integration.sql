-- Migration 045 — GitHub Integration Phase 1
-- Cho phép khách connect GitHub repo → auto deploy lên Zeni Cloud (như Vercel)

CREATE TABLE IF NOT EXISTS github_connections (
  id              BIGSERIAL PRIMARY KEY,
  workspace_id    VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  -- Repository info
  repo_url        TEXT NOT NULL,                       -- https://github.com/user/repo
  repo_owner      TEXT NOT NULL,                       -- 'user' or 'org'
  repo_name       TEXT NOT NULL,                       -- 'repo'
  default_branch  VARCHAR(64) NOT NULL DEFAULT 'main',
  -- Auth (nếu private repo cần Personal Access Token)
  is_private      BOOLEAN NOT NULL DEFAULT FALSE,
  access_token_enc BYTEA,                              -- KMS-encrypted GitHub PAT
  -- Webhook
  webhook_secret  TEXT NOT NULL,                       -- shared secret for HMAC verify
  webhook_id      BIGINT,                              -- GitHub's webhook ID (for delete later)
  -- Auto-deploy config
  auto_deploy     BOOLEAN NOT NULL DEFAULT TRUE,
  build_command   TEXT,                                -- override default build (optional)
  install_command TEXT,                                -- npm ci, pip install -r requirements.txt
  output_dir      TEXT,                                -- public/, dist/, build/
  framework       VARCHAR(32),                         -- 'nextjs' | 'react' | 'vue' | 'static' | 'fastapi' | 'express' | 'auto'
  port            INT NOT NULL DEFAULT 8080,
  env_vars        JSONB NOT NULL DEFAULT '{}'::jsonb,
  -- Project linkage
  project_id      UUID REFERENCES projects(id) ON DELETE SET NULL,
  -- State
  status          VARCHAR(20) NOT NULL DEFAULT 'connected',  -- 'connected'|'disabled'|'error'
  last_deploy_at  TIMESTAMPTZ,
  last_deploy_sha VARCHAR(40),
  last_deploy_status VARCHAR(20),                      -- 'success'|'failed'|'building'
  metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
  -- Audit
  created_by      UUID,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (workspace_id, repo_owner, repo_name)
);

CREATE INDEX IF NOT EXISTS idx_gh_conn_ws ON github_connections (workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_gh_conn_project ON github_connections (project_id);

-- Build/deploy history
CREATE TABLE IF NOT EXISTS github_deploys (
  id              BIGSERIAL PRIMARY KEY,
  connection_id   BIGINT NOT NULL REFERENCES github_connections(id) ON DELETE CASCADE,
  workspace_id    VARCHAR(32) NOT NULL,
  -- Trigger
  trigger_type    VARCHAR(20) NOT NULL,  -- 'webhook'|'manual'|'redeploy'
  commit_sha      VARCHAR(40) NOT NULL,
  commit_message  TEXT,
  commit_author   TEXT,
  branch          VARCHAR(64),
  -- Build
  build_id        TEXT,                  -- Cloud Build operation ID
  build_url       TEXT,                  -- log URL
  status          VARCHAR(20) NOT NULL DEFAULT 'queued',  -- 'queued'|'building'|'deploying'|'success'|'failed'|'cancelled'
  -- Deploy
  image_url       TEXT,                  -- Artifact Registry URL
  deploy_url      TEXT,                  -- final Cloud Run URL
  -- Timing
  started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at    TIMESTAMPTZ,
  duration_sec    INT,
  -- Error
  error_message   TEXT,
  build_log       TEXT
);

CREATE INDEX IF NOT EXISTS idx_gh_deploys_conn ON github_deploys (connection_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_gh_deploys_status ON github_deploys (status) WHERE status IN ('queued','building','deploying');

-- Framework detection templates (built-in defaults)
CREATE TABLE IF NOT EXISTS github_framework_templates (
  framework       VARCHAR(32) PRIMARY KEY,
  display_name    TEXT NOT NULL,
  detect_files    TEXT[] NOT NULL,        -- e.g. ['next.config.js','package.json with next']
  install_cmd     TEXT NOT NULL,
  build_cmd       TEXT,
  output_dir      TEXT,
  default_port    INT NOT NULL DEFAULT 8080,
  dockerfile_template TEXT NOT NULL       -- generated if no Dockerfile in repo
);

INSERT INTO github_framework_templates (framework, display_name, detect_files, install_cmd, build_cmd, output_dir, default_port, dockerfile_template) VALUES
  ('nextjs', 'Next.js', ARRAY['next.config.js','next.config.mjs','next.config.ts'], 'npm ci', 'npm run build', '.next', 3000,
$$FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build
FROM node:20-alpine
WORKDIR /app
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public 2>/dev/null || true
EXPOSE 3000
ENV PORT=3000
CMD ["node", "server.js"]$$),
  ('react', 'React/Vite', ARRAY['vite.config.js','vite.config.ts'], 'npm ci', 'npm run build', 'dist', 80,
$$FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build
FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE 80$$),
  ('vue', 'Vue.js', ARRAY['vue.config.js'], 'npm ci', 'npm run build', 'dist', 80,
$$FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build
FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE 80$$),
  ('static', 'Static HTML', ARRAY['index.html'], '', '', '.', 80,
$$FROM nginx:alpine
COPY . /usr/share/nginx/html
EXPOSE 80$$),
  ('fastapi', 'FastAPI', ARRAY['requirements.txt with fastapi','main.py with FastAPI'], 'pip install -r requirements.txt', '', '.', 8080,
$$FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]$$),
  ('express', 'Express.js', ARRAY['package.json with express'], 'npm ci', '', '.', 3000,
$$FROM node:20-alpine
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev
COPY . .
EXPOSE 3000
CMD ["node", "server.js"]$$)
ON CONFLICT (framework) DO UPDATE SET
  display_name = EXCLUDED.display_name,
  detect_files = EXCLUDED.detect_files,
  install_cmd = EXCLUDED.install_cmd,
  build_cmd = EXCLUDED.build_cmd,
  output_dir = EXCLUDED.output_dir,
  default_port = EXCLUDED.default_port,
  dockerfile_template = EXCLUDED.dockerfile_template;
