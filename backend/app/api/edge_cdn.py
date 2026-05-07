"""
Zeni Cloud — Edge CDN + Custom Domain API.

Router prefix `/edge`.

Mục đích:
  Quản lý edge layer (CDN) + multi-domain + SSL automation cho khách hàng
  deploy app trên Zeni Cloud (Compute / Container / Static).

Endpoints:

  Zones (CDN config / domain):
    POST   /edge/zones?ws=
    GET    /edge/zones?ws=&project_id=
    GET    /edge/zones/{id}?ws=
    PATCH  /edge/zones/{id}?ws=
    DELETE /edge/zones/{id}?ws=
    POST   /edge/zones/{id}/verify?ws=

  Routes (path-based routing/caching):
    POST   /edge/zones/{id}/routes?ws=
    GET    /edge/zones/{id}/routes?ws=
    PATCH  /edge/zones/{id}/routes/{rid}?ws=
    DELETE /edge/zones/{id}/routes/{rid}?ws=

  Cache:
    POST   /edge/zones/{id}/purge?ws=
    GET    /edge/zones/{id}/purge-history?ws=

  Security (WAF / rate-limit / IP block / country block):
    POST   /edge/zones/{id}/security?ws=
    GET    /edge/zones/{id}/security?ws=
    PATCH  /edge/zones/{id}/security/{rid}?ws=
    DELETE /edge/zones/{id}/security/{rid}?ws=

  SSL/TLS Certificates:
    GET    /edge/certificates?ws=
    POST   /edge/certificates/issue?ws=
    POST   /edge/certificates/{id}/renew?ws=
    DELETE /edge/certificates/{id}?ws=

  Analytics:
    GET    /edge/zones/{id}/analytics?ws=&from=&to=
    GET    /edge/zones/{id}/realtime?ws=

Security:
  - Mọi endpoint: get_current_user + require_workspace_access(ws)
  - PAT scope: 'edge' or 'full'
  - audit_push cho mọi state change quan trọng
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push
from app.services.edge_engine import (
    apply_security_rule,
    fetch_realtime_metrics,
    is_valid_domain,
    issue_certificate,
    provision_zone,
    purge_cache,
    renew_certificate,
    sync_zone_status,
    verify_dns,
)

log = logging.getLogger("zeni.api.edge")
router = APIRouter(prefix="/edge", tags=["edge-cdn"])


# ─── Constants ──────────────────────────────────────────
MAX_LIMIT = 200
MAX_PURGE_TARGETS = 30
MAX_ROUTES_PER_ZONE = 200
MAX_SECURITY_RULES_PER_ZONE = 100

CDN_PROVIDERS = {"cloudflare", "fastly", "cloud_cdn"}
ZONE_STATUSES = {"pending", "provisioning", "active", "disabled", "error"}
SSL_STATUSES = {"pending", "active", "expired", "error", "none"}
SSL_PROVIDERS = {"lets_encrypt", "custom", "cloudflare_universal", "google_managed"}
PURGE_TYPES = {"all", "url", "tag", "host", "prefix"}
SECURITY_RULE_TYPES = {
    "waf", "rate_limit", "ip_block", "country_block",
    "bot_protection", "asn_block", "user_agent_block",
}
SECURITY_ACTIONS = {"block", "challenge", "log", "allow", "rate_limit"}
CERT_TYPES = {"lets_encrypt", "custom", "cloudflare_universal", "google_managed", "self_signed"}
CERT_STATUSES = {"pending", "issued", "active", "expiring", "expired", "revoked", "error"}
REDIRECT_STATUSES = {301, 302, 307, 308}
ALLOWED_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════
def _check_scope(me: CurrentUser) -> None:
    """PAT phải có scope 'edge' hoặc 'full'. JWT users pass."""
    if me.auth_scope is None:
        return
    scopes = {s.strip() for s in (me.auth_scope or "").split(",")}
    if "full" not in scopes and "edge" not in scopes:
        raise HTTPException(status_code=403, detail="PAT cần scope 'edge' hoặc 'full' để dùng /edge")


def _require_writer(me: CurrentUser) -> None:
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không có quyền ghi")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize_jsonb(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def _isoformat(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    try:
        return v.isoformat()
    except Exception:
        return str(v)


async def _load_zone(db: AsyncSession, zone_id: int, ws: str) -> dict[str, Any]:
    row = (await db.execute(text("""
        SELECT id, workspace_id, project_id, domain, cdn_provider, zone_id,
               origin_url, status, ssl_status, ssl_provider, ssl_expires_at,
               http2_enabled, http3_enabled, waf_enabled, bot_protection,
               always_use_https, min_tls_version, metadata,
               created_at, updated_at
          FROM cdn_zones
         WHERE id = :id AND workspace_id = :ws
    """), {"id": zone_id, "ws": ws})).first()
    if not row:
        raise HTTPException(404, "zone không tồn tại")
    return _row_zone(row)


def _row_zone(r) -> dict[str, Any]:
    return {
        "id": int(r[0]),
        "workspace_id": r[1],
        "project_id": str(r[2]) if r[2] is not None else None,
        "domain": r[3],
        "cdn_provider": r[4],
        "provider_zone_id": r[5],
        "origin_url": r[6],
        "status": r[7],
        "ssl_status": r[8],
        "ssl_provider": r[9],
        "ssl_expires_at": _isoformat(r[10]),
        "http2_enabled": bool(r[11]),
        "http3_enabled": bool(r[12]),
        "waf_enabled": bool(r[13]),
        "bot_protection": bool(r[14]),
        "always_use_https": bool(r[15]),
        "min_tls_version": r[16],
        "metadata": _serialize_jsonb(r[17]),
        "created_at": _isoformat(r[18]),
        "updated_at": _isoformat(r[19]),
    }


def _row_route(r) -> dict[str, Any]:
    return {
        "id": int(r[0]),
        "zone_id": int(r[1]),
        "path_pattern": r[2],
        "origin_url": r[3],
        "cache_ttl_seconds": int(r[4] or 0),
        "cache_browser_ttl": int(r[5] or 0),
        "bypass_cache_cookie": r[6],
        "cache_key_query_strings": list(r[7] or []),
        "redirect_to": r[8],
        "redirect_status": int(r[9]) if r[9] is not None else 302,
        "methods": list(r[10] or []),
        "headers_add": _serialize_jsonb(r[11]),
        "headers_remove": list(r[12] or []),
        "priority": int(r[13]),
        "enabled": bool(r[14]),
        "created_at": _isoformat(r[15]),
        "updated_at": _isoformat(r[16]),
    }


def _row_security(r) -> dict[str, Any]:
    return {
        "id": int(r[0]),
        "zone_id": int(r[1]),
        "rule_type": r[2],
        "rule_config": _serialize_jsonb(r[3]),
        "action": r[4],
        "description": r[5],
        "enabled": bool(r[6]),
        "priority": int(r[7]),
        "provider_rule_id": r[8],
        "hits_count": int(r[9] or 0),
        "last_hit_at": _isoformat(r[10]),
        "created_at": _isoformat(r[11]),
        "updated_at": _isoformat(r[12]),
    }


def _row_cert(r) -> dict[str, Any]:
    return {
        "id": int(r[0]),
        "workspace_id": r[1],
        "zone_id": int(r[2]) if r[2] is not None else None,
        "domain": r[3],
        "san_domains": list(r[4] or []),
        "cert_type": r[5],
        "fingerprint_sha256": r[6],
        "issuer": r[7],
        "issued_at": _isoformat(r[8]),
        "expires_at": _isoformat(r[9]),
        "status": r[10],
        "auto_renew": bool(r[11]),
        "renew_attempts": int(r[12] or 0),
        "last_renew_at": _isoformat(r[13]),
        "last_error": r[14],
        "acme_challenge": r[15],
        "secret_ref": r[16],
        "created_at": _isoformat(r[17]),
        "updated_at": _isoformat(r[18]),
    }


_CERT_COLS = (
    "id, workspace_id, zone_id, domain, san_domains, cert_type, "
    "fingerprint_sha256, issuer, issued_at, expires_at, status, auto_renew, "
    "renew_attempts, last_renew_at, last_error, acme_challenge, key_pem_secret_ref, "
    "created_at, updated_at"
)


# ════════════════════════════════════════════════════════════════════════════
# 1. ZONES
# ════════════════════════════════════════════════════════════════════════════
class ZoneCreateIn(BaseModel):
    project_id: str | None = Field(default=None, max_length=64)
    domain: str = Field(..., min_length=3, max_length=253)
    cdn_provider: str = Field(default="cloudflare")
    origin_url: str | None = Field(default=None, max_length=512)

    @field_validator("domain")
    @classmethod
    def _v_dom(cls, v: str) -> str:
        if not is_valid_domain(v):
            raise ValueError(f"domain không hợp lệ: {v}")
        return v.lower()

    @field_validator("cdn_provider")
    @classmethod
    def _v_prov(cls, v: str) -> str:
        if v not in CDN_PROVIDERS:
            raise ValueError(f"cdn_provider phải thuộc {sorted(CDN_PROVIDERS)}")
        return v


class ZonePatchIn(BaseModel):
    cdn_provider: str | None = None
    origin_url: str | None = Field(default=None, max_length=512)
    http2_enabled: bool | None = None
    http3_enabled: bool | None = None
    waf_enabled: bool | None = None
    bot_protection: bool | None = None
    always_use_https: bool | None = None
    min_tls_version: str | None = Field(default=None, max_length=8)
    status: str | None = None


@router.post("/zones", status_code=201)
async def create_zone(
    data: ZoneCreateIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Tạo CDN zone cho một domain. Provision sẽ chạy ngay (best-effort)."""
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)

    # Provision với provider (best-effort, lỗi → mark pending)
    try:
        prov = await provision_zone(
            provider=data.cdn_provider,
            domain=data.domain,
            origin_url=data.origin_url,
            workspace_id=ws,
        )
    except Exception as e:
        log.warning("provision_zone failed domain=%s: %s", data.domain, e)
        prov = {
            "zone_id": None, "status": "error",
            "ssl_status": "error", "ssl_provider": "cloudflare_universal",
            "name_servers": [], "dns_records": [],
            "instructions": f"Provision lỗi: {e}", "stub": True,
        }

    try:
        row = (await db.execute(text("""
            INSERT INTO cdn_zones
              (workspace_id, project_id, domain, cdn_provider, zone_id,
               origin_url, status, ssl_status, ssl_provider, metadata)
            VALUES
              (:ws, :pid, :dom, :prov, :zid,
               :origin, :st, :ssl, :sslp, CAST(:meta AS JSONB))
            RETURNING id, workspace_id, project_id, domain, cdn_provider, zone_id,
                      origin_url, status, ssl_status, ssl_provider, ssl_expires_at,
                      http2_enabled, http3_enabled, waf_enabled, bot_protection,
                      always_use_https, min_tls_version, metadata,
                      created_at, updated_at
        """), {
            "ws": ws,
            "pid": data.project_id,
            "dom": data.domain,
            "prov": data.cdn_provider,
            "zid": prov.get("zone_id"),
            "origin": data.origin_url,
            "st": prov.get("status", "pending"),
            "ssl": prov.get("ssl_status", "pending"),
            "sslp": prov.get("ssl_provider", "cloudflare_universal"),
            "meta": json.dumps({
                "name_servers": prov.get("name_servers", []),
                "dns_records": prov.get("dns_records", []),
                "instructions": prov.get("instructions"),
                "stub": prov.get("stub", False),
            }),
        })).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(409, f"domain {data.domain} đã được tạo trong workspace")
        log.exception("create_zone insert failed")
        raise HTTPException(502, f"không tạo được zone: {type(e).__name__}")

    out = _row_zone(row)
    out["provisioning"] = prov

    try:
        await audit_push(
            db, actor=me.email, workspace_id=ws,
            action="edge.zone.create", target=f"zone#{out['id']}",
            severity="ok",
            metadata={"domain": data.domain, "provider": data.cdn_provider, "stub": prov.get("stub", False)},
        )
        await db.commit()
    except Exception:
        await db.rollback()
    return out


@router.get("/zones")
async def list_zones(
    ws: str,
    project_id: str | None = None,
    status: str | None = None,
    provider: str | None = None,
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)

    clauses = ["workspace_id = :ws"]
    params: dict[str, Any] = {"ws": ws, "lim": limit, "off": offset}
    if project_id:
        clauses.append("project_id = :pid")
        params["pid"] = project_id
    if status:
        if status not in ZONE_STATUSES:
            raise HTTPException(400, f"status phải thuộc {sorted(ZONE_STATUSES)}")
        clauses.append("status = :st")
        params["st"] = status
    if provider:
        if provider not in CDN_PROVIDERS:
            raise HTTPException(400, f"provider phải thuộc {sorted(CDN_PROVIDERS)}")
        clauses.append("cdn_provider = :prov")
        params["prov"] = provider

    where = " AND ".join(clauses)
    rows = (await db.execute(text(f"""
        SELECT id, workspace_id, project_id, domain, cdn_provider, zone_id,
               origin_url, status, ssl_status, ssl_provider, ssl_expires_at,
               http2_enabled, http3_enabled, waf_enabled, bot_protection,
               always_use_https, min_tls_version, metadata,
               created_at, updated_at
          FROM cdn_zones
         WHERE {where}
         ORDER BY created_at DESC
         LIMIT :lim OFFSET :off
    """), params)).all()
    total = (await db.execute(
        text(f"SELECT COUNT(*) FROM cdn_zones WHERE {where}"),
        {k: v for k, v in params.items() if k not in ("lim", "off")}
    )).scalar_one()

    return {
        "workspace_id": ws,
        "count": len(rows),
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
        "zones": [_row_zone(r) for r in rows],
    }


@router.get("/zones/{zone_id}")
async def get_zone(
    zone_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    return await _load_zone(db, zone_id, ws)


@router.patch("/zones/{zone_id}")
async def patch_zone(
    zone_id: int,
    data: ZonePatchIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)

    if data.cdn_provider is not None and data.cdn_provider not in CDN_PROVIDERS:
        raise HTTPException(400, f"cdn_provider phải thuộc {sorted(CDN_PROVIDERS)}")
    if data.status is not None and data.status not in ZONE_STATUSES:
        raise HTTPException(400, f"status phải thuộc {sorted(ZONE_STATUSES)}")

    sets: list[str] = []
    params: dict[str, Any] = {"id": zone_id, "ws": ws}
    for f in ("cdn_provider", "origin_url", "http2_enabled", "http3_enabled",
              "waf_enabled", "bot_protection", "always_use_https",
              "min_tls_version", "status"):
        v = getattr(data, f)
        if v is not None:
            sets.append(f"{f} = :{f}")
            params[f] = v
    if not sets:
        raise HTTPException(400, "không có field nào cần update")

    sql = f"""
        UPDATE cdn_zones SET {', '.join(sets)}
         WHERE id = :id AND workspace_id = :ws
         RETURNING id, workspace_id, project_id, domain, cdn_provider, zone_id,
                   origin_url, status, ssl_status, ssl_provider, ssl_expires_at,
                   http2_enabled, http3_enabled, waf_enabled, bot_protection,
                   always_use_https, min_tls_version, metadata,
                   created_at, updated_at
    """
    try:
        row = (await db.execute(text(sql), params)).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("patch_zone failed id=%s", zone_id)
        raise HTTPException(502, f"update failed: {type(e).__name__}")
    if not row:
        raise HTTPException(404, "zone không tồn tại")

    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="edge.zone.update", target=f"zone#{zone_id}",
                         severity="ok", metadata={"fields": list(params.keys())})
        await db.commit()
    except Exception:
        await db.rollback()
    return _row_zone(row)


@router.delete("/zones/{zone_id}")
async def delete_zone(
    zone_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)

    z = await _load_zone(db, zone_id, ws)
    res = await db.execute(text("""
        DELETE FROM cdn_zones WHERE id = :id AND workspace_id = :ws
    """), {"id": zone_id, "ws": ws})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(404, "zone không tồn tại")

    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="edge.zone.delete", target=f"zone#{zone_id}",
                         severity="warn",
                         metadata={"domain": z["domain"], "provider": z["cdn_provider"]})
        await db.commit()
    except Exception:
        await db.rollback()
    return {"deleted": True, "id": zone_id, "domain": z["domain"]}


@router.post("/zones/{zone_id}/verify")
async def verify_zone(
    zone_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Check DNS đã trỏ đúng + sync trạng thái từ provider."""
    await require_workspace_access(ws, me)
    _check_scope(me)
    z = await _load_zone(db, zone_id, ws)

    meta = z.get("metadata") or {}
    expected_ns = meta.get("name_servers") or []
    expected_target = None
    for rec in (meta.get("dns_records") or []):
        if rec.get("type") in ("CNAME", "A"):
            expected_target = rec.get("value")
            break

    dns_check = await verify_dns(
        domain=z["domain"],
        expected_target=expected_target,
        expected_ns=expected_ns,
    )
    sync = await sync_zone_status(
        provider=z["cdn_provider"],
        provider_zone_id=z.get("provider_zone_id"),
    )

    # Update zone status nếu provider khẳng định active
    new_status = sync.get("status") or z["status"]
    new_ssl = sync.get("ssl_status") or z["ssl_status"]
    new_exp = sync.get("ssl_expires_at")
    if new_status != z["status"] or new_ssl != z["ssl_status"]:
        try:
            await db.execute(text("""
                UPDATE cdn_zones SET status = :st, ssl_status = :ssl,
                       ssl_expires_at = :exp
                 WHERE id = :id AND workspace_id = :ws
            """), {"st": new_status, "ssl": new_ssl, "exp": new_exp,
                   "id": zone_id, "ws": ws})
            await db.commit()
        except Exception:
            await db.rollback()

    return {
        "zone_id": zone_id,
        "domain": z["domain"],
        "dns": dns_check,
        "provider_sync": sync,
        "status": new_status,
        "ssl_status": new_ssl,
    }


# ════════════════════════════════════════════════════════════════════════════
# 2. ROUTES
# ════════════════════════════════════════════════════════════════════════════
class RouteIn(BaseModel):
    path_pattern: str = Field(..., min_length=1, max_length=512)
    origin_url: str | None = Field(default=None, max_length=512)
    cache_ttl_seconds: int = Field(default=0, ge=0, le=31_536_000)
    cache_browser_ttl: int = Field(default=0, ge=0, le=31_536_000)
    bypass_cache_cookie: str | None = Field(default=None, max_length=120)
    cache_key_query_strings: list[str] = Field(default_factory=list)
    redirect_to: str | None = Field(default=None, max_length=512)
    redirect_status: int = Field(default=302)
    methods: list[str] = Field(default_factory=lambda: ["GET", "HEAD"])
    headers_add: dict[str, Any] = Field(default_factory=dict)
    headers_remove: list[str] = Field(default_factory=list)
    priority: int = Field(default=100, ge=0, le=10_000)
    enabled: bool = True

    @field_validator("redirect_status")
    @classmethod
    def _v_rs(cls, v: int) -> int:
        if v not in REDIRECT_STATUSES:
            raise ValueError(f"redirect_status phải thuộc {sorted(REDIRECT_STATUSES)}")
        return v

    @field_validator("methods")
    @classmethod
    def _v_methods(cls, v: list[str]) -> list[str]:
        out = []
        for m in v:
            mu = (m or "").upper().strip()
            if mu in ALLOWED_METHODS:
                out.append(mu)
        return out or ["GET", "HEAD"]


class RoutePatchIn(BaseModel):
    path_pattern: str | None = None
    origin_url: str | None = None
    cache_ttl_seconds: int | None = None
    cache_browser_ttl: int | None = None
    bypass_cache_cookie: str | None = None
    cache_key_query_strings: list[str] | None = None
    redirect_to: str | None = None
    redirect_status: int | None = None
    methods: list[str] | None = None
    headers_add: dict[str, Any] | None = None
    headers_remove: list[str] | None = None
    priority: int | None = None
    enabled: bool | None = None


_ROUTE_COLS = (
    "id, zone_id, path_pattern, origin_url, cache_ttl_seconds, cache_browser_ttl, "
    "bypass_cache_cookie, cache_key_query_strings, redirect_to, redirect_status, "
    "methods, headers_add, headers_remove, priority, enabled, created_at, updated_at"
)


@router.post("/zones/{zone_id}/routes", status_code=201)
async def create_route(
    zone_id: int,
    data: RouteIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    await _load_zone(db, zone_id, ws)

    cnt = (await db.execute(
        text("SELECT COUNT(*) FROM cdn_routes WHERE zone_id = :z"),
        {"z": zone_id},
    )).scalar_one()
    if int(cnt or 0) >= MAX_ROUTES_PER_ZONE:
        raise HTTPException(400, f"vượt quá {MAX_ROUTES_PER_ZONE} routes / zone")

    try:
        row = (await db.execute(text(f"""
            INSERT INTO cdn_routes
              (zone_id, path_pattern, origin_url, cache_ttl_seconds, cache_browser_ttl,
               bypass_cache_cookie, cache_key_query_strings, redirect_to, redirect_status,
               methods, headers_add, headers_remove, priority, enabled)
            VALUES
              (:z, :pp, :ou, :ttl, :bttl,
               :bcc, :cqs, :rt, :rs,
               :m, CAST(:ha AS JSONB), :hr, :p, :en)
            RETURNING {_ROUTE_COLS}
        """), {
            "z": zone_id, "pp": data.path_pattern, "ou": data.origin_url,
            "ttl": data.cache_ttl_seconds, "bttl": data.cache_browser_ttl,
            "bcc": data.bypass_cache_cookie,
            "cqs": list(data.cache_key_query_strings or []),
            "rt": data.redirect_to, "rs": data.redirect_status,
            "m": data.methods, "ha": json.dumps(data.headers_add or {}),
            "hr": list(data.headers_remove or []),
            "p": data.priority, "en": data.enabled,
        })).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("create_route failed zone=%s", zone_id)
        raise HTTPException(502, f"không tạo được route: {type(e).__name__}")

    out = _row_route(row)
    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="edge.route.create", target=f"route#{out['id']}",
                         severity="ok",
                         metadata={"zone_id": zone_id, "pattern": data.path_pattern})
        await db.commit()
    except Exception:
        await db.rollback()
    return out


@router.get("/zones/{zone_id}/routes")
async def list_routes(
    zone_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _load_zone(db, zone_id, ws)

    rows = (await db.execute(text(f"""
        SELECT {_ROUTE_COLS} FROM cdn_routes
         WHERE zone_id = :z
         ORDER BY priority ASC, id ASC
    """), {"z": zone_id})).all()
    return {"zone_id": zone_id, "count": len(rows), "routes": [_row_route(r) for r in rows]}


@router.patch("/zones/{zone_id}/routes/{route_id}")
async def patch_route(
    zone_id: int,
    route_id: int,
    data: RoutePatchIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    await _load_zone(db, zone_id, ws)

    if data.redirect_status is not None and data.redirect_status not in REDIRECT_STATUSES:
        raise HTTPException(400, f"redirect_status phải thuộc {sorted(REDIRECT_STATUSES)}")

    sets: list[str] = []
    params: dict[str, Any] = {"id": route_id, "z": zone_id}

    simple_fields = ("path_pattern", "origin_url", "cache_ttl_seconds",
                     "cache_browser_ttl", "bypass_cache_cookie",
                     "redirect_to", "redirect_status", "priority", "enabled")
    for f in simple_fields:
        v = getattr(data, f)
        if v is not None:
            sets.append(f"{f} = :{f}")
            params[f] = v
    if data.cache_key_query_strings is not None:
        sets.append("cache_key_query_strings = :cqs")
        params["cqs"] = list(data.cache_key_query_strings)
    if data.methods is not None:
        sets.append("methods = :m")
        params["m"] = [m.upper() for m in data.methods if m.upper() in ALLOWED_METHODS]
    if data.headers_add is not None:
        sets.append("headers_add = CAST(:ha AS JSONB)")
        params["ha"] = json.dumps(data.headers_add)
    if data.headers_remove is not None:
        sets.append("headers_remove = :hr")
        params["hr"] = list(data.headers_remove)

    if not sets:
        raise HTTPException(400, "không có field nào cần update")

    sql = f"""
        UPDATE cdn_routes SET {', '.join(sets)}
         WHERE id = :id AND zone_id = :z
         RETURNING {_ROUTE_COLS}
    """
    try:
        row = (await db.execute(text(sql), params)).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("patch_route failed id=%s", route_id)
        raise HTTPException(502, f"update failed: {type(e).__name__}")
    if not row:
        raise HTTPException(404, "route không tồn tại")
    return _row_route(row)


@router.delete("/zones/{zone_id}/routes/{route_id}")
async def delete_route(
    zone_id: int,
    route_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    await _load_zone(db, zone_id, ws)

    res = await db.execute(text("""
        DELETE FROM cdn_routes WHERE id = :id AND zone_id = :z
    """), {"id": route_id, "z": zone_id})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(404, "route không tồn tại")

    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="edge.route.delete", target=f"route#{route_id}",
                         severity="warn", metadata={"zone_id": zone_id})
        await db.commit()
    except Exception:
        await db.rollback()
    return {"deleted": True, "id": route_id}


# ════════════════════════════════════════════════════════════════════════════
# 3. CACHE PURGE
# ════════════════════════════════════════════════════════════════════════════
class PurgeIn(BaseModel):
    purge_type: str = Field(default="all")
    targets: list[str] = Field(default_factory=list)

    @field_validator("purge_type")
    @classmethod
    def _v_pt(cls, v: str) -> str:
        if v not in PURGE_TYPES:
            raise ValueError(f"purge_type phải thuộc {sorted(PURGE_TYPES)}")
        return v


@router.post("/zones/{zone_id}/purge")
async def purge_zone_cache(
    zone_id: int,
    data: PurgeIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    z = await _load_zone(db, zone_id, ws)

    targets = (data.targets or [])[:MAX_PURGE_TARGETS]
    if data.purge_type != "all" and not targets:
        raise HTTPException(400, "purge_type khác 'all' phải kèm targets[]")

    # Insert log row first (status pending)
    log_row = (await db.execute(text("""
        INSERT INTO cdn_cache_purge_log
          (workspace_id, zone_id, purge_type, targets, purged_by, status)
        VALUES (:ws, :z, :pt, :tg, :by, 'pending')
        RETURNING id
    """), {
        "ws": ws, "z": zone_id, "pt": data.purge_type,
        "tg": targets, "by": me.email,
    })).first()
    await db.commit()
    log_id = int(log_row[0])

    # Call provider
    try:
        result = await purge_cache(
            provider=z["cdn_provider"],
            provider_zone_id=z.get("provider_zone_id"),
            purge_type=data.purge_type,
            targets=targets,
        )
        new_status = result.get("status", "success")
        err_msg = result.get("error")
    except Exception as e:
        result = {"job_id": None, "duration_ms": None, "stub": True}
        new_status = "failed"
        err_msg = str(e)[:500]

    # Update log row
    try:
        await db.execute(text("""
            UPDATE cdn_cache_purge_log
               SET status = :st, provider_job_id = :jid,
                   error_message = :err, duration_ms = :dur
             WHERE id = :id
        """), {
            "st": new_status, "jid": result.get("job_id"),
            "err": err_msg, "dur": result.get("duration_ms"),
            "id": log_id,
        })
        await db.commit()
    except Exception:
        await db.rollback()

    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="edge.cache.purge", target=f"zone#{zone_id}",
                         severity="ok" if new_status == "success" else "warn",
                         metadata={"purge_type": data.purge_type, "count": len(targets)})
        await db.commit()
    except Exception:
        await db.rollback()

    return {
        "log_id": log_id,
        "zone_id": zone_id,
        "purge_type": data.purge_type,
        "targets_count": len(targets),
        "status": new_status,
        "provider_job_id": result.get("job_id"),
        "duration_ms": result.get("duration_ms"),
        "stub": result.get("stub", False),
    }


@router.get("/zones/{zone_id}/purge-history")
async def purge_history(
    zone_id: int,
    ws: str,
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _load_zone(db, zone_id, ws)

    rows = (await db.execute(text("""
        SELECT id, purge_type, targets, purged_by, purged_at,
               status, provider_job_id, error_message, duration_ms
          FROM cdn_cache_purge_log
         WHERE zone_id = :z AND workspace_id = :ws
         ORDER BY purged_at DESC
         LIMIT :lim
    """), {"z": zone_id, "ws": ws, "lim": limit})).all()

    return {
        "zone_id": zone_id,
        "count": len(rows),
        "history": [{
            "id": int(r[0]), "purge_type": r[1],
            "targets": list(r[2] or []), "purged_by": r[3],
            "purged_at": _isoformat(r[4]), "status": r[5],
            "provider_job_id": r[6], "error_message": r[7],
            "duration_ms": int(r[8]) if r[8] is not None else None,
        } for r in rows],
    }


# ════════════════════════════════════════════════════════════════════════════
# 4. SECURITY RULES
# ════════════════════════════════════════════════════════════════════════════
class SecurityRuleIn(BaseModel):
    rule_type: str
    rule_config: dict[str, Any] = Field(default_factory=dict)
    action: str = "block"
    description: str | None = Field(default=None, max_length=500)
    enabled: bool = True
    priority: int = Field(default=100, ge=0, le=10_000)

    @field_validator("rule_type")
    @classmethod
    def _v_rt(cls, v: str) -> str:
        if v not in SECURITY_RULE_TYPES:
            raise ValueError(f"rule_type phải thuộc {sorted(SECURITY_RULE_TYPES)}")
        return v

    @field_validator("action")
    @classmethod
    def _v_act(cls, v: str) -> str:
        if v not in SECURITY_ACTIONS:
            raise ValueError(f"action phải thuộc {sorted(SECURITY_ACTIONS)}")
        return v


class SecurityRulePatchIn(BaseModel):
    rule_config: dict[str, Any] | None = None
    action: str | None = None
    description: str | None = None
    enabled: bool | None = None
    priority: int | None = None


_SEC_COLS = (
    "id, zone_id, rule_type, rule_config, action, description, enabled, "
    "priority, provider_rule_id, hits_count, last_hit_at, created_at, updated_at"
)


@router.post("/zones/{zone_id}/security", status_code=201)
async def create_security_rule(
    zone_id: int,
    data: SecurityRuleIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    z = await _load_zone(db, zone_id, ws)

    cnt = (await db.execute(
        text("SELECT COUNT(*) FROM cdn_security_rules WHERE zone_id = :z"),
        {"z": zone_id},
    )).scalar_one()
    if int(cnt or 0) >= MAX_SECURITY_RULES_PER_ZONE:
        raise HTTPException(400, f"vượt quá {MAX_SECURITY_RULES_PER_ZONE} rules / zone")

    # Sync to provider (best-effort)
    try:
        sync = await apply_security_rule(
            provider=z["cdn_provider"],
            provider_zone_id=z.get("provider_zone_id"),
            rule_type=data.rule_type,
            rule_config=data.rule_config,
            action=data.action,
            enabled=data.enabled,
        )
    except Exception as e:
        log.warning("apply_security_rule failed: %s", e)
        sync = {"provider_rule_id": None, "applied": False, "error": str(e)[:200]}

    try:
        row = (await db.execute(text(f"""
            INSERT INTO cdn_security_rules
              (zone_id, rule_type, rule_config, action, description, enabled, priority,
               provider_rule_id)
            VALUES
              (:z, :rt, CAST(:cfg AS JSONB), :act, :desc, :en, :p, :prid)
            RETURNING {_SEC_COLS}
        """), {
            "z": zone_id, "rt": data.rule_type,
            "cfg": json.dumps(data.rule_config or {}),
            "act": data.action, "desc": data.description,
            "en": data.enabled, "p": data.priority,
            "prid": sync.get("provider_rule_id"),
        })).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("create_security_rule insert failed")
        raise HTTPException(502, f"không tạo được security rule: {type(e).__name__}")

    out = _row_security(row)
    out["sync"] = sync
    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="edge.security.create", target=f"sec#{out['id']}",
                         severity="warn",
                         metadata={"zone_id": zone_id, "rule_type": data.rule_type, "action": data.action})
        await db.commit()
    except Exception:
        await db.rollback()
    return out


@router.get("/zones/{zone_id}/security")
async def list_security_rules(
    zone_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _load_zone(db, zone_id, ws)

    rows = (await db.execute(text(f"""
        SELECT {_SEC_COLS} FROM cdn_security_rules
         WHERE zone_id = :z
         ORDER BY priority ASC, id ASC
    """), {"z": zone_id})).all()
    return {"zone_id": zone_id, "count": len(rows),
            "rules": [_row_security(r) for r in rows]}


@router.patch("/zones/{zone_id}/security/{rule_id}")
async def patch_security_rule(
    zone_id: int,
    rule_id: int,
    data: SecurityRulePatchIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    await _load_zone(db, zone_id, ws)

    if data.action is not None and data.action not in SECURITY_ACTIONS:
        raise HTTPException(400, f"action phải thuộc {sorted(SECURITY_ACTIONS)}")

    sets: list[str] = []
    params: dict[str, Any] = {"id": rule_id, "z": zone_id}
    for f in ("action", "description", "enabled", "priority"):
        v = getattr(data, f)
        if v is not None:
            sets.append(f"{f} = :{f}")
            params[f] = v
    if data.rule_config is not None:
        sets.append("rule_config = CAST(:cfg AS JSONB)")
        params["cfg"] = json.dumps(data.rule_config)
    if not sets:
        raise HTTPException(400, "không có field nào cần update")

    sql = f"""
        UPDATE cdn_security_rules SET {', '.join(sets)}
         WHERE id = :id AND zone_id = :z
         RETURNING {_SEC_COLS}
    """
    try:
        row = (await db.execute(text(sql), params)).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("patch_security_rule failed id=%s", rule_id)
        raise HTTPException(502, f"update failed: {type(e).__name__}")
    if not row:
        raise HTTPException(404, "rule không tồn tại")
    return _row_security(row)


@router.delete("/zones/{zone_id}/security/{rule_id}")
async def delete_security_rule(
    zone_id: int,
    rule_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)
    await _load_zone(db, zone_id, ws)

    res = await db.execute(text("""
        DELETE FROM cdn_security_rules WHERE id = :id AND zone_id = :z
    """), {"id": rule_id, "z": zone_id})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(404, "rule không tồn tại")

    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="edge.security.delete", target=f"sec#{rule_id}",
                         severity="warn", metadata={"zone_id": zone_id})
        await db.commit()
    except Exception:
        await db.rollback()
    return {"deleted": True, "id": rule_id}


# ════════════════════════════════════════════════════════════════════════════
# 5. CERTIFICATES
# ════════════════════════════════════════════════════════════════════════════
class CertIssueIn(BaseModel):
    domain: str = Field(..., min_length=3, max_length=253)
    cert_type: str = Field(default="lets_encrypt")
    san_domains: list[str] = Field(default_factory=list)
    zone_id: int | None = None
    auto_renew: bool = True
    acme_challenge: str = Field(default="http-01")

    @field_validator("domain")
    @classmethod
    def _v_dom(cls, v: str) -> str:
        if not is_valid_domain(v):
            raise ValueError(f"domain không hợp lệ: {v}")
        return v.lower()

    @field_validator("cert_type")
    @classmethod
    def _v_ct(cls, v: str) -> str:
        if v not in CERT_TYPES:
            raise ValueError(f"cert_type phải thuộc {sorted(CERT_TYPES)}")
        return v


@router.get("/certificates")
async def list_certificates(
    ws: str,
    domain: str | None = None,
    status: str | None = None,
    expiring_within_days: int | None = Query(default=None, ge=0, le=365),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)

    clauses = ["workspace_id = :ws"]
    params: dict[str, Any] = {"ws": ws, "lim": limit, "off": offset}
    if domain:
        clauses.append("domain = :dom")
        params["dom"] = domain.lower()
    if status:
        if status not in CERT_STATUSES:
            raise HTTPException(400, f"status phải thuộc {sorted(CERT_STATUSES)}")
        clauses.append("status = :st")
        params["st"] = status
    if expiring_within_days is not None:
        threshold = _now() + timedelta(days=expiring_within_days)
        clauses.append("expires_at IS NOT NULL AND expires_at <= :th")
        params["th"] = threshold

    where = " AND ".join(clauses)
    rows = (await db.execute(text(f"""
        SELECT {_CERT_COLS} FROM cdn_certificates
         WHERE {where}
         ORDER BY created_at DESC
         LIMIT :lim OFFSET :off
    """), params)).all()
    total = (await db.execute(
        text(f"SELECT COUNT(*) FROM cdn_certificates WHERE {where}"),
        {k: v for k, v in params.items() if k not in ("lim", "off")}
    )).scalar_one()
    return {
        "workspace_id": ws,
        "count": len(rows), "total": int(total or 0),
        "limit": limit, "offset": offset,
        "certificates": [_row_cert(r) for r in rows],
    }


@router.post("/certificates/issue", status_code=201)
async def issue_cert(
    data: CertIssueIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)

    # Validate zone ownership if provided
    if data.zone_id is not None:
        await _load_zone(db, data.zone_id, ws)

    try:
        res = await issue_certificate(
            domain=data.domain,
            cert_type=data.cert_type,
            san_domains=data.san_domains,
            acme_challenge=data.acme_challenge,
        )
    except Exception as e:
        log.exception("issue_certificate failed domain=%s", data.domain)
        raise HTTPException(502, f"không issue được cert: {e}")

    try:
        row = (await db.execute(text(f"""
            INSERT INTO cdn_certificates
              (workspace_id, zone_id, domain, san_domains, cert_type,
               key_pem_secret_ref, fingerprint_sha256, issuer,
               issued_at, expires_at, status, auto_renew, acme_challenge)
            VALUES
              (:ws, :zid, :dom, :san, :ct,
               :sref, :fp, :iss,
               :ia, :exp, :st, :ar, :ac)
            RETURNING {_CERT_COLS}
        """), {
            "ws": ws, "zid": data.zone_id, "dom": data.domain,
            "san": list(data.san_domains or []),
            "ct": data.cert_type,
            "sref": res.get("secret_ref"),
            "fp": res.get("fingerprint"),
            "iss": res.get("issuer"),
            "ia": res.get("issued_at"),
            "exp": res.get("expires_at"),
            "st": res.get("status", "pending"),
            "ar": data.auto_renew,
            "ac": data.acme_challenge,
        })).first()
        await db.commit()
    except Exception as e:
        await db.rollback()
        log.exception("cert insert failed domain=%s", data.domain)
        raise HTTPException(502, f"không lưu được cert: {type(e).__name__}")

    out = _row_cert(row)
    out["provisioning_note"] = res.get("note")

    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="edge.cert.issue", target=f"cert#{out['id']}",
                         severity="ok",
                         metadata={"domain": data.domain, "cert_type": data.cert_type})
        await db.commit()
    except Exception:
        await db.rollback()
    return out


@router.post("/certificates/{cert_id}/renew")
async def renew_cert(
    cert_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)

    row = (await db.execute(text("""
        SELECT id, domain, cert_type FROM cdn_certificates
         WHERE id = :id AND workspace_id = :ws
    """), {"id": cert_id, "ws": ws})).first()
    if not row:
        raise HTTPException(404, "cert không tồn tại")

    res = await renew_certificate(
        cert_id=int(row[0]), domain=row[1], cert_type=row[2], db=db,
    )
    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="edge.cert.renew", target=f"cert#{cert_id}",
                         severity="ok" if res.get("renewed") else "warn",
                         metadata={"domain": row[1]})
        await db.commit()
    except Exception:
        await db.rollback()
    return {"cert_id": cert_id, "domain": row[1], **res}


@router.delete("/certificates/{cert_id}")
async def delete_cert(
    cert_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    _require_writer(me)

    row = (await db.execute(text("""
        SELECT domain FROM cdn_certificates
         WHERE id = :id AND workspace_id = :ws
    """), {"id": cert_id, "ws": ws})).first()
    if not row:
        raise HTTPException(404, "cert không tồn tại")

    await db.execute(text("""
        DELETE FROM cdn_certificates WHERE id = :id AND workspace_id = :ws
    """), {"id": cert_id, "ws": ws})
    await db.commit()

    try:
        await audit_push(db, actor=me.email, workspace_id=ws,
                         action="edge.cert.delete", target=f"cert#{cert_id}",
                         severity="warn", metadata={"domain": row[0]})
        await db.commit()
    except Exception:
        await db.rollback()
    return {"deleted": True, "id": cert_id, "domain": row[0]}


# ════════════════════════════════════════════════════════════════════════════
# 6. ANALYTICS
# ════════════════════════════════════════════════════════════════════════════
@router.get("/zones/{zone_id}/analytics")
async def zone_analytics(
    zone_id: int,
    ws: str,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    await _load_zone(db, zone_id, ws)

    today = date.today()
    df = today - timedelta(days=29)
    dt = today
    try:
        if from_:
            df = date.fromisoformat(from_)
        if to:
            dt = date.fromisoformat(to)
    except ValueError:
        raise HTTPException(400, "from/to phải đúng format YYYY-MM-DD")
    if df > dt:
        raise HTTPException(400, "from > to")
    if (dt - df).days > 365:
        raise HTTPException(400, "khoảng thời gian không quá 365 ngày")

    rows = (await db.execute(text("""
        SELECT date, requests, bandwidth_gb, cache_hit_rate,
               threats_blocked, unique_visitors,
               requests_2xx, requests_3xx, requests_4xx, requests_5xx,
               avg_response_ms, p95_response_ms, bytes_saved_gb,
               top_countries, top_paths
          FROM cdn_analytics_daily
         WHERE zone_id = :z AND date BETWEEN :df AND :dt
         ORDER BY date ASC
    """), {"z": zone_id, "df": df, "dt": dt})).all()

    daily = [{
        "date": r[0].isoformat() if r[0] else None,
        "requests": int(r[1] or 0),
        "bandwidth_gb": float(r[2] or 0),
        "cache_hit_rate": float(r[3] or 0),
        "threats_blocked": int(r[4] or 0),
        "unique_visitors": int(r[5] or 0),
        "requests_2xx": int(r[6] or 0),
        "requests_3xx": int(r[7] or 0),
        "requests_4xx": int(r[8] or 0),
        "requests_5xx": int(r[9] or 0),
        "avg_response_ms": int(r[10] or 0),
        "p95_response_ms": int(r[11] or 0),
        "bytes_saved_gb": float(r[12] or 0),
        "top_countries": _serialize_jsonb(r[13]) or [],
        "top_paths": _serialize_jsonb(r[14]) or [],
    } for r in rows]

    # Summary
    total_req = sum(d["requests"] for d in daily)
    total_bw = sum(d["bandwidth_gb"] for d in daily)
    total_threats = sum(d["threats_blocked"] for d in daily)
    avg_chr = (sum(d["cache_hit_rate"] for d in daily) / len(daily)) if daily else 0.0
    total_saved = sum(d["bytes_saved_gb"] for d in daily)

    return {
        "zone_id": zone_id,
        "from": df.isoformat(),
        "to": dt.isoformat(),
        "summary": {
            "total_requests": total_req,
            "total_bandwidth_gb": round(total_bw, 4),
            "avg_cache_hit_rate": round(avg_chr, 2),
            "total_threats_blocked": total_threats,
            "total_bandwidth_saved_gb": round(total_saved, 4),
            "days": len(daily),
        },
        "daily": daily,
    }


@router.get("/zones/{zone_id}/realtime")
async def zone_realtime(
    zone_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    _check_scope(me)
    z = await _load_zone(db, zone_id, ws)

    metrics = await fetch_realtime_metrics(
        provider=z["cdn_provider"],
        provider_zone_id=z.get("provider_zone_id"),
    )
    return {
        "zone_id": zone_id,
        "domain": z["domain"],
        "provider": z["cdn_provider"],
        **metrics,
    }


# ════════════════════════════════════════════════════════════════════════════
# End of /edge router
# ════════════════════════════════════════════════════════════════════════════
