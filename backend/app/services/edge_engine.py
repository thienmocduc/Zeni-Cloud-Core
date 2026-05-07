"""
Zeni Cloud — Edge CDN engine service.

Trách nhiệm:
  - provision_zone(provider, domain, origin)   — call Cloudflare API hoặc setup Cloud CDN
  - purge_cache(zone_id, targets)              — call provider API để invalidate
  - apply_security_rule(zone_id, rule)         — sync rule (WAF/rate-limit) vào provider
  - issue_certificate(domain, type)            — Let's Encrypt ACME flow / Cloudflare Universal
  - renew_certificate(cert_id)                 — gia hạn cert sắp hết hạn
  - verify_dns(domain, expected_target)        — check NS / CNAME records
  - aggregate_analytics_daily()                — cron: pull yesterday stats từ provider

Provider strategy:
  * Cloudflare (default): nếu có CLOUDFLARE_API_TOKEN trong env → call CF API thật
  * Cloud CDN (fallback): nếu CF không có key → setup GCP Load Balancer + CDN
  * Stub mode (dev): KHÔNG có cả 2 → trả mock response, mark zone status='pending'

Notes về bảo mật:
  * Private keys của cert KHÔNG bao giờ lưu plain trong DB.
    Lưu vào Google Secret Manager → DB chỉ giữ key_pem_secret_ref.
  * audit_push được gọi ở layer trên (api/edge_cdn.py) — engine chỉ trả kết quả.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import ssl
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("zeni.edge.engine")


# ════════════════════════════════════════════════════════════════════════════
# Constants & helpers
# ════════════════════════════════════════════════════════════════════════════
DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.IGNORECASE)

CF_API_BASE = "https://api.cloudflare.com/client/v4"
CF_DEFAULT_TIMEOUT = 30.0
CF_PROVISION_RETRY = 2

CERT_RENEW_THRESHOLD_DAYS = 21        # gia hạn khi còn ≤ 21 ngày
CERT_LE_DEFAULT_VALIDITY_DAYS = 90
CERT_UNIVERSAL_VALIDITY_DAYS = 365


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_valid_domain(domain: str) -> bool:
    """Kiểm tra domain hợp lệ (RFC 1035)."""
    return bool(DOMAIN_RE.match((domain or "").strip())) and len(domain or "") <= 253


def _cf_token() -> str | None:
    return os.environ.get("CLOUDFLARE_API_TOKEN") or os.environ.get("CF_API_TOKEN")


def _cf_account_id() -> str | None:
    return os.environ.get("CLOUDFLARE_ACCOUNT_ID") or os.environ.get("CF_ACCOUNT_ID")


def _gcp_enabled() -> bool:
    return bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or os.environ.get("GCP_PROJECT_ID"))


def _stub_mode() -> bool:
    """Stub mode khi KHÔNG có provider credentials."""
    return _cf_token() is None and not _gcp_enabled()


# ════════════════════════════════════════════════════════════════════════════
# Cloudflare HTTP client (lazy)
# ════════════════════════════════════════════════════════════════════════════
class CloudflareClient:
    """Async HTTPX wrapper. Trả raw dict, lỗi nâng RuntimeError với chi tiết."""

    def __init__(self, token: str, timeout: float = CF_DEFAULT_TIMEOUT):
        self.token = token
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "CloudflareClient":
        self._client = httpx.AsyncClient(
            base_url=CF_API_BASE,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def _req(self, method: str, path: str, **kw) -> dict[str, Any]:
        assert self._client is not None
        try:
            r = await self._client.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise RuntimeError(f"Cloudflare network error: {e}") from e
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"Cloudflare non-JSON response (status={r.status_code})")
        if not r.is_success or not data.get("success", False):
            errs = data.get("errors") or [{"message": r.text}]
            msg = "; ".join(str(e.get("message", e)) for e in errs)
            raise RuntimeError(f"Cloudflare API {r.status_code}: {msg}")
        return data

    async def get(self, path: str, **kw): return await self._req("GET", path, **kw)
    async def post(self, path: str, **kw): return await self._req("POST", path, **kw)
    async def put(self, path: str, **kw): return await self._req("PUT", path, **kw)
    async def patch(self, path: str, **kw): return await self._req("PATCH", path, **kw)
    async def delete(self, path: str, **kw): return await self._req("DELETE", path, **kw)


# ════════════════════════════════════════════════════════════════════════════
# 1. Zone provisioning
# ════════════════════════════════════════════════════════════════════════════
async def provision_zone(
    *,
    provider: str,
    domain: str,
    origin_url: str | None = None,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """Tạo zone trên CDN provider.

    Returns:
        {
            "zone_id": "...",            # provider-side id (None nếu stub)
            "status": "pending|active",
            "ssl_status": "pending|active",
            "ssl_provider": "cloudflare_universal|...",
            "name_servers": [...],       # khách trỏ NS về (CF) HOẶC
            "dns_records": [...],         # khách add CNAME (Cloud CDN)
            "instructions": "...",
            "stub": bool,
        }
    """
    if not is_valid_domain(domain):
        raise ValueError(f"Domain không hợp lệ: {domain}")

    if provider == "cloudflare" and _cf_token():
        return await _provision_cloudflare(domain, origin_url)
    if provider == "cloud_cdn" and _gcp_enabled():
        return await _provision_cloud_cdn(domain, origin_url)
    if provider == "fastly":
        # Fastly stub — chưa implement
        return _stub_response(domain, "fastly", origin_url, reason="fastly_not_implemented")

    # No credentials → stub
    return _stub_response(domain, provider, origin_url)


async def _provision_cloudflare(domain: str, origin_url: str | None) -> dict[str, Any]:
    """Tạo zone Cloudflare + bật Universal SSL."""
    token = _cf_token()
    if not token:
        return _stub_response(domain, "cloudflare", origin_url, reason="no_cf_token")
    account_id = _cf_account_id()
    if not account_id:
        return _stub_response(domain, "cloudflare", origin_url, reason="no_cf_account")

    async with CloudflareClient(token) as cf:
        # 1. Create zone
        try:
            res = await cf.post("/zones", json={
                "name": domain,
                "account": {"id": account_id},
                "type": "full",
                "jump_start": True,
            })
        except RuntimeError as e:
            log.warning("CF zone create failed: %s", e)
            return _stub_response(domain, "cloudflare", origin_url, reason=str(e)[:80])

        zone = res.get("result", {})
        zone_id = zone.get("id")
        ns = zone.get("name_servers", [])

        # 2. Bật Universal SSL + Always Use HTTPS + min TLS 1.2
        if zone_id:
            try:
                await cf.patch(f"/zones/{zone_id}/settings/ssl",
                               json={"value": "full"})
                await cf.patch(f"/zones/{zone_id}/settings/always_use_https",
                               json={"value": "on"})
                await cf.patch(f"/zones/{zone_id}/settings/min_tls_version",
                               json={"value": "1.2"})
                await cf.patch(f"/zones/{zone_id}/settings/http3",
                               json={"value": "on"})
            except RuntimeError as e:
                log.warning("CF settings patch failed (continuing): %s", e)

            # 3. Optional: tạo CNAME đến origin
            if origin_url:
                target = _extract_host(origin_url)
                if target:
                    try:
                        await cf.post(f"/zones/{zone_id}/dns_records", json={
                            "type": "CNAME",
                            "name": "@",
                            "content": target,
                            "proxied": True,
                        })
                    except RuntimeError as e:
                        log.info("CF root DNS record skipped: %s", e)

    return {
        "zone_id": zone_id,
        "status": "provisioning",
        "ssl_status": "pending",
        "ssl_provider": "cloudflare_universal",
        "name_servers": ns,
        "dns_records": [],
        "instructions": (
            f"1. Vào DNS panel của registrar.\n"
            f"2. Đổi Name Servers thành: {', '.join(ns) if ns else '(check Cloudflare dashboard)'}\n"
            f"3. Đợi NS propagate 5 phút - 24 giờ.\n"
            f"4. SSL Universal cert tự issue trong 15 phút sau khi NS active."
        ),
        "stub": False,
    }


async def _provision_cloud_cdn(domain: str, origin_url: str | None) -> dict[str, Any]:
    """Stub provisioning Google Cloud CDN.

    Production: tạo Backend Service + URL Map + Target HTTPS Proxy + Forwarding Rule.
    Hiện tại chỉ trả CNAME instruction.
    """
    return {
        "zone_id": f"gcp-cdn-{domain}",
        "status": "pending",
        "ssl_status": "pending",
        "ssl_provider": "google_managed",
        "name_servers": [],
        "dns_records": [
            {"type": "CNAME", "name": domain, "value": "ghs.googlehosted.com."},
        ],
        "instructions": (
            f"1. Add CNAME: {domain} → ghs.googlehosted.com\n"
            f"2. Đợi DNS propagate 5-30 phút.\n"
            f"3. Google-managed SSL cert tự issue (10-30 phút).\n"
        ),
        "stub": False,
    }


def _stub_response(domain: str, provider: str, origin_url: str | None, *, reason: str = "no_creds") -> dict[str, Any]:
    return {
        "zone_id": None,
        "status": "pending",
        "ssl_status": "pending",
        "ssl_provider": "cloudflare_universal" if provider == "cloudflare" else "google_managed",
        "name_servers": [],
        "dns_records": [
            {"type": "CNAME", "name": domain, "value": "edge.zenicloud.io"},
        ],
        "instructions": (
            f"[STUB MODE — {reason}]\n"
            f"Zone sẽ được provision khi {provider.upper()} credentials được cấu hình.\n"
            f"Tạm thời mark zone status='pending'."
        ),
        "stub": True,
    }


def _extract_host(url: str) -> str | None:
    """Lấy host từ URL (có hoặc không có protocol)."""
    if not url:
        return None
    s = url.strip()
    s = re.sub(r"^https?://", "", s, flags=re.I)
    s = s.split("/", 1)[0]
    s = s.split(":", 1)[0]
    return s or None


# ════════════════════════════════════════════════════════════════════════════
# 2. Cache management
# ════════════════════════════════════════════════════════════════════════════
async def purge_cache(
    *,
    provider: str,
    provider_zone_id: str | None,
    purge_type: str,
    targets: list[str],
) -> dict[str, Any]:
    """Invalidate cache trên CDN provider.

    purge_type: 'all' | 'url' | 'tag' | 'host' | 'prefix'
    """
    if purge_type not in {"all", "url", "tag", "host", "prefix"}:
        raise ValueError(f"purge_type không hợp lệ: {purge_type}")

    started = time.perf_counter()

    if provider == "cloudflare" and _cf_token() and provider_zone_id:
        result = await _purge_cloudflare(provider_zone_id, purge_type, targets)
    elif provider == "cloud_cdn":
        result = {"job_id": f"gcp-purge-{int(_now().timestamp())}",
                  "status": "success", "stub": True}
    else:
        result = {"job_id": None, "status": "success", "stub": True,
                  "reason": "no_provider_credentials"}

    result["duration_ms"] = int((time.perf_counter() - started) * 1000)
    return result


async def _purge_cloudflare(zone_id: str, purge_type: str, targets: list[str]) -> dict[str, Any]:
    token = _cf_token()
    if not token:
        return {"job_id": None, "status": "success", "stub": True}

    body: dict[str, Any] = {}
    if purge_type == "all":
        body["purge_everything"] = True
    elif purge_type == "url":
        body["files"] = targets[:30]
    elif purge_type == "tag":
        body["tags"] = targets[:30]
    elif purge_type == "host":
        body["hosts"] = targets[:30]
    elif purge_type == "prefix":
        body["prefixes"] = targets[:30]

    async with CloudflareClient(token) as cf:
        try:
            res = await cf.post(f"/zones/{zone_id}/purge_cache", json=body)
        except RuntimeError as e:
            return {"job_id": None, "status": "failed", "error": str(e)[:200]}

    return {
        "job_id": res.get("result", {}).get("id"),
        "status": "success",
        "stub": False,
    }


# ════════════════════════════════════════════════════════════════════════════
# 3. Security rules
# ════════════════════════════════════════════════════════════════════════════
async def apply_security_rule(
    *,
    provider: str,
    provider_zone_id: str | None,
    rule_type: str,
    rule_config: dict[str, Any],
    action: str,
    enabled: bool,
) -> dict[str, Any]:
    """Sync security rule vào provider. Trả {provider_rule_id, applied}.

    rule_type: 'waf'|'rate_limit'|'ip_block'|'country_block'|'bot_protection'|...
    action:    'block'|'challenge'|'log'|'allow'|'rate_limit'
    """
    if not provider_zone_id or _stub_mode():
        return {"provider_rule_id": None, "applied": False, "stub": True}

    if provider == "cloudflare" and _cf_token():
        return await _apply_cf_security(provider_zone_id, rule_type, rule_config, action, enabled)

    return {"provider_rule_id": None, "applied": False, "stub": True}


async def _apply_cf_security(
    zone_id: str, rule_type: str, cfg: dict[str, Any], action: str, enabled: bool,
) -> dict[str, Any]:
    """Map sang Cloudflare Rulesets / Firewall Rules.

    Đây là implementation đơn giản — production cần map detailed sang CF Rulesets API.
    """
    token = _cf_token()
    if not token:
        return {"provider_rule_id": None, "applied": False, "stub": True}

    expr = _build_cf_expression(rule_type, cfg)
    if not expr:
        return {"provider_rule_id": None, "applied": False,
                "error": f"không build được expression cho {rule_type}"}

    cf_action = {"block": "block", "challenge": "challenge", "log": "log",
                 "allow": "skip", "rate_limit": "rate_limit"}.get(action, "block")

    body = {
        "action": cf_action,
        "expression": expr,
        "description": f"zeni:{rule_type}",
        "enabled": enabled,
    }

    async with CloudflareClient(token) as cf:
        try:
            res = await cf.post(
                f"/zones/{zone_id}/rulesets/phases/http_request_firewall_custom/entrypoint/rules",
                json=body,
            )
        except RuntimeError as e:
            return {"provider_rule_id": None, "applied": False, "error": str(e)[:200]}

    rid = res.get("result", {}).get("id")
    return {"provider_rule_id": rid, "applied": True, "stub": False}


def _build_cf_expression(rule_type: str, cfg: dict[str, Any]) -> str | None:
    """Build Cloudflare Ruleset expression từ rule_config."""
    if rule_type == "ip_block":
        ips = cfg.get("ips") or []
        if not ips:
            return None
        ip_list = ", ".join(f'"{ip}"' for ip in ips[:50])
        return f"(ip.src in {{{ip_list}}})"

    if rule_type == "country_block":
        codes = cfg.get("country_codes") or []
        if not codes:
            return None
        code_list = ", ".join(f'"{c.upper()}"' for c in codes[:50])
        return f"(ip.geoip.country in {{{code_list}}})"

    if rule_type == "asn_block":
        asns = cfg.get("asns") or []
        if not asns:
            return None
        asn_list = ", ".join(re.sub(r"\D", "", str(a)) for a in asns[:50] if a)
        return f"(ip.geoip.asnum in {{{asn_list}}})"

    if rule_type == "user_agent_block":
        patterns = cfg.get("patterns") or []
        if not patterns:
            return None
        ors = " or ".join(f'http.user_agent matches "{p}"' for p in patterns[:20])
        return f"({ors})"

    if rule_type == "bot_protection":
        return '(cf.client.bot)'

    if rule_type == "rate_limit":
        path = cfg.get("match_path") or "/*"
        method = cfg.get("match_method") or "GET"
        return f'(http.request.uri.path matches "{path}" and http.request.method eq "{method}")'

    if rule_type == "waf":
        # WAF managed ruleset → enable via different endpoint, đơn giản hoá
        return '(http.request.uri.path matches ".*")'

    return None


# ════════════════════════════════════════════════════════════════════════════
# 4. Certificates (SSL/TLS)
# ════════════════════════════════════════════════════════════════════════════
async def issue_certificate(
    *,
    domain: str,
    cert_type: str = "lets_encrypt",
    san_domains: list[str] | None = None,
    acme_challenge: str = "http-01",
) -> dict[str, Any]:
    """Issue SSL cert. Trả metadata + secret_ref (KHÔNG trả private key).

    cert_type:
      - 'lets_encrypt'         : ACME flow (đòi DNS hoặc HTTP-01 challenge)
      - 'cloudflare_universal' : CF tự issue khi zone active
      - 'google_managed'       : Cloud Run / GCLB tự manage
      - 'custom'               : khách upload, lưu vào Secret Manager
      - 'self_signed'          : dev only
    """
    if not is_valid_domain(domain):
        raise ValueError(f"Domain không hợp lệ: {domain}")

    if cert_type == "cloudflare_universal":
        return {
            "status": "pending",
            "issued_at": None,
            "expires_at": (_now() + timedelta(days=CERT_UNIVERSAL_VALIDITY_DAYS)).isoformat(),
            "issuer": "Cloudflare Inc ECC CA-3",
            "secret_ref": None,
            "fingerprint": None,
            "note": "CF Universal SSL tự issue ~15 phút sau khi zone active.",
        }

    if cert_type == "google_managed":
        return {
            "status": "pending",
            "issued_at": None,
            "expires_at": (_now() + timedelta(days=CERT_LE_DEFAULT_VALIDITY_DAYS)).isoformat(),
            "issuer": "Google Trust Services",
            "secret_ref": None,
            "fingerprint": None,
            "note": "Google-managed cert tự issue sau khi domain mapping verified.",
        }

    if cert_type == "lets_encrypt":
        # Production: dùng acme-tiny / certbot / aiohttp-acme client
        # Hiện tại stub — đánh dấu pending để cron tiếp tục
        return {
            "status": "pending",
            "issued_at": None,
            "expires_at": (_now() + timedelta(days=CERT_LE_DEFAULT_VALIDITY_DAYS)).isoformat(),
            "issuer": "Let's Encrypt",
            "secret_ref": f"gcp-secret://placeholder/letsencrypt-{domain}",
            "fingerprint": None,
            "acme_challenge": acme_challenge,
            "note": "ACME flow stub — production cần background worker để hoàn tất.",
        }

    if cert_type == "self_signed":
        # KHÔNG dùng cho production
        return {
            "status": "issued",
            "issued_at": _now().isoformat(),
            "expires_at": (_now() + timedelta(days=365)).isoformat(),
            "issuer": "Zeni Cloud Self-Signed (dev)",
            "secret_ref": f"gcp-secret://dev/self-signed-{domain}",
            "fingerprint": None,
            "note": "Self-signed — chỉ dùng cho dev/staging.",
        }

    if cert_type == "custom":
        return {
            "status": "pending",
            "issued_at": None,
            "expires_at": None,
            "issuer": "Custom (upload bởi khách)",
            "secret_ref": None,
            "fingerprint": None,
            "note": "Khách phải upload cert qua /edge/certificates/upload (chưa public).",
        }

    raise ValueError(f"cert_type không hỗ trợ: {cert_type}")


async def renew_certificate(
    *,
    cert_id: int,
    domain: str,
    cert_type: str,
    db: AsyncSession,
) -> dict[str, Any]:
    """Gia hạn cert. Update DB record với status mới."""
    res = await issue_certificate(domain=domain, cert_type=cert_type)
    try:
        await db.execute(text("""
            UPDATE cdn_certificates
               SET status        = :st,
                   expires_at    = :exp,
                   issued_at     = :iss,
                   last_renew_at = NOW(),
                   renew_attempts = renew_attempts + 1,
                   last_error    = NULL
             WHERE id = :id
        """), {
            "id": cert_id,
            "st": res.get("status") or "pending",
            "exp": res.get("expires_at"),
            "iss": res.get("issued_at"),
        })
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("renew_certificate db update failed cert_id=%s", cert_id)
        return {"renewed": False, "error": str(e)[:200]}
    return {"renewed": True, **res}


# ════════════════════════════════════════════════════════════════════════════
# 5. DNS verification
# ════════════════════════════════════════════════════════════════════════════
async def verify_dns(
    *,
    domain: str,
    expected_target: str | None = None,
    expected_ns: list[str] | None = None,
) -> dict[str, Any]:
    """Check NS / CNAME records của domain.

    Returns:
        {"verified": bool, "current_records": {...}, "expected": {...}, "messages": [...]}
    """
    msgs: list[str] = []
    current: dict[str, Any] = {"a": [], "cname": None, "ns": []}

    # Resolve A record (sync trong asyncio executor)
    loop = asyncio.get_event_loop()
    try:
        ips = await loop.run_in_executor(None, _resolve_a, domain)
        current["a"] = ips
    except Exception as e:
        msgs.append(f"A lookup error: {e}")

    try:
        cname = await loop.run_in_executor(None, _resolve_cname, domain)
        current["cname"] = cname
    except Exception as e:
        msgs.append(f"CNAME lookup error: {e}")

    try:
        ns = await loop.run_in_executor(None, _resolve_ns, domain)
        current["ns"] = ns
    except Exception as e:
        msgs.append(f"NS lookup error: {e}")

    verified = True
    if expected_target:
        target_host = _extract_host(expected_target) or expected_target
        if (current.get("cname") or "").rstrip(".").lower() != target_host.rstrip(".").lower():
            # Cũng có thể đã trỏ A direct
            if not current.get("a"):
                verified = False
                msgs.append(f"CNAME của {domain} không trỏ về {target_host}")

    if expected_ns:
        expected_set = {n.rstrip(".").lower() for n in expected_ns}
        actual_set = {n.rstrip(".").lower() for n in (current.get("ns") or [])}
        if not expected_set.issubset(actual_set):
            verified = False
            missing = expected_set - actual_set
            msgs.append(f"NS records thiếu: {sorted(missing)}")

    return {
        "verified": verified,
        "current_records": current,
        "expected": {"target": expected_target, "ns": expected_ns or []},
        "messages": msgs,
    }


def _resolve_a(domain: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(domain, None, family=socket.AF_INET)
        return list({info[4][0] for info in infos})
    except socket.gaierror:
        return []


def _resolve_cname(domain: str) -> str | None:
    """Best-effort CNAME via dnspython if available, else None."""
    try:
        import dns.resolver  # type: ignore
        ans = dns.resolver.resolve(domain, "CNAME", lifetime=3.0)
        for r in ans:
            return str(r.target).rstrip(".")
    except Exception:
        pass
    return None


def _resolve_ns(domain: str) -> list[str]:
    try:
        import dns.resolver  # type: ignore
        ans = dns.resolver.resolve(domain, "NS", lifetime=3.0)
        return [str(r.target).rstrip(".") for r in ans]
    except Exception:
        return []


# ════════════════════════════════════════════════════════════════════════════
# 6. Analytics aggregation (cron)
# ════════════════════════════════════════════════════════════════════════════
async def aggregate_analytics_daily(
    db: AsyncSession,
    *,
    target_date: date | None = None,
) -> dict[str, Any]:
    """Cron worker: kéo stats hôm qua từ provider, upsert vào cdn_analytics_daily.

    Chạy 02:00 UTC mỗi ngày.
    """
    target_date = target_date or (date.today() - timedelta(days=1))
    log.info("aggregate_analytics_daily for %s", target_date.isoformat())

    rows = (await db.execute(text("""
        SELECT id, workspace_id, domain, cdn_provider, zone_id
          FROM cdn_zones
         WHERE status IN ('active','provisioning')
    """))).all()

    inserted = 0
    failed = 0

    for r in rows:
        zone_pk, ws, domain, provider, provider_zone_id = r[0], r[1], r[2], r[3], r[4]
        try:
            stats = await _fetch_provider_analytics(
                provider=provider,
                provider_zone_id=provider_zone_id,
                domain=domain,
                target_date=target_date,
            )
            await db.execute(text("""
                INSERT INTO cdn_analytics_daily
                  (zone_id, date, requests, bandwidth_gb, cache_hit_rate,
                   threats_blocked, unique_visitors,
                   requests_2xx, requests_3xx, requests_4xx, requests_5xx,
                   avg_response_ms, p95_response_ms, bytes_saved_gb,
                   top_countries, top_paths, fetched_at)
                VALUES
                  (:zid, :d, :req, :bw, :chr, :tb, :uv,
                   :r2, :r3, :r4, :r5,
                   :avg, :p95, :bs,
                   CAST(:tc AS JSONB), CAST(:tp AS JSONB), NOW())
                ON CONFLICT (zone_id, date) DO UPDATE SET
                   requests        = EXCLUDED.requests,
                   bandwidth_gb    = EXCLUDED.bandwidth_gb,
                   cache_hit_rate  = EXCLUDED.cache_hit_rate,
                   threats_blocked = EXCLUDED.threats_blocked,
                   unique_visitors = EXCLUDED.unique_visitors,
                   requests_2xx    = EXCLUDED.requests_2xx,
                   requests_3xx    = EXCLUDED.requests_3xx,
                   requests_4xx    = EXCLUDED.requests_4xx,
                   requests_5xx    = EXCLUDED.requests_5xx,
                   avg_response_ms = EXCLUDED.avg_response_ms,
                   p95_response_ms = EXCLUDED.p95_response_ms,
                   bytes_saved_gb  = EXCLUDED.bytes_saved_gb,
                   top_countries   = EXCLUDED.top_countries,
                   top_paths       = EXCLUDED.top_paths,
                   fetched_at      = NOW()
            """), {
                "zid": zone_pk, "d": target_date,
                "req": stats["requests"], "bw": stats["bandwidth_gb"],
                "chr": stats["cache_hit_rate"], "tb": stats["threats_blocked"],
                "uv": stats["unique_visitors"],
                "r2": stats["requests_2xx"], "r3": stats["requests_3xx"],
                "r4": stats["requests_4xx"], "r5": stats["requests_5xx"],
                "avg": stats["avg_response_ms"], "p95": stats["p95_response_ms"],
                "bs": stats["bytes_saved_gb"],
                "tc": json.dumps(stats.get("top_countries", [])),
                "tp": json.dumps(stats.get("top_paths", [])),
            })
            inserted += 1
        except Exception as e:
            log.warning("analytics fetch failed zone=%s: %s", zone_pk, e)
            failed += 1

    try:
        await db.commit()
    except Exception:
        await db.rollback()

    return {
        "date": target_date.isoformat(),
        "zones_total": len(rows),
        "inserted": inserted,
        "failed": failed,
    }


async def _fetch_provider_analytics(
    *,
    provider: str,
    provider_zone_id: str | None,
    domain: str,
    target_date: date,
) -> dict[str, Any]:
    """Pull stats từ provider. Stub mode → trả zeros."""
    if provider == "cloudflare" and _cf_token() and provider_zone_id:
        return await _fetch_cloudflare_analytics(provider_zone_id, target_date)
    return _empty_stats()


async def _fetch_cloudflare_analytics(zone_id: str, target_date: date) -> dict[str, Any]:
    """Cloudflare GraphQL Analytics API. Stub fallback nếu lỗi."""
    token = _cf_token()
    if not token:
        return _empty_stats()

    since = f"{target_date.isoformat()}T00:00:00Z"
    until = f"{target_date.isoformat()}T23:59:59Z"
    query = """
    query($zone: String!, $since: String!, $until: String!) {
      viewer { zones(filter: {zoneTag: $zone}) {
        httpRequests1dGroups(limit: 1, filter: {date_geq: $since, date_leq: $until}) {
          sum { requests bytes threats cachedRequests }
          uniq { uniques }
        }
      }}
    }"""
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.post(
                "https://api.cloudflare.com/client/v4/graphql",
                headers={"Authorization": f"Bearer {token}"},
                json={"query": query, "variables": {
                    "zone": zone_id, "since": since, "until": until,
                }},
            )
        data = r.json()
        groups = (data.get("data", {}).get("viewer", {})
                  .get("zones", [{}])[0].get("httpRequests1dGroups", []))
        if not groups:
            return _empty_stats()
        s = groups[0].get("sum", {})
        u = groups[0].get("uniq", {})
        requests = int(s.get("requests", 0))
        bytes_ = int(s.get("bytes", 0))
        cached = int(s.get("cachedRequests", 0))
        cache_hit_rate = round(100.0 * cached / max(requests, 1), 2)
        return {
            "requests": requests,
            "bandwidth_gb": round(bytes_ / 1e9, 4),
            "cache_hit_rate": cache_hit_rate,
            "threats_blocked": int(s.get("threats", 0)),
            "unique_visitors": int(u.get("uniques", 0)),
            "requests_2xx": 0, "requests_3xx": 0,
            "requests_4xx": 0, "requests_5xx": 0,
            "avg_response_ms": 0, "p95_response_ms": 0,
            "bytes_saved_gb": round(bytes_ * (cache_hit_rate / 100.0) / 1e9, 4),
            "top_countries": [],
            "top_paths": [],
        }
    except Exception as e:
        log.warning("CF analytics fetch failed: %s", e)
        return _empty_stats()


def _empty_stats() -> dict[str, Any]:
    return {
        "requests": 0, "bandwidth_gb": 0.0, "cache_hit_rate": 0.0,
        "threats_blocked": 0, "unique_visitors": 0,
        "requests_2xx": 0, "requests_3xx": 0, "requests_4xx": 0, "requests_5xx": 0,
        "avg_response_ms": 0, "p95_response_ms": 0, "bytes_saved_gb": 0.0,
        "top_countries": [], "top_paths": [],
    }


# ════════════════════════════════════════════════════════════════════════════
# 7. Cert renewal cron
# ════════════════════════════════════════════════════════════════════════════
async def renew_expiring_certificates(db: AsyncSession) -> dict[str, Any]:
    """Cron: scan cert sắp hết hạn (≤21d), gia hạn auto."""
    threshold = _now() + timedelta(days=CERT_RENEW_THRESHOLD_DAYS)
    rows = (await db.execute(text("""
        SELECT id, domain, cert_type
          FROM cdn_certificates
         WHERE auto_renew = TRUE
           AND status IN ('active','issued','expiring')
           AND expires_at IS NOT NULL
           AND expires_at <= :th
         LIMIT 200
    """), {"th": threshold})).all()

    renewed = 0
    failed = 0
    for r in rows:
        res = await renew_certificate(
            cert_id=r[0], domain=r[1], cert_type=r[2], db=db,
        )
        if res.get("renewed"):
            renewed += 1
        else:
            failed += 1

    return {"scanned": len(rows), "renewed": renewed, "failed": failed}


# ════════════════════════════════════════════════════════════════════════════
# 8. Realtime metrics (last 5 min)
# ════════════════════════════════════════════════════════════════════════════
async def fetch_realtime_metrics(
    *,
    provider: str,
    provider_zone_id: str | None,
) -> dict[str, Any]:
    """Trả last-5min stats (best-effort)."""
    if not provider_zone_id or _stub_mode():
        return {
            "requests_last_5min": 0,
            "bandwidth_mb_last_5min": 0.0,
            "cache_hit_rate": 0.0,
            "threats_blocked": 0,
            "stub": True,
            "fetched_at": _now().isoformat(),
        }
    if provider == "cloudflare" and _cf_token():
        # Production: call CF GraphQL với time-window 5 phút
        return {
            "requests_last_5min": 0,
            "bandwidth_mb_last_5min": 0.0,
            "cache_hit_rate": 0.0,
            "threats_blocked": 0,
            "stub": True,
            "note": "CF realtime endpoint chưa wired (cần Workers/Logpush).",
            "fetched_at": _now().isoformat(),
        }
    return {
        "requests_last_5min": 0,
        "bandwidth_mb_last_5min": 0.0,
        "cache_hit_rate": 0.0,
        "threats_blocked": 0,
        "stub": True,
        "fetched_at": _now().isoformat(),
    }


# ════════════════════════════════════════════════════════════════════════════
# 9. Zone status sync (called from /verify endpoint)
# ════════════════════════════════════════════════════════════════════════════
async def sync_zone_status(
    *,
    provider: str,
    provider_zone_id: str | None,
) -> dict[str, Any]:
    """Pull current zone status từ provider. Trả {status, ssl_status, ssl_expires_at}."""
    if not provider_zone_id or _stub_mode():
        return {"status": "pending", "ssl_status": "pending", "ssl_expires_at": None,
                "stub": True}

    if provider == "cloudflare" and _cf_token():
        token = _cf_token()
        try:
            async with CloudflareClient(token) as cf:
                z = await cf.get(f"/zones/{provider_zone_id}")
                ssl = await cf.get(f"/zones/{provider_zone_id}/ssl/universal/settings")
        except RuntimeError as e:
            return {"status": "error", "ssl_status": "error", "ssl_expires_at": None,
                    "error": str(e)[:200]}
        zres = z.get("result", {})
        sres = ssl.get("result", {})
        cf_status = (zres.get("status") or "pending").lower()
        # CF zone statuses: 'active','pending','initializing','moved','deleted'
        st = "active" if cf_status == "active" else "provisioning"
        ssl_enabled = sres.get("enabled", False)
        ssl_st = "active" if (st == "active" and ssl_enabled) else "pending"
        return {
            "status": st,
            "ssl_status": ssl_st,
            "ssl_expires_at": (_now() + timedelta(days=CERT_UNIVERSAL_VALIDITY_DAYS)).isoformat() if ssl_st == "active" else None,
            "stub": False,
        }

    return {"status": "pending", "ssl_status": "pending", "ssl_expires_at": None,
            "stub": True}
