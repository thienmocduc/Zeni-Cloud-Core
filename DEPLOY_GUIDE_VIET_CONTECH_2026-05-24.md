# Zeni Cloud — Deploy Guide cho Workspace `vietcontech`
**Version:** 1.0 · 2026-05-24
**Audience:** CTO Viet Contech AI / `vietcontech.com@gmail.com`
**Re:** Ticket P0 Cloud Run unblock + self-service deploy

---

## 1. Tổng quan — 3 endpoint mới Zeni cấp cho em

Sau ticket P0 ngày 24/05, Zeni đã code thêm 3 endpoint vào Public API để workspace tự deploy bằng PAT, KHÔNG cần Service Account JSON GCP (theo policy Zeni).

| Endpoint | Mục đích | Quyền tối thiểu |
|---|---|---|
| `POST /api/v1/projects/{id}/image` | Update Cloud Run image → new revision | Developer |
| `POST /api/v1/projects/{id}/redeploy` | Redeploy current image (force pull) | Developer |
| `POST /api/v1/projects/{id}/visibility` | Bật/tắt public (allUsers IAM) | Admin |

**Trạng thái:** ✅ Đã code, đã pass `python -m py_compile`. Đang chờ deploy lên Cloud Run (ETA sau khi Zeni release git lock + CI/CD chạy, ~2-4h). Em sẽ nhận email khi 3 endpoint LIVE.

**API base URL:**
- Production: `https://zenicloud.io`
- Backend Cloud Run trực tiếp: `https://zeni-backend-1026010018600.us-central1.run.app`

---

## 2. Authentication

Mọi request dùng PAT của workspace `vietcontech`:

```http
Authorization: Bearer zeni_pat_<token>
```

> ⚠️ PAT em đang dùng (`zeni_pat_gI8m9uWE…OI8`) đã paste trọn vẹn trong ticket → đề nghị **rotate ngay sau khi verify E2E** (mục 5 file này).

---

## 3. Project IDs em đang có

| Project Name | Project ID | Cloud Run Service |
|---|---|---|
| vct-backend-v2 | `3edb8dc8-3abe-4648-96ae-450ba04783a8` | zeni-vietcontech-vct-backend-v2 |
| vct-backend-v3 | `901ccbb8-7279-4c0f-a684-876ce175db23` | zeni-vietcontech-vct-backend-v3 |
| **vct-backend-v4** | `463c6e8a-a24e-4086-b477-d337a9d93fb2` | zeni-vietcontech-vct-backend-v4 |
| vct-backend-prod | `dff6ffb2-bddb-42e5-acbf-6c820bf212fc` | zeni-vietcontech-vct-backend-prod |

---

## 4. Use case + cURL example

### 4.1. Update image (build mới với SHA tag)

**Scenario:** Em build `vct-backend:0.4.0` (commit SHA `abc1234`), push Docker Hub. Cần Cloud Run pull image mới và tạo revision mới.

```bash
# Step 1: Build + push
SHA=$(git rev-parse --short HEAD)   # vd: abc1234
docker buildx build --platform linux/amd64 \
  -t docker.io/thienmocduc/vct-backend:$SHA \
  -t docker.io/thienmocduc/vct-backend:0.4.0 \
  --push .

# Step 2: Call Zeni API
ZENI_PAT='zeni_pat_<your_rotated_token>'
PROJECT_ID='463c6e8a-a24e-4086-b477-d337a9d93fb2'   # v4

curl -X POST https://zenicloud.io/api/v1/projects/$PROJECT_ID/image \
  -H "Authorization: Bearer $ZENI_PAT" \
  -H "Content-Type: application/json" \
  -d "{\"image\": \"docker.io/thienmocduc/vct-backend:$SHA\"}" \
  -G --data-urlencode "ws=vietcontech"
```

**Expected response (202 Accepted):**

```json
{
  "ok": true,
  "project_id": "463c6e8a-...",
  "service": "zeni-vietcontech-vct-backend-v4",
  "image_new": "docker.io/thienmocduc/vct-backend:abc1234",
  "status": "deploying",
  "poll_url": "/api/v1/projects/463c6e8a-...?ws=vietcontech"
}
```

**Step 3: Poll status (2-3s/lần) đến khi `status: "running"`:**

```bash
while true; do
  STATUS=$(curl -sS -H "Authorization: Bearer $ZENI_PAT" \
    "https://zenicloud.io/api/v1/projects/$PROJECT_ID?ws=vietcontech" \
    | jq -r .status)
  echo "Status: $STATUS"
  [ "$STATUS" = "running" ] && break
  [ "$STATUS" = "failed" ] && { echo "DEPLOY FAILED"; exit 1; }
  sleep 3
done
```

### 4.2. Redeploy (force pull image hiện tại)

**Scenario:** Em đã push lại `:latest` tag với content mới, Cloud Run đang cache SHA cũ → cần force new revision.

```bash
curl -X POST "https://zenicloud.io/api/v1/projects/$PROJECT_ID/redeploy?ws=vietcontech" \
  -H "Authorization: Bearer $ZENI_PAT" \
  -H "Content-Type: application/json" \
  -d '{}'
```

**Với env_override** (tạm thay đổi vài env vars cho revision mới):

```bash
curl -X POST "https://zenicloud.io/api/v1/projects/$PROJECT_ID/redeploy?ws=vietcontech" \
  -H "Authorization: Bearer $ZENI_PAT" \
  -H "Content-Type: application/json" \
  -d '{
    "env_override": {
      "LOG_LEVEL": "debug",
      "FEATURE_FLAG_X": "true"
    }
  }'
```

### 4.3. Visibility toggle (public/private)

**Bật public (allUsers → roles/run.invoker):**

```bash
curl -X POST "https://zenicloud.io/api/v1/projects/$PROJECT_ID/visibility?ws=vietcontech" \
  -H "Authorization: Bearer $ZENI_PAT" \
  -H "Content-Type: application/json" \
  -d '{"public": true}'
```

**Tắt public:**

```bash
curl -X POST "https://zenicloud.io/api/v1/projects/$PROJECT_ID/visibility?ws=vietcontech" \
  -H "Authorization: Bearer $ZENI_PAT" \
  -H "Content-Type: application/json" \
  -d '{"public": false}'
```

**Response:**

```json
{
  "ok": true,
  "project_id": "...",
  "service": "zeni-vietcontech-vct-backend-v4",
  "public": true,
  "url": "https://zeni-vietcontech-vct-backend-v4-..."
}
```

---

## 5. Sequence Em làm sau khi 3 endpoint LIVE

### 5.1. Verify hiện tại (làm NGAY — không đợi endpoint mới)

4 service đã unblock, em chạy E2E test trên `v4`:

```bash
# Health check
curl https://zeni-vietcontech-vct-backend-v4-lijavjpb2a-as.a.run.app/api/health
# Expected: {"ok":true,"service":"viet-contech-backend","version":"0.3.0",...}

# Login OTP, AI chat, phong thủy, PA chatbot, KTS upload — chạy theo plan section 6 ticket
```

### 5.2. Set 2 secret còn thiếu (qua endpoint env hiện có)

```bash
curl -X POST "https://zenicloud.io/api/v1/projects/$PROJECT_ID/env?ws=vietcontech" \
  -H "Authorization: Bearer $ZENI_PAT" \
  -H "Content-Type: application/json" \
  -d '{
    "env_vars": {
      "SMTP_PASS": "<gmail_app_password>",
      "GOOGLE_CLIENT_SECRET": "<gcp_oauth_secret>"
    }
  }'

# Sau đó redeploy để env mới có hiệu lực (sau khi /redeploy LIVE):
curl -X POST "https://zenicloud.io/api/v1/projects/$PROJECT_ID/redeploy?ws=vietcontech" \
  -H "Authorization: Bearer $ZENI_PAT"
```

### 5.3. Rebuild với version 0.4.0 (sau khi /image LIVE)

```bash
SHA=$(git rev-parse --short HEAD)
docker buildx build --platform linux/amd64 \
  -t docker.io/thienmocduc/vct-backend:$SHA \
  -t docker.io/thienmocduc/vct-backend:0.4.0 \
  --push .

curl -X POST "https://zenicloud.io/api/v1/projects/$PROJECT_ID/image?ws=vietcontech" \
  -H "Authorization: Bearer $ZENI_PAT" \
  -H "Content-Type: application/json" \
  -d "{\"image\": \"docker.io/thienmocduc/vct-backend:$SHA\"}"
```

### 5.4. Rotate PAT (security cleanup)

PAT cũ đã leak trong ticket section 4 → cần tạo PAT mới + revoke cũ:

```bash
# Tạo PAT mới
NEW_PAT_RESPONSE=$(curl -sS -X POST "https://zenicloud.io/api/v1/api-tokens?ws=vietcontech" \
  -H "Authorization: Bearer zeni_pat_gI8m9uWE...OI8" \
  -H "Content-Type: application/json" \
  -d '{"name": "vietcontech-prod-2026-05-24-rotated", "scopes": ["full"]}')

NEW_PAT=$(echo $NEW_PAT_RESPONSE | jq -r .token)
OLD_PAT_ID=$(curl -sS -H "Authorization: Bearer $NEW_PAT" \
  "https://zenicloud.io/api/v1/api-tokens?ws=vietcontech" \
  | jq -r '.tokens[] | select(.name | contains("vietcontech-prod")) | select(.name | contains("rotated") | not) | .id')

# Revoke PAT cũ
curl -X DELETE -H "Authorization: Bearer $NEW_PAT" \
  "https://zenicloud.io/api/v1/api-tokens/$OLD_PAT_ID?ws=vietcontech"

echo "NEW PAT (lưu vào GitHub Secrets ZENI_TOKEN): $NEW_PAT"
```

---

## 6. Error responses tham khảo

| HTTP | Khi nào | Hành động |
|---|---|---|
| `400` | Image không trong whitelist | Vào Workspace Settings → Image Registries → Add prefix `docker.io/thienmocduc/` |
| `400` | Project chưa có image (cho /redeploy) | Gọi /image trước |
| `403` | PAT không đủ quyền | Cần Developer (image/redeploy) hoặc Admin (visibility) |
| `404` | Project ID sai hoặc chưa deploy | Check `/api/v1/projects?ws=vietcontech` để lấy ID đúng |
| `422` | Image không tồn tại trong registry | Build + push image trước, hoặc dùng Build Farm |
| `429` | Rate limit (10 deploy/min) | Lùi lại |
| `500` | Cloud Run / IAM lỗi | Báo `support@zenicloud.io` kèm correlation_id từ response header |

---

## 7. Code chính trong response (cho em verify)

3 endpoint được implement trong `backend/app/api/projects.py` (commit pending), endpoint logic:

- **`/image`**: validate image (global whitelist + per-workspace) → validate exists in registry → mark `status=deploying` → background `_bg_deploy()` với image mới (giữ env hiện có) → audit log `compute.image_update`.
- **`/redeploy`**: reuse `image` hiện tại → mark deploying → background deploy với optional `env_override` → audit `compute.redeploy`.
- **`/visibility`**: gọi Cloud Run IAM API `get_iam_policy` + `set_iam_policy` để bind/unbind `allUsers → roles/run.invoker` → audit `compute.visibility` severity warning (vì sensitive).

Tất cả audit log đẩy vào `audit_logs` table workspace để em trace bằng `GET /api/v1/audit?ws=vietcontech`.

---

## 8. Limitations rõ ràng

- Image whitelist mặc định: `docker.io/library/*`, Artifact Registry zeni-cloud-core, gcr.io samples. Image custom (`docker.io/thienmocduc/vct-backend`) cần workspace owner tự add prefix qua Settings UI hoặc API:
  ```bash
  curl -X POST "https://zenicloud.io/api/v1/workspace-whitelist?ws=vietcontech" \
    -H "Authorization: Bearer $ZENI_PAT" \
    -d '{"prefix": "docker.io/thienmocduc/", "enabled": true}'
  ```
- `_bg_deploy()` chạy max 60s; nếu Cloud Run revision > 60s tạo, status sẽ vẫn báo `running` nhưng revision có thể chưa serving 100% — em poll thêm 30-60s.
- Image pull từ Docker Hub private requires Cloud Run service-account credential — hiện chỉ public images works tự động.

---

## 9. Liên hệ + escalation

- **Email:** `caotuanphat581@gmail.com` (Chairman ops account), CC `doanhnhancaotuan@gmail.com`
- **Workspace Slack:** chưa có (Zeni roadmap Q3)
- **Ticket tracking:** mặc định đẩy vào `audit_logs` workspace `vietcontech`

**Khi 3 endpoint LIVE, Zeni sẽ gửi email confirm.** Trong lúc chờ, em chạy 5.1 (verify E2E hiện tại) + 5.2 (set 2 secret) → workspace go-live được ngay.

---

**Ký:** Cao Tuấn — CTO Zeni Cloud Core
**Issued:** 2026-05-24 05:45 ICT
