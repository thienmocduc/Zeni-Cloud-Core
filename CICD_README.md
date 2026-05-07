# Zeni Cloud Core — CI/CD Pipeline

Hệ thống CI/CD GitHub Actions tự động cho Zeni Cloud Core. Được thiết kế để build → test → deploy mà không cần thao tác tay, đồng thời rollback nhanh khi production gặp lỗi.

---

## Tổng quan workflow

| Workflow | File | Trigger | Mục đích |
|---|---|---|---|
| **CI** | `.github/workflows/ci.yml` | PR → main, push → main/develop | Lint + security + tests + build check |
| **Deploy Production** | `.github/workflows/deploy.yml` | Push → main hoặc manual | Build → push Artifact Registry → deploy Cloud Run → smoke test |
| **Rollback** | `.github/workflows/rollback.yml` | Manual only | Route 100% traffic về 1 revision cũ |
| **Dependency Update** | `.github/workflows/dependency-update.yml` | Cron Mon 03:00 UTC | Auto-PR refresh `requirements.txt` |

---

## 1. Setup Workload Identity Federation (lần đầu)

Workflow dùng **Workload Identity Federation** thay vì service account JSON key — không lưu credential trong GitHub Secrets, an toàn hơn nhiều.

```bash
# Biến môi trường
PROJECT_ID="zeni-cloud-core"
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
POOL_ID="github"
PROVIDER_ID="github-provider"
GH_REPO="thienmocduc/Zeni-Cloud-Core"   # đổi cho đúng owner/repo
DEPLOYER_SA="github-deployer@$PROJECT_ID.iam.gserviceaccount.com"

# 1. Tạo Workload Identity Pool
gcloud iam workload-identity-pools create $POOL_ID \
  --project=$PROJECT_ID \
  --location=global \
  --display-name="GitHub Actions Pool"

# 2. Tạo OIDC provider cho GitHub
gcloud iam workload-identity-pools providers create-oidc $PROVIDER_ID \
  --project=$PROJECT_ID \
  --location=global \
  --workload-identity-pool=$POOL_ID \
  --display-name="GitHub OIDC" \
  --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
  --attribute-condition="assertion.repository_owner=='thienmocduc'" \
  --issuer-uri="https://token.actions.githubusercontent.com"

# 3. Tạo deployer service account
gcloud iam service-accounts create github-deployer \
  --project=$PROJECT_ID \
  --display-name="GitHub Actions Deployer"

# 4. Cấp quyền cần thiết cho deployer
for ROLE in roles/run.admin roles/artifactregistry.writer roles/iam.serviceAccountUser roles/cloudsql.client roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:$DEPLOYER_SA" \
    --role="$ROLE"
done

# 5. Cho phép GitHub Actions impersonate deployer SA
gcloud iam service-accounts add-iam-policy-binding $DEPLOYER_SA \
  --project=$PROJECT_ID \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL_ID/attribute.repository/$GH_REPO"
```

Sau khi xong, copy `$PROJECT_NUMBER` ra để add vào GitHub Secrets bên dưới.

---

## 2. Required GitHub Secrets

Đi tới **Settings → Secrets and variables → Actions** của repo và add:

| Secret | Bắt buộc | Giá trị |
|---|---|---|
| `GCP_PROJECT_NUMBER` | ✅ | Output của bước 1 (số 12 chữ số). Workflow dùng để build path WIF. |
| `SLACK_WEBHOOK_URL` | ❌ optional | Slack incoming webhook để nhận notify deploy. Thiếu thì step Slack tự skip. |

> **KHÔNG** add `GCP_SA_KEY` JSON — Workload Identity đã thay thế hoàn toàn.

---

## 3. Required GCP Secrets

Workflow `deploy.yml` mount 7 secret từ Secret Manager vào Cloud Run. Chuẩn bị trước:

```bash
gcloud secrets create zeni-database-url --data-file=- <<< "postgresql+asyncpg://user:pass@/db?host=/cloudsql/..."
gcloud secrets create zeni-jwt-secret --data-file=- <<< "$(openssl rand -hex 32)"
gcloud secrets create zeni-vault-key --data-file=- <<< "$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
gcloud secrets create zeni-admin-password --data-file=- <<< "ChangeMeStrong!"
gcloud secrets create zeni-smtp-user --data-file=- <<< "doanhnhancaotuan@gmail.com"
gcloud secrets create zeni-smtp-password --data-file=- <<< "<gmail-app-password>"
gcloud secrets create zeni-cron-secret --data-file=- <<< "$(openssl rand -hex 24)"

# Cấp quyền cho Cloud Run runtime SA (zeni-cloud-core-sa)
for S in zeni-database-url zeni-jwt-secret zeni-vault-key zeni-admin-password zeni-smtp-user zeni-smtp-password zeni-cron-secret; do
  gcloud secrets add-iam-policy-binding $S \
    --member="serviceAccount:zeni-cloud-core-sa@zeni-cloud-core.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"
done
```

---

## 4. Manual Deploy (workflow_dispatch)

Khi muốn deploy thủ công với 1 tag cụ thể:

1. Vào **Actions → Deploy Production → Run workflow**
2. Chọn branch `main`
3. Optional: nhập `tag=v56` (mặc định auto-increment từ tag mới nhất)
4. Optional: tick `skip_smoke=true` chỉ khi khẩn cấp

Workflow sẽ:
- Build image → push `us-central1-docker.pkg.dev/zeni-cloud-core/zeni-images/zeni-backend:v56`
- Deploy Cloud Run revision mới
- Run smoke test → Slack notify → tag git commit

---

## 5. Rollback Procedure

Khi deploy gây sự cố production:

```bash
# 1. List 5 revision gần nhất
gcloud run revisions list \
  --service=zeni-backend \
  --region=us-central1 \
  --limit=5

# Output ví dụ:
#   zeni-backend-00056-x7p   ← bị lỗi
#   zeni-backend-00055-k5b   ← muốn rollback về đây
#   zeni-backend-00054-...
```

2. Vào **Actions → Rollback Production → Run workflow**
3. Nhập `to_revision=zeni-backend-00055-k5b`
4. Nhập `reason="Login API 500 errors after v56 deploy"`
5. Run

Workflow sẽ:
- Verify revision tồn tại
- Snapshot revision hiện tại (cho audit)
- Update traffic 100% về revision đích
- Smoke test `/health`
- Slack notify

**Thời gian rollback typical: < 60 giây.**

---

## 6. Branch Protection Rules (khuyến nghị)

Vào **Settings → Branches → Add rule** cho branch `main`:

- ✅ Require a pull request before merging
- ✅ Require status checks to pass before merging
  - Required checks: `lint`, `security`, `test`, `build-check`
- ✅ Require branches to be up to date before merging
- ✅ Require conversation resolution before merging
- ✅ Do not allow bypassing the above settings
- ❌ Allow force pushes (KHÔNG bật)
- ❌ Allow deletions (KHÔNG bật)

---

## 7. Local Testing

Chạy thử pipeline local trước khi push:

```bash
cd backend

# Lint
pip install ruff black mypy
ruff check app
black --check app
mypy app --ignore-missing-imports

# Security
pip install bandit safety
bandit -r app -lll
safety check

# Tests (cần postgres local)
docker run -d --name zeni-test-pg \
  -e POSTGRES_USER=zeni_app \
  -e POSTGRES_PASSWORD=testpass \
  -e POSTGRES_DB=zeni_cloud \
  -p 5432:5432 postgres:16

export DATABASE_URL=postgresql+asyncpg://zeni_app:testpass@localhost:5432/zeni_cloud
export JWT_SECRET=test-secret
export VAULT_KEY=$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')

pip install -r requirements.txt pytest pytest-asyncio httpx
pytest tests/ -v
```

---

## 8. Monitoring & Alerts

Sau khi deploy, theo dõi:

| Tool | URL | Mục đích |
|---|---|---|
| Cloud Run logs | `gcloud run services logs read zeni-backend --region=us-central1` | Realtime logs |
| Error Reporting | https://console.cloud.google.com/errors?project=zeni-cloud-core | Group exceptions |
| Uptime check | Cloud Monitoring → Uptime checks → `zeni-health` | Ping `/health` 1 phút/lần |
| Slack `#zeni-deploys` | (cấu hình SLACK_WEBHOOK_URL) | Notify mỗi deploy/rollback |

---

## 9. Workflow Files — quick reference

```
.github/workflows/
├── ci.yml                  # PR + push CI gate (lint + security + tests + build)
├── deploy.yml              # Production deploy (build → push → deploy → smoke)
├── rollback.yml            # Manual rollback to specified revision
└── dependency-update.yml   # Weekly auto-PR for requirements.txt
```

---

## 10. Troubleshooting

**Workload Identity auth fails với "Permission denied"**
→ Check `gcloud iam service-accounts get-iam-policy github-deployer@...` đã có binding `principalSet://...attribute.repository/<owner>/<repo>` chưa.

**Smoke test fail nhưng app vẫn lên**
→ Cloud Run revision có thể chưa nhận traffic. Tăng `sleep` từ 30s lên 60s ở step "Smoke test".

**Slack notify không gửi**
→ `SLACK_WEBHOOK_URL` chưa add hoặc webhook bị Slack revoke. Step có guard `env.SLACK_WEBHOOK_URL != ''` nên sẽ skip thay vì fail.

**Dependency update PR conflict**
→ Close PR cũ, manual run lại workflow `Dependency update`.

**`git push origin v56` fail "tag exists"**
→ Tag đã tồn tại từ deploy trước. Manual workflow_dispatch và bump tag thủ công, hoặc xoá tag cũ: `git tag -d v56 && git push --delete origin v56`.

---

Generated for Zeni Cloud Core — questions ping CEO Cao Tuấn Phát hoặc deployer SA owner.
