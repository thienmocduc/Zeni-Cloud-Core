# 🥷 ZENI RED TEAM — Strategic Blueprint v1

**Status**: 📦 LOCKED — Build sau khi Zeni Cloud GA xong
**Owner**: Chairman Thiên Mộc Đức (CEO Zeni Holdings)
**Created**: 2026-05-03
**Build start**: TBD (sau khi Zeni Cloud Sprint A12+ hoàn thành)
**Estimated effort**: 3-12 tháng phụ thuộc scope (MVP 3 agents = 3 ngày, full = 12 tuần)

---

## 🎯 Vision

Zeni Cloud KHÔNG outsource bảo mật. Tự build:
1. **Tự hack mình** (Red Team AI Black Hat) trước khi hacker thật làm
2. **Tự bảo vệ mình** (Blue Team AI defenders auto-patch)
3. **Bán dịch vụ này cho khách** ("Pentera VN" — $99-2999/tháng/customer)

ROI: -$50K dev → +$600K/năm potential revenue (100 customers × $499/tháng).

Cạnh tranh: Pentera ($100K/năm), AttackIQ ($85K/năm), HackerOne ($30K + 20% bounty).
**Zeni rẻ hơn 10x, dùng AI thay manual pentester, tích hợp sẵn Cloud → moat lớn.**

---

## 🥷 RED TEAM — 8 AI Black Hat Agents (Claude Sonnet 4.6)

| Agent | Specialty | Attack vectors |
|-------|-----------|----------------|
| 🐈‍⬛ **BlackCat** | SQL Injection | UNION, blind, time-based, NoSQL |
| 👻 **Phantom** | XSS/CSRF | Stored, reflected, DOM, CSP bypass |
| 💀 **Ghost** | Social Engineering | Phishing email, fake OTP, vishing |
| 🪦 **Wraith** | API Abuse | IDOR, BOLA, mass assignment, rate-limit bypass |
| 🌑 **Shadow** | Cloud Misconfig | Public S3, IAM wrong, exposed metadata API |
| 👤 **Specter** | Supply Chain | npm/pip typosquat, dep confusion |
| 🦇 **Nightmare** | Brute Force | Credential stuffing, password spray, 2FA bypass |
| ☠️ **Reaper** | Zero-Day Research | CVE-watch, fuzzing, novel exploit chains |

**Mỗi agent có:**
- ✅ Memory (học từ attack thành công + thất bại trước)
- ✅ CVE database access (auto-fetch CVE mới, thử exploit)
- ✅ Sandbox (GCP project riêng `zeni-redteam-sandbox`, KHÔNG chạm prod data)
- ✅ Personality prompt (mỗi agent có style riêng)
- ✅ Reward function (gain "score" → leaderboard internal)
- ✅ Cron schedule (run mỗi 30-60 phút 1 attack scenario)

---

## 🛡️ BLUE TEAM — 4 AI Defender Agents

| Agent | Role |
|-------|------|
| 🛡️ **Sentinel** | Real-time WAF rule generator từ Red findings |
| 🔧 **Guardian** | Auto-patch suggester → tạo PR GitHub auto |
| 👁️ **Watcher** | Log analysis + alert anomaly |
| ✚ **Healer** | Auto-remediate (rotate keys, block IP, suspend user) |

---

## 📊 24/7 Workflow

```
[00:00] Cron trigger → spawn Red Agent BlackCat
        BlackCat: "Hôm nay tôi sẽ thử SQLi vào /api/v1/projects?ws=' OR 1=1--"
        → Run attack via httpx
        → Get response, analyze
        → Nếu thấy data leak → Generate finding report
        → Insert vào red_team_findings (severity: CRITICAL)

[00:15] Blue Agent Sentinel pickup finding:
        → Generate Cloud Armor rule
        → Auto-deploy WAF rule (with human approval option)
        → Notify dev team Slack

[00:30] Guardian agent analyzes finding:
        → Open GitHub issue
        → Suggest code patch (pseudocode in PR description)
        → Tag PR for human review

[01:00] Red Agent Wraith spawns
        → Tries IDOR → CRITICAL finding nếu có

[02:00] Re-test cycle: Red team re-runs ALL past findings to verify still patched
        → If regression → ALERT escalate

[Daily 06:00 UTC] Daily report email to admin
        → "8 Red agents tested 247 attack vectors, 3 NEW findings, 2 patched, 1 open"
```

---

## 🛠️ MVP Implementation Plan (3 ngày)

### **Day 1: Backend + DB**

```sql
-- Migration 047_red_team.sql
CREATE TABLE red_team_findings (
  id BIGSERIAL PRIMARY KEY,
  agent_name VARCHAR(32),
  attack_type VARCHAR(64),
  severity VARCHAR(16),
  target_endpoint TEXT,
  payload TEXT,
  evidence JSONB,
  cvss_score NUMERIC(3,1),
  status VARCHAR(20),
  cve_reference TEXT,
  remediation_suggestion TEXT,
  blue_team_response TEXT,
  found_at TIMESTAMPTZ DEFAULT NOW(),
  fixed_at TIMESTAMPTZ
);

CREATE TABLE red_team_runs (
  id BIGSERIAL PRIMARY KEY,
  agent_name VARCHAR(32),
  run_at TIMESTAMPTZ DEFAULT NOW(),
  attacks_executed INT,
  findings_count INT,
  duration_sec INT,
  ai_cost_usd NUMERIC(8,4)
);
```

**Endpoints `/api/v1/red-team/*`:**
- `GET /findings?status=new` — list findings
- `POST /findings/{id}/verify` — mark verified
- `POST /findings/{id}/patch` — link to PR
- `GET /agents` — agent status (last_run, score)
- `POST /agents/{name}/trigger` — manual trigger
- `GET /dashboard/stats` — total/by-severity/MTTR

### **Day 2: 3 Red Agents MVP (BlackCat, Phantom, Wraith)**

```python
# services/red_team/blackcat.py
class BlackCatAgent:
    """SQL Injection specialist."""
    PROMPT = """You are BlackCat, a black-hat penetration tester specializing in SQL injection.
    Your job: find SQLi vulnerabilities in {target_endpoint}.
    
    Strategy:
    1. Identify input parameters (query, body, headers)
    2. Test injection: ' OR 1=1--, '" UNION SELECT, sleep(5)--
    3. Analyze response for: SQL errors, data leak, time delay
    4. If vuln confirmed → return finding JSON
    
    DO NOT touch production data. Sandbox only.
    """
    
    async def run(self, target_workspace: str):
        endpoints = await self.discover_endpoints(target_workspace)
        for ep in endpoints:
            payload = self.generate_payload(ep)
            response = await httpx.post(ep, json=payload)
            finding = await self.analyze_with_claude(response)
            if finding['confirmed']:
                await self.report_finding(finding)
```

### **Day 3: Cron + Dashboard**
- Cloud Scheduler trigger mỗi 30 phút
- Round-robin pick 1 agent → run
- Dashboard `/admin/red-team` real-time
- Email daily report

---

## 💰 Cost & Revenue Analysis

### Build cost (em làm):
- Dev: ~3 ngày MVP, ~12 tuần full production
- Infra: Cloud Run + Scheduler ~$30/tháng
- AI calls: Claude Sonnet ~$200/tháng (8 agents × 30 runs/day × ~$0.03 per scenario)
- **Total ongoing**: $230/tháng

### Revenue model — "Zeni Red Team as a Service":
- **Starter** $99/tháng — 3 agents, weekly scan
- **Pro** $499/tháng — 8 agents, daily scan
- **Enterprise** $2999/tháng — 8 agents + custom + 24/7 + dashboard riêng + SLA

**Tiềm năng**: 100 khách Pro × $499 = **$50K MRR** ($600K/năm)

### Market comparison:
| Sản phẩm | Pricing | So với Zeni |
|----------|---------|-------------|
| **Pentera** (Israeli) | $50-200K/năm | 10-40x đắt hơn |
| **AttackIQ** | $30-100K/năm | 6-20x đắt hơn |
| **Cymulate** | $50K/năm | 10x đắt hơn |
| **Detectify** | $1-5K/tháng | 2-10x đắt hơn |
| **HackerOne** | $30K setup + 20% bounty | Khó tiếp cận |
| **Zeni Red Team** | **$99-2999/tháng** | VN-first, tiếng Việt, tích hợp Cloud |

---

## 🎯 Roadmap

### Phase 1: MVP (3 ngày) — Internal use only
- 3 agents (BlackCat, Phantom, Wraith)
- Test against Zeni Cloud sandbox
- Dashboard internal admin only

### Phase 2: Production (8 tuần) — Open beta
- 8 agents full
- 4 Blue Team defenders
- WAF auto-rule generation
- Daily email report
- 5-10 beta customers free

### Phase 3: SaaS Launch (12 tuần) — GA Public
- Customer self-service signup
- Multi-tenant Red Team (mỗi customer có sandbox riêng)
- Pricing tiers + billing tích hợp Zeni Pay
- Marketing campaign cho VN market

### Phase 4: Bug Bounty Platform (6 tháng sau GA)
- External hackers earn $ZENI token
- Verify findings tự động bằng Red Team agents
- Leaderboard, gamification

---

## 🔐 Security & Ethics Constraints

### MUST DO:
- ✅ Sandbox only — NEVER attack production data
- ✅ Customer opt-in — chỉ scan workspaces đã đăng ký
- ✅ Rate limit — không DDoS chính mình
- ✅ Audit log — mọi attack được ghi lại
- ✅ Human approval cho destructive actions (rotate prod keys, suspend user)

### MUST NOT:
- ❌ Attack customer's external systems (chỉ Zeni Cloud-hosted)
- ❌ Exfil data thật (chỉ verify exfil capability via test data)
- ❌ Persist attack tools/backdoors
- ❌ Share findings publicly trước khi customer fix

---

## 📅 Khi nào bắt đầu

**Trigger conditions:**
1. Zeni Cloud GA hoàn thành (Sprint A12 done)
2. >= 5 paying customers (revenue base)
3. Backend stable >= 30 ngày không downtime
4. SOC 2 Type I certification (để bán Enterprise)

**Mục tiêu start**: Q3-Q4 2026 hoặc khi anh chốt sớm hơn.

---

## 🎁 Bonus ideas (Phase 5+)

- **Zeni Red Team for AI** — Test prompt injection, jailbreak, data poisoning trên LLM apps khách
- **Zeni Red Team for Web3** — Smart contract audit auto (re-entrancy, integer overflow)
- **Zeni Red Team Academy** — Train ethical hackers VN, certify, recruit cho team
- **Zeni Insurance** — Tự cung cấp cyber insurance cho customer (cover up to $1M)

---

**📦 LOCKED**: Document này KHÔNG được sửa cho đến khi Chairman ra quyết định build.
**Backup**: Em sẽ commit file này vào repo (private).
