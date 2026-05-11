"""
Error Catalog (Phase 1 P1.4 — chairman approved 2026-05-11)

Mục tiêu: thay error message cryptic ("ImportError: cannot import name X")
bằng message user-friendly + hint actionable + docs link.

Pattern lấy cảm hứng:
  - Vercel deployment errors: mã + hint + "Learn more" link
  - Stripe API errors: { code, message, doc_url, suggestion }
  - Sentry breadcrumbs: context + suggested fix

Usage:
    from app.core.error_catalog import lookup, format_error_response

    detail = lookup("BUILD_NO_DOCKERFILE", repo="viet-contech")
    raise HTTPException(422, detail=detail)

    # OR in service layer:
    if not dockerfile_exists:
        return format_error_response("BUILD_NO_DOCKERFILE", repo=repo_name)

KHÔNG đụng code cũ — đây là file mới hoàn toàn. Caller chỉ cần import.
"""
from __future__ import annotations

from typing import Any


# ─── Catalog: error_code → message + hint + docs ────────────────────
# Convention: code = SCREAMING_SNAKE_CASE, scoped by feature.
# {layer}_{action}_{problem}
# - layer: BUILD, DEPLOY, AUTH, DATA, AI, AUTOMATION, DOMAIN, BILLING
# - action: optional (UPLOAD, MAP, FETCH, ...)
# - problem: descriptive
ERROR_CATALOG: dict[str, dict[str, Any]] = {

    # ─── L1 BUILD / DEPLOY ─────────────────────────────────────────
    "BUILD_NO_DOCKERFILE": {
        "user_msg": "Repo không có Dockerfile và Zeni không detect được framework để auto-build.",
        "hint": "Thêm Dockerfile vào root repo, HOẶC chuyển sang framework có Zeni preset (Next.js/FastAPI/Go/...). Xem framework_detector.py để biết list 15+ framework Zeni hỗ trợ.",
        "docs": "https://zenicloud.io/docs/deploy/dockerfile",
        "http_status": 422,
        "category": "L1_BUILD",
        "severity": "blocker",
    },
    "BUILD_FAILED_INSTALL_DEPS": {
        "user_msg": "Cài đặt dependencies thất bại. Kiểm tra log để xem chi tiết.",
        "hint": "Common causes: (1) package.json/requirements.txt corrupt, (2) private dependency cần auth, (3) network timeout. Xem build log để biết package nào fail.",
        "docs": "https://zenicloud.io/docs/deploy/troubleshooting#install-fail",
        "http_status": 500,
        "category": "L1_BUILD",
        "severity": "blocker",
    },
    "BUILD_FAILED_COMPILE": {
        "user_msg": "Build code thất bại. Có thể TypeScript error, syntax error, hoặc missing env var.",
        "hint": "Test build local: `npm run build` (Node) hoặc `python -m build` (Python). Fix lỗi rồi push lại.",
        "docs": "https://zenicloud.io/docs/deploy/troubleshooting#build-fail",
        "http_status": 500,
        "category": "L1_BUILD",
        "severity": "blocker",
    },
    "BUILD_TIMEOUT": {
        "user_msg": "Build vượt timeout 15 phút.",
        "hint": "Tối ưu: (1) dùng image base nhỏ hơn, (2) split build vào multi-stage Dockerfile, (3) cache layer dependencies.",
        "docs": "https://zenicloud.io/docs/deploy/build-performance",
        "http_status": 408,
        "category": "L1_BUILD",
        "severity": "warning",
    },
    "BUILD_NO_IMAGE_PERMISSION": {
        "user_msg": "Zeni không có quyền push image vào registry này.",
        "hint": "Image registry phải nằm trong workspace whitelist. Liên hệ admin workspace để thêm registry vào whitelist, hoặc dùng registry mặc định của Zeni (us-central1-docker.pkg.dev).",
        "docs": "https://zenicloud.io/docs/deploy/image-registry",
        "http_status": 403,
        "category": "L1_BUILD",
        "severity": "blocker",
    },
    "DEPLOY_CLOUD_RUN_FAIL": {
        "user_msg": "Deploy vào Cloud Run thất bại. Image có thể không pull được hoặc container không khởi động.",
        "hint": "Check (1) image URL đúng + accessible, (2) container listen port khớp với config, (3) memory/CPU đủ cho startup. Xem revision logs để debug.",
        "docs": "https://zenicloud.io/docs/deploy/troubleshooting#cloud-run-fail",
        "http_status": 500,
        "category": "L1_DEPLOY",
        "severity": "blocker",
    },
    "DEPLOY_HEALTH_CHECK_FAIL": {
        "user_msg": "Container deploy nhưng health check fail.",
        "hint": "Service phải respond 200 trên path `/health` (hoặc path đã config) trong 4 phút. Test local: `curl localhost:PORT/health`.",
        "docs": "https://zenicloud.io/docs/deploy/health-check",
        "http_status": 503,
        "category": "L1_DEPLOY",
        "severity": "blocker",
    },

    # ─── L1 DOMAIN ─────────────────────────────────────────────────
    "DOMAIN_INVALID_FORMAT": {
        "user_msg": "Domain không hợp lệ. Định dạng: subdomain.example.com (lowercase, chữ + số + dấu gạch).",
        "hint": "Ví dụ hợp lệ: app.vietcontech.com, api.witsagi.io. KHÔNG hợp lệ: app_vietcontech.com (underscore), APP.example.com (uppercase).",
        "docs": "https://zenicloud.io/docs/domain/format",
        "http_status": 422,
        "category": "L1_DOMAIN",
        "severity": "user_error",
    },
    "DOMAIN_DNS_NOT_PROPAGATED": {
        "user_msg": "DNS chưa propagate. Zeni chưa thấy domain trỏ về LB IP của Zeni Cloud.",
        "hint": "Add A record tại registrar: {domain} → 34.160.162.190 (TTL 300). Đợi 5-30 phút. Check status qua GET /projects/{id}/domain/{domain}/status.",
        "docs": "https://zenicloud.io/docs/domain/dns",
        "http_status": 425,  # Too Early
        "category": "L1_DOMAIN",
        "severity": "info",
    },
    "DOMAIN_CERT_PROVISIONING": {
        "user_msg": "SSL cert đang được Google cấp. Mất 10-15 phút sau khi DNS resolve đúng.",
        "hint": "Đợi thêm. Cert ACTIVE thì https://{domain} sẽ 200. Status qua GET /projects/{id}/domain/{domain}/status.",
        "docs": "https://zenicloud.io/docs/domain/ssl",
        "http_status": 425,
        "category": "L1_DOMAIN",
        "severity": "info",
    },
    "DOMAIN_BACKEND_ATTACH_ROLLBACK": {
        "user_msg": "Zeni đã rollback cert attach do health check zenicloud.io degraded.",
        "hint": "Đây là safety net — rollback đảm bảo zenicloud.io không bị ảnh hưởng. Vui lòng retry sau 5 phút. Nếu vẫn fail, liên hệ support.",
        "docs": "https://zenicloud.io/docs/domain/troubleshooting",
        "http_status": 503,
        "category": "L1_DOMAIN",
        "severity": "warning",
    },

    # ─── L2 DATA ───────────────────────────────────────────────────
    "DATA_QUERY_INVALID_SQL": {
        "user_msg": "SQL query không hợp lệ. Có thể syntax error hoặc bảng/cột không tồn tại.",
        "hint": "Test query local trên PostgreSQL 15+. Lưu ý: Zeni Data L2 isolated per workspace — bảng phải belong workspace của bạn.",
        "docs": "https://zenicloud.io/docs/data/sql",
        "http_status": 422,
        "category": "L2_DATA",
        "severity": "user_error",
    },
    "DATA_QUOTA_EXCEEDED": {
        "user_msg": "Workspace đã vượt quota storage hoặc query/tháng.",
        "hint": "Upgrade plan (Pro $50/Business $200/Enterprise), hoặc top up wallet để pay-as-you-go. Check usage qua GET /pricing/usage.",
        "docs": "https://zenicloud.io/docs/pricing",
        "http_status": 402,  # Payment Required
        "category": "L2_DATA",
        "severity": "blocker",
    },
    "VECTOR_INVALID_DIM": {
        "user_msg": "Vector dimension không khớp với collection.",
        "hint": "Khi tạo collection set `dim` (vd: 1536 cho text-embedding-3-small, 3072 cho large). Khi insert/search vectors phải đúng dim.",
        "docs": "https://zenicloud.io/docs/vector/dimensions",
        "http_status": 422,
        "category": "L2_VECTOR",
        "severity": "user_error",
    },

    # ─── L3 AI ─────────────────────────────────────────────────────
    "AI_MODEL_NOT_FOUND": {
        "user_msg": "Model AI không tồn tại hoặc chưa support.",
        "hint": "Zeni hỗ trợ: claude-haiku-4-5, claude-sonnet-4-5, claude-opus-4-1, gemini-2.5-pro, gemini-2.5-flash, gpt-4o, imagen-3.0. Dùng model_id chính xác.",
        "docs": "https://zenicloud.io/docs/ai/models",
        "http_status": 422,
        "category": "L3_AI",
        "severity": "user_error",
    },
    "AI_PROVIDER_API_ERROR": {
        "user_msg": "Provider AI (Anthropic/Google/OpenAI/DeepSeek) trả lỗi.",
        "hint": "Tạm thời, Zeni Router sẽ failover sang model khác trong tier. Nếu vẫn fail, có thể BYO API key qua POST /workspaces/{ws}/ai-providers.",
        "docs": "https://zenicloud.io/docs/ai/byo-keys",
        "http_status": 502,
        "category": "L3_AI",
        "severity": "warning",
    },
    "AI_QUOTA_EXCEEDED": {
        "user_msg": "Workspace đã hết quota tokens AI/tháng.",
        "hint": "Upgrade plan hoặc top up wallet để pay-as-you-go. Check usage qua GET /pricing/usage.",
        "docs": "https://zenicloud.io/docs/pricing",
        "http_status": 402,
        "category": "L3_AI",
        "severity": "blocker",
    },

    # ─── L4 AUTOMATION ─────────────────────────────────────────────
    "CONNECTOR_INVALID_TYPE": {
        "user_msg": "Connector type không nằm trong danh sách NATIVE_CONNECTORS.",
        "hint": "Hỗ trợ: Zalo OA, Shopee, TikTok Shop, Meta Ads, Google Ads, Mailchimp, VNPay, MoMo, ZaloPay, Stripe, Slack, Discord, Twilio, SendGrid, OpenAI, Anthropic, Notion, Airtable, Google Sheets, HubSpot, Salesforce. Hoặc dùng generic webhook.",
        "docs": "https://zenicloud.io/docs/automation/connectors",
        "http_status": 400,
        "category": "L4_AUTOMATION",
        "severity": "user_error",
    },
    "CONNECTOR_CONFIG_MISSING": {
        "user_msg": "Connector config thiếu field bắt buộc.",
        "hint": "Mỗi loại connector cần khác field. Vd: Zalo OA cần app_id + access_token. VNPay cần tmn_code + hash_secret. Xem docs connector spec.",
        "docs": "https://zenicloud.io/docs/automation/connectors",
        "http_status": 422,
        "category": "L4_AUTOMATION",
        "severity": "user_error",
    },

    # ─── L5 AUTH / IDENTITY ────────────────────────────────────────
    "AUTH_INVALID_TOKEN": {
        "user_msg": "JWT token không hợp lệ hoặc đã hết hạn.",
        "hint": "Refresh token qua POST /auth/refresh hoặc login lại tại /app.",
        "docs": "https://zenicloud.io/docs/auth/jwt",
        "http_status": 401,
        "category": "L5_AUTH",
        "severity": "user_error",
    },
    "AUTH_MFA_REQUIRED": {
        "user_msg": "Tài khoản đã enable MFA. Cần verify TOTP code.",
        "hint": "Mở app Authy/Google Authenticator → lấy 6-digit code → POST /auth/mfa/verify.",
        "docs": "https://zenicloud.io/docs/auth/mfa",
        "http_status": 401,
        "category": "L5_AUTH",
        "severity": "info",
    },
    "AUTH_FORBIDDEN_ROLE": {
        "user_msg": "Role hiện tại không đủ quyền thực hiện action này.",
        "hint": "Cần role Admin hoặc Owner. Liên hệ Owner workspace để invite role cao hơn.",
        "docs": "https://zenicloud.io/docs/auth/rbac",
        "http_status": 403,
        "category": "L5_AUTH",
        "severity": "user_error",
    },

    # ─── BILLING ───────────────────────────────────────────────────
    "BILLING_WALLET_INSUFFICIENT": {
        "user_msg": "Wallet không đủ balance + đã vượt quota subscription.",
        "hint": "Top up wallet qua POST /billing/wallet/topup (VietQR EMV — scan app banking VN), hoặc upgrade plan cao hơn.",
        "docs": "https://zenicloud.io/docs/billing/wallet",
        "http_status": 402,
        "category": "BILLING",
        "severity": "blocker",
    },
    "BILLING_PLAN_REQUIRED": {
        "user_msg": "Workspace chưa subscribe plan và wallet trống.",
        "hint": "Subscribe Pro $50 hoặc Business $200 qua POST /billing/subscribe. Free tier có hard limits.",
        "docs": "https://zenicloud.io/docs/pricing",
        "http_status": 402,
        "category": "BILLING",
        "severity": "blocker",
    },

    # ─── GITHUB INTEGRATION ────────────────────────────────────────
    "GITHUB_WEBHOOK_INVALID_SIGNATURE": {
        "user_msg": "Webhook signature không khớp — payload có thể bị tamper hoặc secret sai.",
        "hint": "Reset webhook secret qua PATCH /github/connections/{id}, sau đó update lại trên GitHub repo Settings.",
        "docs": "https://zenicloud.io/docs/github/webhook",
        "http_status": 401,
        "category": "GITHUB",
        "severity": "warning",
    },
    "GITHUB_REPO_NOT_FOUND": {
        "user_msg": "Repo GitHub không tồn tại hoặc Zeni chưa được grant access.",
        "hint": "Public repo: paste URL trực tiếp. Private repo: install GitHub App qua /app → Settings → GitHub.",
        "docs": "https://zenicloud.io/docs/github/private-repo",
        "http_status": 404,
        "category": "GITHUB",
        "severity": "user_error",
    },

    # ─── GENERIC ───────────────────────────────────────────────────
    "INTERNAL_ERROR": {
        "user_msg": "Lỗi nội bộ Zeni Cloud. Đội ngũ đã được notify.",
        "hint": "Vui lòng retry sau 30s. Nếu liên tục fail, email support@zenicloud.io với trace_id.",
        "docs": "https://zenicloud.io/status",
        "http_status": 500,
        "category": "GENERIC",
        "severity": "critical",
    },
}


# ─── PUBLIC API ──────────────────────────────────────────────────────
def lookup(code: str, **context: Any) -> dict[str, Any]:
    """
    Get error detail by code, optionally interpolating context vars in messages.

    Example:
        detail = lookup("DOMAIN_DNS_NOT_PROPAGATED", domain="app.witsagi.com")
        # → {"code": "...", "user_msg": "DNS chưa propagate...", "hint": "Add A record at registrar: app.witsagi.com → 34.160.162.190...", ...}

    Args:
        code: error code key in ERROR_CATALOG
        **context: variables for str.format() interpolation
    """
    entry = ERROR_CATALOG.get(code)
    if not entry:
        # Unknown code — return generic
        entry = ERROR_CATALOG["INTERNAL_ERROR"]
        code = "INTERNAL_ERROR"

    # Partial-format safe: missing keys remain as literal "{key}" instead of
    # raising KeyError (which would skip interpolation entirely).
    class _SafeDict(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    out: dict[str, Any] = {"code": code}
    ctx = _SafeDict(context) if context else None
    for k, v in entry.items():
        if isinstance(v, str) and ctx is not None and "{" in v:
            try:
                out[k] = v.format_map(ctx)
            except (IndexError, ValueError):
                out[k] = v
        else:
            out[k] = v
    return out


def format_error_response(code: str, **context: Any) -> dict[str, Any]:
    """
    Format error for FastAPI HTTPException detail field.

    Returns dict with `code`, `user_msg`, `hint`, `docs`, `category`, `severity`.
    Caller uses HTTPException(status_code=detail["http_status"], detail=detail).
    """
    return lookup(code, **context)


def list_codes_by_category(category: str) -> list[str]:
    """Helper for testing / docs gen — list all codes in a category."""
    return sorted(c for c, e in ERROR_CATALOG.items() if e.get("category") == category)
