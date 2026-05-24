# Re: Yêu cầu Zeni Cloud Support — Workspace `vietcontech` (P0)

**To:** CTO Viet Contech AI — `vietcontech.com@gmail.com`
**From:** Zeni Cloud Engineering (CTO Cao Tuấn — `doanhnhancaotuan@gmail.com`)
**Ngày:** 2026-05-24
**Re:** Ticket P0 — Unblock production go-live
**Severity acknowledged:** P0
**Resolution time:** ~30 phút kể từ khi ticket vào hệ thống Zeni

---

## 1. KẾT LUẬN — 4 service đã UNBLOCKED và LIVE

Ngay sau khi ticket vào, Zeni đã thao tác trực tiếp ở phía Zeni Cloud (Hướng A — KHÔNG cấp credential GCP ra ngoài tenant — xem mục 3 bên dưới). Hiện 4/4 service workspace `vietcontech` đều trả `HTTP 200`:

| Service | Cloud Run URL | HTTP | Response time | Body |
|---|---|---|---|---|
| `vct-backend-v2` | `https://zeni-vietcontech-vct-backend-v2-lijavjpb2a-as.a.run.app` | **200** | 1.96s cold | `{"ok":true,"service":"viet-contech-backend","version":"0.3.0","env":"production","mode":"real","db":{"tables":24}}` |
| `vct-backend-v3` | `https://zeni-vietcontech-vct-backend-v3-lijavjpb2a-as.a.run.app` | **200** | 2.15s cold | same |
| **`vct-backend-v4` (priority)** | `https://zeni-vietcontech-vct-backend-v4-lijavjpb2a-as.a.run.app` | **200** | 1.97s cold | same |
| `vct-backend-prod` | `https://zeni-vietcontech-vct-backend-prod-lijavjpb2a-as.a.run.app` | **200** | 0.33s warm | same |

Em (Viet Contech) có thể chạy E2E test ngay theo plan section 6 của ticket (login OTP, AI chat, phong thủy, PA chatbot, KTS upload) — Zeni KHÔNG cần hỗ trợ thêm cho phần verify.

---

## 2. Zeni đã làm gì cụ thể

### 2.1. v2 / v3 / v4 — Bind IAM (image đã đúng từ trước)

State Cloud Run thực tế ngay khi audit:
- v2, v3, v4: **image đã ĐÚNG** = `docker.io/thienmocduc/vct-backend:latest`
- v2, v3, v4: **thiếu IAM binding** `allUsers → roles/run.invoker` → đó là root cause 403 Forbidden

Lệnh đã chạy (account Zeni-side, region `asia-southeast1`):

```bash
for v in v2 v3 v4; do
  gcloud run services add-iam-policy-binding zeni-vietcontech-vct-backend-$v \
    --region=asia-southeast1 --member=allUsers --role=roles/run.invoker \
    --project=zeni-cloud-core
done
```

Kết quả:
- `v2`: etag `BwZSiXSxXdQ=`, role `roles/run.invoker` ✅
- `v3`: etag `BwZSiXTZFXA=`, role `roles/run.invoker` ✅
- `v4`: etag `BwZSiXUC28g=`, role `roles/run.invoker` ✅

### 2.2. prod — Update image (đang dùng placeholder)

State thực tế:
- `prod`: image SAI = `gcr.io/zeni-cloud-core/templates/express-hello:latest` (placeholder mặc định)
- `prod`: IAM `allUsers` đã có sẵn

Lệnh đã chạy:

```bash
gcloud run services update zeni-vietcontech-vct-backend-prod \
  --image=docker.io/thienmocduc/vct-backend:latest \
  --region=asia-southeast1 --project=zeni-cloud-core
```

Kết quả: revision mới `zeni-vietcontech-vct-backend-prod-00004-z4j` deployed và serving 100% traffic.

---

## 3. Về 3 Hướng đề xuất trong ticket

| Hướng | Trạng thái | Lý do |
|---|---|---|
| 🟢 **A — Drain + fix worker** | ✅ Đã làm (drain) | Phù hợp business model Zeni — Zeni vận hành hạ tầng, khách dùng dịch vụ |
| 🟡 **B — Mở 2 endpoint public API** | ⏳ Đang code | Sẽ deploy trong 24-48h. Chi tiết section 4 |
| 🔴 **C — Cấp GCP Service Account JSON** | ❌ Zeni TỪ CHỐI | Section 3.1 giải thích |

### 3.1. Vì sao Zeni TỪ CHỐI cấp Service Account JSON

Đây là chính sách của Zeni Cloud, không phải quyết định ad-hoc. Lý do:

**(a) Risk leak credential:** SA JSON là key vạn năng — chỉ cần 1 lần commit nhầm vào public GitHub repo (pattern phổ biến #1), bot scrape AI sẽ pickup trong 5-10 phút. Attacker có thể mine crypto trên Cloud Run của Zeni → hóa đơn ngàn USD/giờ, hoặc inject malicious image vào service `zeni-vietcontech-*` để MITM data khách hàng cuối của Viet Contech.

**(b) IAM Condition không bulletproof:** `resource.name.startsWith('zeni-vietcontech-')` KHÔNG apply cho mọi Cloud Run API. Một số API (logging, monitoring, services.list) bypass condition → tenant đọc được tên + log của workspace khác trong cùng GCP project.

**(c) Vi phạm business model:** Zeni Cloud là PaaS (Platform-as-a-Service). Khách dùng Zeni qua API/SDK/Dashboard của Zeni — không phải bypass xuống raw GCP infrastructure. Nếu khách dùng SA JSON trực tiếp, Zeni mất tracking, mất observability, không còn lý do tồn tại như "Cloud" mà chỉ là proxy GCP — và khách cũng mất luôn các tiện ích VAT VN, VietQR billing, AI Router, Council Opus, etc.

**(d) Compliance:** Audit log từ SA JSON chỉ ghi danh nghĩa SA, không truy được user/laptop nào dùng → khi có incident bảo mật, không thể truy trách nhiệm.

→ Thay vào đó, Zeni sẽ provide **public API endpoints** để Viet Contech tự deploy bằng PAT của workspace (không cần GCP access).

---

## 4. Endpoint API mới — Zeni đang code, sẽ deploy 24-48h

Sau khi đọc ticket section 2 (CTO Viet Contech đã thử 405/404 trên các endpoint), Zeni xác nhận thiếu endpoint trong public API. Đang implement:

### 4.1. `POST /api/v1/projects/{id}/image`

```http
POST /api/v1/projects/3edb8dc8-3abe-4648-96ae-450ba04783a8/image
Authorization: Bearer zeni_pat_<workspace_pat>
Content-Type: application/json

{
  "image": "docker.io/thienmocduc/vct-backend:sha-abc1234"
}
```

→ Trigger Cloud Run service update + verify pull access + tạo revision mới. Trả về:

```json
{
  "ok": true,
  "service": "zeni-vietcontech-vct-backend-v2",
  "revision": "zeni-vietcontech-vct-backend-v2-00003-xyz",
  "url": "https://...",
  "deployed_at": "2026-05-24T05:30:00Z"
}
```

### 4.2. `POST /api/v1/projects/{id}/redeploy`

```http
POST /api/v1/projects/{id}/redeploy
Authorization: Bearer zeni_pat_<workspace_pat>

{
  "env_override": {...}  // optional
}
```

→ Redeploy với image hiện tại (force pull mới nếu tag `:latest` cached).

### 4.3. `POST /api/v1/projects/{id}/visibility`

```http
POST /api/v1/projects/{id}/visibility
Authorization: Bearer zeni_pat_<workspace_pat>

{
  "public": true
}
```

→ Tự động bind/unbind IAM `allUsers → roles/run.invoker`.

**ETA:** 2026-05-26 00:00 ICT (48h). Em sẽ nhận email follow-up khi 3 endpoint live.

---

## 5. Vài lưu ý kỹ thuật bổ sung

### 5.1. ⚠️ Version mismatch — image `:latest` đang cached

Service trả `version:"0.3.0"` thay vì `0.4.0` mà em mong đợi. Root cause: Cloud Run pull image bằng tag `:latest`. Nếu Docker Hub đã có image mới push lên với cùng tag `:latest`, Cloud Run vẫn dùng SHA cũ đã pull (vì cache).

**Khuyến nghị:** Em rebuild + push với **commit SHA tag** thay vì `:latest`:

```bash
SHA=$(git rev-parse --short HEAD)
docker build -t docker.io/thienmocduc/vct-backend:$SHA -t docker.io/thienmocduc/vct-backend:latest .
docker push docker.io/thienmocduc/vct-backend:$SHA
docker push docker.io/thienmocduc/vct-backend:latest

# Sau đó dùng endpoint /image (khi Zeni deploy xong) hoặc tạm thời ping support:
# POST /api/v1/projects/{id}/image  body: {"image": "docker.io/thienmocduc/vct-backend:<sha>"}
```

### 5.2. 🔐 PAT trong ticket cần rotate

Em đã paste PAT `zeni_pat_gI8m9uWEWreUU7mAjUdf7bGR6nZP53i-a7AdGNxVOI8` trong section 4 của ticket — file ticket có thể được lưu hệ thống Zeni hoặc email logs. **Zeni khuyến nghị em rotate PAT này NGAY** sau khi verify E2E xong:

```
POST /api/v1/api-tokens
Authorization: Bearer <token_cũ>
Body: {"name": "viet-contech-prod-2026-05-24-rotated"}

→ Trả PAT mới. Sau đó:
DELETE /api/v1/api-tokens/<token_id_cũ>
```

### 5.3. 🔧 Root cause bug worker (Zeni phía sau)

Zeni xác nhận có bug trong reconciler:
- State DB Zeni báo `status: failed` nhưng Cloud Run thực tế đã `RUNNING` với image đúng
- UI "Redeploy" trigger không thực gửi request

Em (Zeni) đang fix trong sprint hiện tại. Khi 3 endpoint /image, /redeploy, /visibility live (mục 4), bug worker cũng được fix theo.

---

## 6. Tickets cũ liên quan (section 5 ticket)

Zeni đã ghi nhận 5 ticket cũ:
- `ZENI_TICKET_AI_GATEWAY_422.md`
- `ZENI_TICKET_GOOGLE_OAUTH.md`
- `ZENI_TICKET_SMTP.md` / `_v2.md`
- `ZENI_TICKET_ZALO_SSO.md`
- `ZENI_MASTER_REQUEST_2026-05-20.md`

→ Zeni sẽ xử lý theo thứ tự ưu tiên sau khi P0 này (Cloud Run unblock) verify ổn từ phía Viet Contech. Email follow-up sẽ có trong 48h.

---

## 7. Action cho Viet Contech (em làm)

1. ✅ **Verify E2E trong vòng 1h** trên `https://zeni-vietcontech-vct-backend-v4-lijavjpb2a-as.a.run.app/api/health` (đã 200) + các luồng business: login OTP, AI chat, phong thủy, PA chatbot, KTS upload
2. ✅ **Set 2 secret còn thiếu** qua dashboard Zeni (`/app` → Projects → vct-backend-v4 → Env vars):
   - `SMTP_PASS`
   - `GOOGLE_CLIENT_SECRET`
3. ⏳ **Rebuild image với SHA tag** (mục 5.1) — chỉ khi cần version 0.4.0 thực tế
4. ⏳ **Rotate PAT** (mục 5.2) — sau khi E2E pass
5. 📧 **Báo Chairman** (`doanhnhancaotuan@gmail.com`) kết quả E2E

---

## 8. Liên hệ tiếp

- **Email:** `doanhnhancaotuan@gmail.com` (CC: `caotuanphat581@gmail.com` — operator GCP)
- **Channel ưu tiên:** Email trực tiếp + ticket trong workspace `vietcontech`
- **SLA endpoint mới:** 48h
- **SLA ticket cũ:** sẽ ETA trong email follow-up

---

**Ký:** Cao Tuấn — CTO Zeni Cloud Core
**Resolved by:** Zeni Cloud Engineering — 2026-05-24 05:30 ICT
**Resolution method:** Hướng A (Zeni-side fix, no credential exposed)
