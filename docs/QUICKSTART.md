# 🚀 Zeni Cloud · Quick Start (Vietnamese)

Bản hướng dẫn nhanh cho dev mới — chạy được trong 10 phút.

## 1. Đăng ký tài khoản

Truy cập **https://zenicloud.io/signup** điền form:
- Họ tên
- Email công ty
- Mật khẩu (≥8 ký tự, có chữ thường + chữ HOA hoặc số)
- Tên công ty (sẽ tạo workspace)

→ Nhận 50.000đ tín dụng tự động + 5 agent runs + 5 ảnh + 50K tokens free.

## 2. Lấy API Token

Login → `/app` dashboard → **Settings → API Tokens → New Token**:
- Name: "My Production Token"
- Scopes: `ai,data` (hoặc `full` cho admin)
- Expires: 365 ngày

→ Copy token `zeni_pat_xxx...` (chỉ trả 1 lần — lưu ngay vào secrets manager).

## 3. Cài SDK

### Node.js / TypeScript
```bash
npm install @zenicloud/sdk
```

### Python
```bash
pip install zenicloud
```

### cURL
Không cần SDK — gọi trực tiếp REST API.

## 4. Hello World — gọi AI

### Node.js
```typescript
import { ZeniCloud } from '@zenicloud/sdk';

const zeni = new ZeniCloud({ token: process.env.ZENI_TOKEN!, workspace: 'my_company' });

const result = await zeni.ai.complete({
  model: 'gemini-2.5-flash',
  prompt: 'Liệt kê 5 phong cách thiết kế nội thất 2026',
  max_tokens: 500,
});
console.log(result.output);
```

### Python
```python
from zenicloud import ZeniCloud

zeni = ZeniCloud(workspace='my_company')  # tự đọc ZENI_TOKEN env
print(zeni.ai.complete(prompt='Hello', model='gemini-2.5-flash')['output'])
```

### cURL
```bash
curl -X POST "https://zenicloud.io/api/v1/ai/complete?ws=my_company" \
  -H "Authorization: Bearer $ZENI_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-2.5-flash","prompt":"Hello","max_tokens":500}'
```

## 5. Use cases mẫu

### A. Generate ảnh thiết kế nội thất (Imagen 3)

```python
img = zeni.ai.generate_image(
    prompt='Tropical Modern living room 80m2, golden hour, photorealistic 8K',
    aspect_ratio='16:9', n=2,
)
data_uri = img['images'][0]['data_uri']  # base64 PNG
# Cost: $0.04 × 2 = $0.08 (~2.000đ)
```

### B. KTS agent đề xuất concept biệt thự (60s, 11.400đ)

```python
result = zeni.agents.architecture.run(
    brief='Biet thu 3 tang TP HCM 200m2 dat 12x16m, gia dinh 6 nguoi, Tropical Modern',
    generate_renders=True, n_renders=2,
    constraints={'area_m2': 200, 'floors': 3, 'budget_vnd': 8_000_000_000},
)
print(result['concept'])         # KTS-level brief 4000+ chars
print(result['critique'])        # Self-review 3 ưu / 3 nhược / 3 cải tiến
print(len(result['renders']), 'ảnh render Imagen 3')
```

### C. Phân tích ảnh tham khảo (Multimodal Gemini)

```python
analysis = zeni.ai.analyze_image(
    prompt='Phan tich phong cach: vat lieu, mau sac, mood',
    image_url='https://example.com/reference.jpg',
)
print(analysis['output'])
```

### D. Streaming chat (UX realtime)

```python
for chunk in zeni.ai.stream(prompt='Mô tả 100 chữ về Indochine style'):
    print(chunk, end='', flush=True)
```

### E. Deploy app lên Cloud Run

```python
proj = zeni.projects.deploy(
    name='my-nextjs',
    image='us-central1-docker.pkg.dev/zeni-cloud-core/zeni-images/my-nextjs:v1',
    size='m', port=3000,
    env_vars={'NODE_ENV': 'production'},
)
# Async — Cloud Run service tạo trong ~30-60s
# Poll status:
import time
while True:
    p = zeni.projects.get(proj['id'])
    if p['status'] == 'running':
        print('LIVE at', p['domain']); break
    time.sleep(5)
```

### F. SQL trên Cloud SQL workspace schema

```python
# CREATE
zeni.data.sql('''
    CREATE TABLE IF NOT EXISTS orders (
        id BIGSERIAL PRIMARY KEY,
        customer_email TEXT, total_vnd BIGINT, status TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )
''')
# INSERT
zeni.data.sql("INSERT INTO orders(customer_email, total_vnd) VALUES('a@b.com', 500000)")
# SELECT
rows = zeni.data.sql('SELECT * FROM orders ORDER BY id DESC LIMIT 10')
```

### G. Setup cron (Cloud Scheduler)

```python
zeni.automation.create_cron(
    name='daily-report',
    schedule='0 9 * * *',  # 9 AM hàng ngày
    target_url='https://my-app.run.app/api/cron/daily-report',
    method='POST',
    timezone='Asia/Ho_Chi_Minh',
)
```

### H. Webhook + retry/DLQ

```python
# Setup connector
conn = zeni.automation.add_connector(
    type='webhook',
    config={'url': 'https://my-app.com/webhooks/payos', 'secret_token': 'shared'},
)
# Fire event → auto retry nếu fail
zeni.automation.fire_event(
    source='payos', action='payment.success',
    payload={'order_id': 123, 'amount': 500000},
)
# List DLQ failed
dlq = zeni.automation.list_webhook_attempts(status='dlq')
```

## 6. Quota & Pricing

| Tier | Giá/tháng | Quota |
|------|-----------|-------|
| Free | 0đ | 5 runs + 5 ảnh + 50K tokens |
| Starter | 500.000đ | 50 runs + 100 ảnh + 500K tokens |
| Pro ⭐ | 2.000.000đ | 300 runs + 500 ảnh + 5M tokens |
| Business | 6.000.000đ | 1.500 runs + 2.000 ảnh + 20M tokens |
| Enterprise | thoả thuận | unlimited |

Vượt quota → tính theo giá Pay-as-you-go (xem `/billing/price-book`).

## 7. Error handling

| HTTP | Ý nghĩa | Fix |
|------|---------|-----|
| 401 | Token sai | Renew token |
| 402 | Hết quota / wallet | Top-up hoặc upgrade tier |
| 403 | Token thiếu scope | Tạo token với scope đúng |
| 422 | Body invalid | Đọc detail field-by-field |
| 429 | Rate limit | Retry sau 60s |
| 502 | Vertex AI / Imagen lỗi | Retry sau 30s |

## 8. Dashboard + monitoring

- **App dashboard:** https://zenicloud.io/app
- **API docs:** https://zenicloud.io/docs (Swagger interactive)
- **Cost dashboard:** https://zenicloud.io/api/v1/billing/dashboard/summary
- **Status page:** https://status.zenicloud.io (sắp có)

## 9. Support

- Email: caotuanphat581@gmail.com
- Docs: https://zenicloud.io/docs
- Issues: GitHub repo của SDK

---

**Chúc anh em build sản phẩm AI tuyệt vời 🚀**
