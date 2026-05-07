-- ═══════════════════════════════════════════════════════════
-- L1: Expand version field to fit Cloud Run revision counter
-- Cloud Run revision name: {service}-{n}-{rand} → up to 60 chars
-- We store 'rev-{counter}-{rand}' → expand version to VARCHAR(48)
-- ═══════════════════════════════════════════════════════════

ALTER TABLE projects ALTER COLUMN version TYPE VARCHAR(48);
