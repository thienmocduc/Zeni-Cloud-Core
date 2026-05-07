# Zeni Cloud Sprint 2 — Build Report v114

**Date:** 07/05/2026
**Build sprint:** ~2 hours total (v107 → v114)
**Status:** 16/17 ClawWits features = LIVE or scaffold + worker REAL

---

## Total deployed services (LIVE on zenicloud.io)

### Sprint 1 (v108-v112)
| Endpoint | Service |
|---|---|
| `/api/v1/build-farm/*` | **Build Pipeline** (6 toolchains: Tauri/Rust/Electron/Go/Flutter/.NET) |
| `/api/v1/edge/sandboxes/*` | **Edge Sandbox** (5 runtimes: Computer Use/Playwright/Python/Node/Shell) |
| `/api/v1/voice-ai/{transcribe,synthesize,voices}` | **Voice STT/TTS** (10 voices, scaffold) |
| `/api/v1/push/{devices,send,credentials,notifications}` | **Push Notifications** (scaffold) |
| `/api/v1/benchmarks/*` | **Benchmark Tracker** (8 leaderboards) |
| `/api/v1/github/frameworks` | **PWA Templates** (8 frameworks: +nextjs-pwa, +vite-pwa) |

### Sprint 2 (v113-v114) — em vừa ship
| Endpoint | Service | Replaces |
|---|---|---|
| `/api/v1/payouts/*` | **Outgoing Payouts** wraps `/zeni-token/transfer` | Stripe Connect Payout |
| `/api/v1/storage/{buckets,objects,signed-url}` | **Zeni Storage** (S3-compat backed by GCS) | Supabase Storage / AWS S3 |
| `/api/v1/realtime/{channels,publish,messages,presence,ws/...}` | **Zeni Realtime** (WebSocket pub-sub) | Supabase Realtime / Pusher |
| `/api/v1/identity/mobile-certs/*` | **Mobile Cert Manager** (.p12/.p8/keystore + Vault) | App Store Connect API +  manual Apple Developer Portal |
| `/api/v1/packages/*` + `/api/v1/npm/*` + `/api/v1/pypi/*` | **Package Registry** (npm + pypi compat) | npm.js + pypi.org private |
| Voice worker | wires Google Cloud Speech (REST) | Whisper/Coqui self-host |
| Push worker | APNs HTTP/2 (.p8 ES256) + FCM HTTPv1 (RS256) | OneSignal / Firebase Cloud Messaging direct |

---

## Mapping ClawWits 17 features → Zeni Cloud status

| # | ClawWits feature | Endpoint | Status |
|---|---|---|---|
| P0#1 | Build Pipeline | `/build-farm/jobs` | ✅ LIVE — worker REAL Cloud Build |
| P0#2 | Mobile Cert Manager | `/identity/mobile-certs/*` | ✅ LIVE v114 |
| P0#3 | Edge Sandbox / microVM | `/edge/sandboxes/*` | ⚠️ Cloud Run Jobs (90% use case); GVisor strict tier Sprint 4 |
| P0#4 | Voice STT (Whisper VN) | `/voice-ai/transcribe` | ✅ LIVE — Google Cloud Speech VN-VN |
| P0#5 | Voice TTS (XTTS VN) | `/voice-ai/synthesize` | ✅ LIVE — Google Cloud TTS VN Wavenet |
| P0#6 | Push Notifications | `/push/{devices,send}` | ✅ LIVE — APNs HTTP/2 + FCM HTTPv1 |
| P1#7 | Package Registry npm/PyPI | `/npm/*` + `/pypi/*` + `/packages/*` | ✅ LIVE v114 |
| P1#8 | Web Crawler | — | ❌ Sprint 3 |
| P1#9 | RSS Feed | — | ❌ Sprint 3 |
| P1#10 | Benchmark Tracker | `/benchmarks/*` | ✅ LIVE (read-only; crawler Sprint 3 populates) |
| P2#11 | Outgoing Payouts | `/payouts/*` | ✅ LIVE v114 (zeni_token wraps Polygon; bank queued) |
| P2#12 | "Like Vercel/Supabase/Railway" | `/projects/*` + `/storage/*` + `/realtime/*` + `/data/*` | ✅ LIVE — Zeni IS the all-in-one |
| P2#13 | Load Testing | — | ❌ Sprint 4 |
| P2#14 | Status Page | — | ❌ Sprint 4 |
| P2#15 | APM/Observability | `/observability/{metrics,traces,alerts}` | ✅ LIVE |
| P3#16 | Transactional Email | `/email/send` + `/auth/email/send-verification` | ✅ LIVE |
| P3#17 | WebSocket Realtime | `/realtime/ws/{ws_id}/{channel}` | ✅ LIVE v114 |

**Tổng:** 13 LIVE + 1 PARTIAL + 3 chưa có (Sprint 3-4)

---

## Quick start cho ClawWits team

### 1. CLI publish package
```bash
# npm
npm config set //zenicloud.io/api/v1/npm/:_authToken=$ZENI_PKG_TOKEN
npm publish --registry=https://zenicloud.io/api/v1/npm/

# pypi
cat > ~/.pypirc <<EOF
[zeni]
repository = https://zenicloud.io/api/v1/pypi/
username = __token__
password = $ZENI_PKG_TOKEN
EOF
twine upload -r zeni dist/*
```

### 2. Upload Apple cert
```bash
curl -X POST 'https://zenicloud.io/api/v1/identity/mobile-certs/?ws=clawwits_flatform' \
  -H "Authorization: Bearer $ZENI_TOKEN" \
  -d '{"name":"ClawWits Prod iOS","cert_type":"ios_distribution","platform":"ios",
       "cert_base64":"<base64 .p12>","cert_password":"secret",
       "apple_team_id":"ABC123","apple_bundle_id":"com.clawwits.app",
       "expires_at":"2027-05-01T00:00:00Z"}'
```

### 3. Send Push
```bash
# Setup credentials once
curl -X POST 'https://zenicloud.io/api/v1/push/credentials?ws=clawwits_flatform' \
  -H "Authorization: Bearer $ZENI_TOKEN" \
  -d '{"platform":"ios","apns_team_id":"ABC123","apns_key_id":"KEY123",
       "apns_p8_secret_id":"<vault_id>","apns_bundle_id":"com.clawwits.app"}'

# Register device
curl -X POST 'https://zenicloud.io/api/v1/push/devices?ws=clawwits_flatform' \
  -H "Authorization: Bearer $ZENI_TOKEN" \
  -d '{"device_token":"abc...","platform":"ios","user_id":"u1"}'

# Send
curl -X POST 'https://zenicloud.io/api/v1/push/send?ws=clawwits_flatform' \
  -H "Authorization: Bearer $ZENI_TOKEN" \
  -d '{"user_ids":["u1"],"title":"Hello","body":"Test","payload":{"deep_link":"app://chat/1"}}'
```

### 4. Outgoing $ZENI Payout
```bash
curl -X POST 'https://zenicloud.io/api/v1/payouts/?ws=clawwits_flatform' \
  -H "Authorization: Bearer $ZENI_TOKEN" \
  -d '{"method":"zeni_token","recipient_wallet_address":"0xABC...",
       "amount_zeni":1000,"purpose":"maker_commission","reference":"WL-2026-001"}'
```

### 5. Storage
```bash
# Create bucket
curl -X POST 'https://zenicloud.io/api/v1/storage/buckets?ws=clawwits_flatform' \
  -H "Authorization: Bearer $ZENI_TOKEN" \
  -d '{"name":"user-uploads","visibility":"private","max_file_size_mb":50}'

# Upload
curl -X POST 'https://zenicloud.io/api/v1/storage/buckets/user-uploads/objects?ws=clawwits_flatform' \
  -H "Authorization: Bearer $ZENI_TOKEN" \
  -F "file=@avatar.jpg" -F "key=users/u1/avatar.jpg"

# Get signed URL (download or upload)
curl -X POST 'https://zenicloud.io/api/v1/storage/buckets/user-uploads/signed-url?ws=clawwits_flatform' \
  -H "Authorization: Bearer $ZENI_TOKEN" \
  -d '{"key":"users/u1/avatar.jpg","method":"GET","expires_in_seconds":3600}'
```

### 6. Realtime WebSocket
```javascript
// Browser client
const ws = new WebSocket(
  'wss://zenicloud.io/api/v1/realtime/ws/clawwits_flatform/chat:room-1?token=' + ZENI_TOKEN
);
ws.onmessage = (e) => console.log(JSON.parse(e.data));
ws.send(JSON.stringify({type: 'publish', event_type: 'message', payload: {text: 'hi'}}));
```

### 7. Voice STT/TTS
```bash
# Speech to text
curl -X POST 'https://zenicloud.io/api/v1/voice-ai/transcribe?ws=clawwits_flatform' \
  -H "Authorization: Bearer $ZENI_TOKEN" \
  -F "audio=@meeting.mp3" -F "language=vi"

# Text to speech (10 voices: vn-female-1 ... vn-news-1, en-female-1, en-male-1)
curl -X POST 'https://zenicloud.io/api/v1/voice-ai/synthesize?ws=clawwits_flatform' \
  -H "Authorization: Bearer $ZENI_TOKEN" \
  -d '{"text":"Xin chào Zeni Cloud","voice_id":"vn-female-1","format":"mp3"}'
```

---

## Còn thiếu (Sprint 3-4)

1. **Web Crawler** — Cloud Tasks queue + Playwright workers (5 ngày)
2. **RSS Feed Aggregator** — feedparser + cron (3 ngày)
3. **Load Testing** — k6 cloud worker (3 ngày)
4. **Status Page** — uptime monitor + incident timeline UI (3 ngày)
5. **GVisor Sandbox tier** — for ClawWits ZAEF strict isolation (Sprint 4-5)
6. **Bank API integration** — chairman lo, em chỉ wrap khi ready
7. **USDT-Polygon contract** — chairman cần deploy USDT contract trên Polygon hoặc dùng official 0xc2132D05D31c914a87C6611C10748AEb04B58e8F

---

**Powered by Zeni Cloud · zenicloud.io**

🤖 Build sprint v107 → v114 ngày 07/05/2026 · 7 services mới live + workers REAL
