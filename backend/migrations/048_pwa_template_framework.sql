-- Migration 048 — Register PWA framework templates
-- Khach deploy Next.js PWA / Vite PWA -> auto-install-able (manifest + service worker)
-- PWA injection logic chay trong build worker (app/services/source_build.py + github_build.py)

INSERT INTO github_framework_templates (
  framework, display_name, detect_files, install_cmd, build_cmd, output_dir, default_port, dockerfile_template
) VALUES (
  'nextjs-pwa',
  'Next.js PWA (Install-able)',
  ARRAY['next.config.js', 'next.config.mjs', 'next.config.ts'],
  'npm ci && npm install --save-dev next-pwa',
  'npm run build',
  '.next',
  3000,
  'FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci || npm install
RUN npm install --save-dev next-pwa workbox-webpack-plugin
COPY . .
RUN npm run build

FROM node:20-alpine
WORKDIR /app
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
EXPOSE 3000
ENV PORT=3000
CMD ["node", "server.js"]'
)
ON CONFLICT (framework) DO UPDATE SET
  display_name = EXCLUDED.display_name,
  detect_files = EXCLUDED.detect_files,
  install_cmd = EXCLUDED.install_cmd,
  build_cmd = EXCLUDED.build_cmd,
  output_dir = EXCLUDED.output_dir,
  default_port = EXCLUDED.default_port,
  dockerfile_template = EXCLUDED.dockerfile_template;

INSERT INTO github_framework_templates (
  framework, display_name, detect_files, install_cmd, build_cmd, output_dir, default_port, dockerfile_template
) VALUES (
  'vite-pwa',
  'Vite + React PWA (Install-able)',
  ARRAY['vite.config.js', 'vite.config.ts'],
  'npm ci && npm install --save-dev vite-plugin-pwa',
  'npm run build',
  'dist',
  80,
  'FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci || npm install
RUN npm install --save-dev vite-plugin-pwa workbox-window
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE 80'
)
ON CONFLICT (framework) DO UPDATE SET
  display_name = EXCLUDED.display_name,
  detect_files = EXCLUDED.detect_files,
  install_cmd = EXCLUDED.install_cmd,
  build_cmd = EXCLUDED.build_cmd,
  output_dir = EXCLUDED.output_dir,
  default_port = EXCLUDED.default_port,
  dockerfile_template = EXCLUDED.dockerfile_template;
