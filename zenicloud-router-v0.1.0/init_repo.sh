#!/usr/bin/env bash
# ZeniCloud Router - One-shot push to GitHub
# Run AFTER sếp đã:
#   1. Tạo org "zeni-cloud" tại https://github.com/organizations/new
#   2. Tạo repo trống "zenicloud-router" trong org đó (Private)
#   3. cd vào folder đã giải nén tarball
#
# Usage:
#   chmod +x init_repo.sh
#   ./init_repo.sh
#
# Requires: git, gh (optional for auto-create)

set -euo pipefail

ORG="zeni-cloud"
REPO="zenicloud-router"
REMOTE="git@github.com:${ORG}/${REPO}.git"

if [ -d .git ]; then
  echo "Repo đã init. Bỏ qua."
else
  git init -b main
fi

# Install pre-commit secret scanner (recommended)
cat > .git/hooks/pre-commit <<'HOOK'
#!/usr/bin/env bash
# Block commits containing live API keys
if git diff --cached | grep -E "sk-[a-zA-Z0-9_-]{30,}|AKIA[0-9A-Z]{16}" >/dev/null 2>&1; then
  echo "ERROR: live API key detected in diff. Commit blocked."
  echo "If false positive, use: git commit --no-verify"
  exit 1
fi
HOOK
chmod +x .git/hooks/pre-commit
echo "✓ Pre-commit hook installed"

git add .
git commit -m "feat: ZeniCloud Router v0.1.0 — initial scaffold

- 80/15/5 routing strategy (Fast/Balanced/Frontier tiers)
- 8 models in registry (Anthropic + OpenAI + Google + open-weight)
- Mock adapter for development without live keys
- Anthropic real adapter (template for OpenAI/Vertex/Bedrock)
- Failover orchestrator with auth-aware retry
- 33 tests, 100% passing
- Security: API key auth, rate limit, CORS, headers, secret redaction
- CI: GitHub Actions (test + security scan + Docker build)
- Production-ready Dockerfile (non-root, healthcheck)

Lock: 2026-04-30 · CTO Em · Reviewed Chairman Thiên Mộc Đức
Doc lineage: zeni_digital_ai_infra_strategy_v1.html § 04"

git remote add origin "$REMOTE" 2>/dev/null || git remote set-url origin "$REMOTE"
echo
echo "✓ Local commit done."
echo
echo "→ Đẩy lên GitHub:"
echo "    git push -u origin main"
echo
echo "→ Hoặc dùng gh CLI để auto-tạo repo + push:"
echo "    gh repo create ${ORG}/${REPO} --private --source=. --remote=origin --push"
