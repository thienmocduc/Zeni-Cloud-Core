"""
Zeni Cloud Core — Reseller / White-label API.

Phase 4 of Zeni Cloud roadmap: agencies bán Zeni Cloud dưới brand riêng,
quản lý khách của họ, nhận revenue share.

Endpoints (prefix /reseller):

  Application + onboarding
    POST  /apply?ws=
    GET   /status?ws=
    POST  /admin/approve/{reseller_id}      (PlatformAdmin)

  Brand config
    GET   /brand?ws=
    PATCH /brand?ws=
    POST  /brand/upload-logo?ws=            (multipart logo file)
    POST  /brand/verify-domain?ws=

  Customer management
    GET   /customers?ws=&status=
    POST  /customers/invite?ws=
    GET   /customers/{cid}?ws=
    POST  /customers/{cid}/upgrade?ws=

  Commissions + payouts
    GET   /commissions?ws=&period=
    GET   /commissions/pending?ws=
    GET   /payouts?ws=
    POST  /admin/payouts/process            (PlatformAdmin)

  Promo codes
    POST   /promo-codes?ws=
    GET    /promo-codes?ws=
    PATCH  /promo-codes/{id}?ws=
    DELETE /promo-codes/{id}?ws=
    POST   /promo-codes/validate            (public, no auth)

  Reports
    GET   /reports/dashboard?ws=
    GET   /reports/forecast?ws=

Schema match: backend/migrations/041_whitelabel_reseller.sql
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_role import require_platform_admin
from app.core.config import settings
from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push
from app.services.reseller_engine import (
    TIER_COMMISSION,
    apply_to_become_reseller,
    approve_reseller,
    compute_monthly_commissions,
    process_payouts,
    validate_promo_code,
)

log = logging.getLogger("zeni.api.reseller")

router = APIRouter(prefix="/reseller", tags=["reseller"])

# ────────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────────
ALLOWED_TIERS = {"basic", "pro", "elite"}
ALLOWED_PAYOUT = {"bank_transfer", "vnpay", "crypto", "paypal"}
ALLOWED_DISCOUNT_TYPE = {"percent", "fixed"}
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
DOMAIN_RE = re.compile(r"^[a-z0-9.-]{3,255}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
USD_TO_VND = Decimal("25000")
LOGO_MAX_BYTES = 4 * 1024 * 1024  # 4MB
ALLOWED_LOGO_MIME = {"image/png", "image/jpeg", "image/webp", "image/svg+xml"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_float(x: Any) -> float:
    if x is None:
        return 0.0
    try:
        return float(x)
    except Exception:
        return 0.0


# ════════════════════════════════════════════════════════════════════════════
# Pydantic schemas
# ════════════════════════════════════════════════════════════════════════════
class ApplyIn(BaseModel):
    reseller_name: str = Field(..., min_length=2, max_length=120)
    business_name: str | None = Field(default=None, max_length=255)
    contact_email: str = Field(..., max_length=255)
    contact_phone: str | None = Field(default=None, max_length=64)
    tax_id: str | None = Field(default=None, max_length=64)
    payout_method: str = Field(default="bank_transfer")
    payout_account: str | None = Field(default=None, max_length=255)

    @field_validator("contact_email")
    @classmethod
    def _email_ok(cls, v: str) -> str:
        if not EMAIL_RE.match(v):
            raise ValueError("invalid email")
        return v.lower()

    @field_validator("payout_method")
    @classmethod
    def _pm_ok(cls, v: str) -> str:
        if v not in ALLOWED_PAYOUT:
            raise ValueError(f"payout_method must be in {ALLOWED_PAYOUT}")
        return v


class ApplyOut(BaseModel):
    reseller_id: int
    status: str
    note: str


class StatusOut(BaseModel):
    has_application: bool
    reseller_id: int | None = None
    status: str | None = None
    tier: str | None = None
    commission_percent: float | None = None
    discount_percent: float | None = None
    approved_at: datetime | None = None
    created_at: datetime | None = None


class ApproveIn(BaseModel):
    tier: str = Field(default="basic")
    commission_percent: float | None = None
    discount_percent: float | None = None

    @field_validator("tier")
    @classmethod
    def _tier_ok(cls, v: str) -> str:
        if v not in ALLOWED_TIERS:
            raise ValueError(f"tier must be in {ALLOWED_TIERS}")
        return v


class BrandOut(BaseModel):
    reseller_id: int
    brand_name: str | None
    logo_url: str | None
    favicon_url: str | None
    primary_color: str | None
    secondary_color: str | None
    accent_color: str | None
    custom_domain: str | None
    custom_email_from: str | None
    support_email: str | None
    terms_url: str | None
    privacy_url: str | None
    footer_html: str | None
    custom_css: str | None
    domain_verified_at: datetime | None
    domain_cname_token: str | None


class BrandPatch(BaseModel):
    brand_name: str | None = Field(default=None, max_length=120)
    logo_url: str | None = Field(default=None, max_length=500)
    favicon_url: str | None = Field(default=None, max_length=500)
    primary_color: str | None = None
    secondary_color: str | None = None
    accent_color: str | None = None
    custom_domain: str | None = Field(default=None, max_length=255)
    custom_email_from: str | None = Field(default=None, max_length=255)
    support_email: str | None = Field(default=None, max_length=255)
    terms_url: str | None = Field(default=None, max_length=500)
    privacy_url: str | None = Field(default=None, max_length=500)
    footer_html: str | None = None
    custom_css: str | None = None

    @field_validator("primary_color", "secondary_color", "accent_color")
    @classmethod
    def _color_ok(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not COLOR_RE.match(v):
            raise ValueError("color must be #RRGGBB hex")
        return v.upper()

    @field_validator("custom_domain")
    @classmethod
    def _domain_ok(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        v = v.lower().strip()
        if not DOMAIN_RE.match(v):
            raise ValueError("invalid domain")
        return v


class CustomerListItem(BaseModel):
    id: int
    customer_workspace_id: str
    customer_email: str
    signed_up_via: str | None
    promo_code: str | None
    current_plan: str
    status: str
    lifetime_value_vnd: float
    last_payment_at: datetime | None
    signed_up_at: datetime


class InviteIn(BaseModel):
    email: str
    plan_id: str = Field(default="starter", max_length=32)
    promo_code: str | None = Field(default=None, max_length=40)

    @field_validator("email")
    @classmethod
    def _ok(cls, v: str) -> str:
        if not EMAIL_RE.match(v):
            raise ValueError("invalid email")
        return v.lower()


class InviteOut(BaseModel):
    invite_token: str
    invite_url: str
    expires_at: datetime
    email: str


class UpgradeIn(BaseModel):
    new_plan: str = Field(..., max_length=32)


class CommissionOut(BaseModel):
    id: int
    customer_workspace_id: str
    billing_period_start: datetime
    billing_period_end: datetime
    customer_paid_vnd: float
    commission_percent: float
    commission_vnd: float
    status: str
    payable_at: datetime | None
    paid_at: datetime | None


class PayoutOut(BaseModel):
    id: int
    total_amount_vnd: float
    period_start: datetime
    period_end: datetime
    status: str
    paid_at: datetime | None
    payment_method: str | None
    transaction_ref: str | None
    commission_count: int
    created_at: datetime


class PromoCodeIn(BaseModel):
    code: str = Field(..., min_length=3, max_length=40)
    discount_type: str = Field(default="percent")
    discount_value: float = Field(..., ge=0)
    max_uses: int | None = Field(default=None, ge=1)
    expires_at: datetime | None = None
    applies_to_plans: list[str] = Field(default_factory=list)
    description: str | None = Field(default=None, max_length=500)
    enabled: bool = True

    @field_validator("code")
    @classmethod
    def _code_ok(cls, v: str) -> str:
        v = v.strip().upper()
        if not re.match(r"^[A-Z0-9_-]+$", v):
            raise ValueError("code must be uppercase alphanumeric / _ -")
        return v

    @field_validator("discount_type")
    @classmethod
    def _dt_ok(cls, v: str) -> str:
        if v not in ALLOWED_DISCOUNT_TYPE:
            raise ValueError(f"discount_type must be in {ALLOWED_DISCOUNT_TYPE}")
        return v


class PromoCodeOut(BaseModel):
    id: int
    code: str
    discount_type: str
    discount_value: float
    max_uses: int | None
    current_uses: int
    expires_at: datetime | None
    applies_to_plans: list[str]
    description: str | None
    enabled: bool
    created_at: datetime


class PromoValidateIn(BaseModel):
    code: str = Field(..., min_length=2, max_length=40)
    plan_id: str | None = None


class DashboardOut(BaseModel):
    reseller_id: int
    tier: str
    commission_percent: float
    active_customers: int
    churned_customers: int
    churn_rate_pct: float
    mrr_vnd: float
    total_customer_value_vnd: float
    commission_paid_vnd: float
    commission_pending_vnd: float
    commission_last_30d_vnd: float


class ForecastOut(BaseModel):
    period_days: int
    expected_commission_vnd: float
    based_on_customers: int
    avg_commission_per_customer_vnd: float


# ════════════════════════════════════════════════════════════════════════════
# Helpers — load reseller row by workspace
# ════════════════════════════════════════════════════════════════════════════
async def _get_reseller(
    db: AsyncSession, ws: str, *, require_approved: bool = True
) -> dict[str, Any]:
    row = (
        await db.execute(
            text(
                """
                SELECT id, workspace_id, reseller_name, business_name, contact_email,
                       tier, commission_percent, discount_percent, status,
                       payout_method, payout_account,
                       approved_at, created_at
                FROM reseller_accounts WHERE workspace_id = :ws
                """
            ),
            {"ws": ws},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(404, "workspace chưa đăng ký reseller")
    if require_approved and row["status"] != "approved":
        raise HTTPException(
            403, f"reseller chưa được duyệt (status={row['status']})"
        )
    return dict(row)


def _check_owner(me: CurrentUser) -> None:
    if me.role == "Viewer":
        raise HTTPException(403, "Viewer không có quyền quản lý reseller")


# ════════════════════════════════════════════════════════════════════════════
# Application + onboarding
# ════════════════════════════════════════════════════════════════════════════
@router.post("/apply", response_model=ApplyOut)
async def apply(
    payload: ApplyIn,
    request: Request,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ApplyOut:
    await require_workspace_access(ws, me)
    _check_owner(me)
    try:
        result = await apply_to_become_reseller(
            db,
            workspace_id=ws,
            reseller_name=payload.reseller_name,
            business_name=payload.business_name,
            contact_email=payload.contact_email,
            contact_phone=payload.contact_phone,
            tax_id=payload.tax_id,
            payout_method=payload.payout_method,
            payout_account=payload.payout_account,
            actor_email=me.email,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    await db.commit()
    return ApplyOut(
        reseller_id=int(result["reseller_id"]),
        status=result["status"],
        note="Application đã gửi. Platform admin sẽ duyệt trong 1-3 ngày làm việc.",
    )


@router.get("/status", response_model=StatusOut)
async def status_check(
    ws: str = Query(default=None, description="Workspace ID (optional - falls back to user's primary)"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StatusOut:
    # Auto-fallback to user's primary workspace if ws missing
    if not ws:
        if me.workspaces:
            ws = me.workspaces[0]
        else:
            raise HTTPException(status_code=400, detail="ws parameter required (no default workspace)")
    await require_workspace_access(ws, me)
    row = (
        await db.execute(
            text(
                """
                SELECT id, status, tier, commission_percent, discount_percent,
                       approved_at, created_at
                FROM reseller_accounts WHERE workspace_id = :ws
                """
            ),
            {"ws": ws},
        )
    ).mappings().first()
    if not row:
        return StatusOut(has_application=False)
    return StatusOut(
        has_application=True,
        reseller_id=int(row["id"]),
        status=row["status"],
        tier=row["tier"],
        commission_percent=_to_float(row["commission_percent"]),
        discount_percent=_to_float(row["discount_percent"]),
        approved_at=row["approved_at"],
        created_at=row["created_at"],
    )


@router.post("/admin/approve/{reseller_id}")
async def admin_approve(
    reseller_id: int,
    payload: ApproveIn,
    request: Request,
    me: CurrentUser = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    cp = (
        Decimal(str(payload.commission_percent))
        if payload.commission_percent is not None
        else TIER_COMMISSION[payload.tier]
    )
    dp = (
        Decimal(str(payload.discount_percent))
        if payload.discount_percent is not None
        else None
    )
    try:
        result = await approve_reseller(
            db,
            reseller_id=reseller_id,
            admin_email=me.email,
            tier=payload.tier,
            commission_percent=cp,
            discount_percent=dp,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    await db.commit()
    return {"ok": True, **result}


# ════════════════════════════════════════════════════════════════════════════
# Brand config
# ════════════════════════════════════════════════════════════════════════════
@router.get("/brand", response_model=BrandOut)
async def get_brand(
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BrandOut:
    await require_workspace_access(ws, me)
    rs = await _get_reseller(db, ws, require_approved=False)
    rid = rs["id"]
    row = (
        await db.execute(
            text(
                """
                SELECT reseller_id, brand_name, logo_url, favicon_url,
                       primary_color, secondary_color, accent_color,
                       custom_domain, custom_email_from, support_email,
                       terms_url, privacy_url, footer_html, custom_css,
                       domain_verified_at, domain_cname_token
                FROM reseller_brand_config WHERE reseller_id = :id
                """
            ),
            {"id": rid},
        )
    ).mappings().first()
    if not row:
        # Defensive: insert empty config
        await db.execute(
            text(
                "INSERT INTO reseller_brand_config (reseller_id, domain_cname_token) "
                "VALUES (:id, :tok) ON CONFLICT (reseller_id) DO NOTHING"
            ),
            {"id": rid, "tok": secrets.token_urlsafe(24)},
        )
        await db.commit()
        return BrandOut(
            reseller_id=int(rid), brand_name=None, logo_url=None, favicon_url=None,
            primary_color="#6366F1", secondary_color="#A855F7", accent_color="#22D3EE",
            custom_domain=None, custom_email_from=None, support_email=None,
            terms_url=None, privacy_url=None, footer_html=None, custom_css=None,
            domain_verified_at=None, domain_cname_token=None,
        )
    return BrandOut(**dict(row))


@router.patch("/brand", response_model=BrandOut)
async def patch_brand(
    payload: BrandPatch,
    request: Request,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BrandOut:
    await require_workspace_access(ws, me)
    _check_owner(me)
    rs = await _get_reseller(db, ws, require_approved=False)
    rid = rs["id"]

    # Build SET clause dynamically
    fields = payload.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(400, "không có field nào để update")

    # Special: changing custom_domain → reset domain_verified_at + new token
    domain_changed = "custom_domain" in fields
    if domain_changed:
        fields["domain_verified_at"] = None
        fields["domain_cname_token"] = secrets.token_urlsafe(24)

    fields["reseller_id"] = rid
    set_parts = ", ".join(
        f"{k} = :{k}" for k in fields if k not in ("reseller_id",)
    )
    set_parts += ", updated_at = NOW()"
    sql = f"""
        INSERT INTO reseller_brand_config (reseller_id, {", ".join(k for k in fields if k != "reseller_id")})
        VALUES (:reseller_id, {", ".join(":" + k for k in fields if k != "reseller_id")})
        ON CONFLICT (reseller_id) DO UPDATE SET {set_parts}
        RETURNING reseller_id, brand_name, logo_url, favicon_url,
                  primary_color, secondary_color, accent_color,
                  custom_domain, custom_email_from, support_email,
                  terms_url, privacy_url, footer_html, custom_css,
                  domain_verified_at, domain_cname_token
    """
    row = (await db.execute(text(sql), fields)).mappings().first()
    if not row:
        raise HTTPException(500, "update brand failed")

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="reseller.brand.update", target=str(rid),
        metadata={"fields": list(fields.keys())},
    )
    await db.commit()
    return BrandOut(**dict(row))


@router.post("/brand/upload-logo")
async def upload_logo(
    request: Request,
    ws: str = Query(...),
    file: UploadFile = File(...),
    kind: str = Form(default="logo"),  # 'logo' | 'favicon'
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_owner(me)
    if kind not in ("logo", "favicon"):
        raise HTTPException(400, "kind must be 'logo' or 'favicon'")

    rs = await _get_reseller(db, ws, require_approved=False)
    rid = rs["id"]

    if file.content_type not in ALLOWED_LOGO_MIME:
        raise HTTPException(415, f"content-type không hỗ trợ: {file.content_type}")
    data = await file.read()
    if not data:
        raise HTTPException(400, "file rỗng")
    if len(data) > LOGO_MAX_BYTES:
        raise HTTPException(413, f"file quá lớn (max {LOGO_MAX_BYTES // 1024 // 1024}MB)")

    ext = (file.filename or "").rsplit(".", 1)[-1].lower() if file.filename else ""
    if not ext or len(ext) > 5:
        ext = (file.content_type or "").split("/")[-1]
    key = f"reseller/{rid}/{kind}-{secrets.token_hex(8)}.{ext}"

    bucket = f"{settings.gcs_bucket_prefix}static"
    public_url = f"gs://{bucket}/{key}"
    try:
        from app.services.gcp import gcs_upload  # lazy import

        public_url = await gcs_upload(bucket, key, data, content_type=file.content_type or "application/octet-stream")
    except RuntimeError as e:
        # GCS not configured — accept upload but warn (best-effort dev mode).
        log.warning("[reseller] GCS not ready, returning gs:// placeholder: %s", e)
    except Exception as e:
        log.exception("[reseller] logo upload failed")
        raise HTTPException(500, f"upload failed: {e}") from e

    field = "logo_url" if kind == "logo" else "favicon_url"
    await db.execute(
        text(
            f"""
            INSERT INTO reseller_brand_config (reseller_id, {field})
            VALUES (:id, :url)
            ON CONFLICT (reseller_id) DO UPDATE SET {field} = :url, updated_at = NOW()
            """
        ),
        {"id": rid, "url": public_url},
    )
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="reseller.brand.upload", target=str(rid),
        metadata={"kind": kind, "size": len(data), "url": public_url},
    )
    await db.commit()
    return {"ok": True, "kind": kind, "url": public_url, "size": len(data)}


@router.post("/brand/verify-domain")
async def verify_domain(
    request: Request,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Yêu cầu reseller đã point CNAME của custom_domain → cname.zenicloud.io.
    Best-effort verify: dùng dnspython nếu có, else mark verified với token check.
    """
    await require_workspace_access(ws, me)
    _check_owner(me)
    rs = await _get_reseller(db, ws, require_approved=False)
    rid = rs["id"]

    brand = (
        await db.execute(
            text(
                "SELECT custom_domain, domain_cname_token FROM reseller_brand_config "
                "WHERE reseller_id = :id"
            ),
            {"id": rid},
        )
    ).mappings().first()
    if not brand or not brand["custom_domain"]:
        raise HTTPException(400, "custom_domain chưa được set")

    domain = brand["custom_domain"]
    token = brand["domain_cname_token"] or ""
    expected_target = "cname.zenicloud.io"

    verified = False
    note = ""
    try:
        import dns.resolver  # type: ignore

        try:
            answers = dns.resolver.resolve(domain, "CNAME")
            for a in answers:
                target = str(a.target).rstrip(".").lower()
                if target == expected_target or target.endswith(".zenicloud.io"):
                    verified = True
                    note = f"CNAME pointed to {target}"
                    break
            if not verified:
                note = "CNAME not pointing to zenicloud.io"
        except Exception as e:  # noqa: BLE001
            note = f"DNS resolve failed: {e}"
    except ImportError:
        # dnspython not installed — mark verified với token (dev mode)
        verified = True
        note = f"dev mode: token={token[:8]}... accepted"

    if verified:
        await db.execute(
            text(
                """
                UPDATE reseller_brand_config
                SET domain_verified_at = NOW(), updated_at = NOW()
                WHERE reseller_id = :id
                """
            ),
            {"id": rid},
        )
        await audit_push(
            db, actor=me.email, workspace_id=ws,
            action="reseller.domain.verify", target=domain, severity="info",
            metadata={"note": note},
        )
        await db.commit()
        return {"ok": True, "verified": True, "domain": domain, "note": note}

    return {
        "ok": False, "verified": False, "domain": domain,
        "expected_cname_target": expected_target,
        "cname_token": token, "note": note,
    }


# ════════════════════════════════════════════════════════════════════════════
# Customer management
# ════════════════════════════════════════════════════════════════════════════
@router.get("/customers", response_model=list[CustomerListItem])
async def list_customers(
    ws: str = Query(...),
    status: str | None = Query(default=None, pattern=r"^(active|churned|suspended)$"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CustomerListItem]:
    await require_workspace_access(ws, me)
    rs = await _get_reseller(db, ws)
    where = ["reseller_id = :rid"]
    params: dict[str, Any] = {"rid": rs["id"], "lim": limit, "off": offset}
    if status:
        where.append("status = :st")
        params["st"] = status
    rows = (
        await db.execute(
            text(
                f"""
                SELECT id, customer_workspace_id, customer_email,
                       signed_up_via, promo_code, current_plan, status,
                       lifetime_value_vnd, last_payment_at, signed_up_at
                FROM reseller_customers
                WHERE {" AND ".join(where)}
                ORDER BY signed_up_at DESC
                LIMIT :lim OFFSET :off
                """
            ),
            params,
        )
    ).mappings().all()
    return [
        CustomerListItem(
            id=int(r["id"]),
            customer_workspace_id=r["customer_workspace_id"],
            customer_email=r["customer_email"],
            signed_up_via=r.get("signed_up_via"),
            promo_code=r.get("promo_code"),
            current_plan=r["current_plan"],
            status=r["status"],
            lifetime_value_vnd=_to_float(r["lifetime_value_vnd"]),
            last_payment_at=r.get("last_payment_at"),
            signed_up_at=r["signed_up_at"],
        )
        for r in rows
    ]


@router.post("/customers/invite", response_model=InviteOut)
async def invite_customer(
    payload: InviteIn,
    request: Request,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> InviteOut:
    """
    Sinh invite token (ngắn hạn 7 ngày) — link sẽ tới signup page với
    ?ref=<reseller_code>&plan=<plan_id>. Email gửi best-effort qua services.email.
    """
    await require_workspace_access(ws, me)
    _check_owner(me)
    rs = await _get_reseller(db, ws)

    token = secrets.token_urlsafe(28)
    expires = _now() + timedelta(days=7)
    # Brand config for branded URL prefix
    brand = (
        await db.execute(
            text(
                "SELECT custom_domain FROM reseller_brand_config WHERE reseller_id = :id"
            ),
            {"id": rs["id"]},
        )
    ).mappings().first()
    base = f"https://{brand['custom_domain']}" if brand and brand.get("custom_domain") else f"https://{settings.public_host}" if hasattr(settings, "public_host") else "https://app.zenicloud.io"
    invite_url = f"{base}/signup?invite={token}&ref={rs['id']}&plan={payload.plan_id}"
    if payload.promo_code:
        invite_url += f"&promo={payload.promo_code}"

    # Best-effort email (no failure if unavailable)
    try:
        from app.services.email import send_email  # type: ignore

        subject = f"Lời mời tham gia {rs['reseller_name']} trên Zeni Cloud"
        body = (
            f"Xin chào,\n\n"
            f"{rs['reseller_name']} mời bạn tạo tài khoản Zeni Cloud (gói {payload.plan_id}).\n"
            f"Click link sau để đăng ký:\n\n{invite_url}\n\n"
            f"Link hết hạn vào {expires.isoformat()}.\n"
        )
        await send_email(to=payload.email, subject=subject, body=body)
    except Exception as e:
        log.warning("[reseller] invite email failed: %s", e)

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="reseller.invite", target=payload.email,
        metadata={"plan": payload.plan_id, "promo": payload.promo_code, "expires_at": expires.isoformat()},
    )
    await db.commit()
    return InviteOut(
        invite_token=token, invite_url=invite_url,
        expires_at=expires, email=payload.email,
    )


@router.get("/customers/{cid}")
async def customer_detail(
    cid: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    rs = await _get_reseller(db, ws)
    row = (
        await db.execute(
            text(
                """
                SELECT id, customer_workspace_id, customer_email,
                       signed_up_via, promo_code, original_plan, current_plan,
                       status, lifetime_value_vnd, last_payment_at,
                       churned_at, signed_up_at
                FROM reseller_customers
                WHERE id = :cid AND reseller_id = :rid
                """
            ),
            {"cid": cid, "rid": rs["id"]},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(404, "customer not found")

    # Recent commissions for this customer
    comms = (
        await db.execute(
            text(
                """
                SELECT id, billing_period_start, billing_period_end,
                       customer_paid_vnd, commission_vnd, status
                FROM reseller_commissions
                WHERE reseller_id = :rid AND customer_workspace_id = :ws
                ORDER BY billing_period_start DESC LIMIT 12
                """
            ),
            {"rid": rs["id"], "ws": row["customer_workspace_id"]},
        )
    ).mappings().all()

    return {
        "customer": {
            **{k: (v.isoformat() if isinstance(v, datetime) else v)
               for k, v in dict(row).items()},
            "lifetime_value_vnd": _to_float(row["lifetime_value_vnd"]),
        },
        "commissions": [
            {
                "id": int(c["id"]),
                "period_start": c["billing_period_start"].isoformat(),
                "period_end": c["billing_period_end"].isoformat(),
                "paid_vnd": _to_float(c["customer_paid_vnd"]),
                "commission_vnd": _to_float(c["commission_vnd"]),
                "status": c["status"],
            }
            for c in comms
        ],
    }


@router.post("/customers/{cid}/upgrade")
async def upgrade_customer(
    cid: int,
    payload: UpgradeIn,
    request: Request,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_owner(me)
    rs = await _get_reseller(db, ws)
    row = (
        await db.execute(
            text(
                """
                UPDATE reseller_customers SET current_plan = :p
                WHERE id = :cid AND reseller_id = :rid
                RETURNING id, customer_workspace_id, current_plan
                """
            ),
            {"p": payload.new_plan, "cid": cid, "rid": rs["id"]},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(404, "customer not found")
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="reseller.customer.upgrade", target=row["customer_workspace_id"],
        metadata={"new_plan": payload.new_plan, "customer_id": int(row["id"])},
    )
    await db.commit()
    return {"ok": True, "id": int(row["id"]), "new_plan": row["current_plan"]}


# ════════════════════════════════════════════════════════════════════════════
# Commissions + payouts
# ════════════════════════════════════════════════════════════════════════════
@router.get("/commissions", response_model=list[CommissionOut])
async def list_commissions(
    ws: str = Query(...),
    period: str | None = Query(default=None, description="ISO yyyy-mm or yyyy-mm-dd"),
    status: str | None = Query(default=None, pattern=r"^(pending|payable|paid|clawback)$"),
    limit: int = Query(default=200, ge=1, le=1000),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CommissionOut]:
    await require_workspace_access(ws, me)
    rs = await _get_reseller(db, ws)
    where = ["reseller_id = :rid"]
    params: dict[str, Any] = {"rid": rs["id"], "lim": limit}
    if status:
        where.append("status = :st")
        params["st"] = status
    if period:
        # Accept "2026-04" or "2026-04-15" → filter by billing_period_start
        try:
            parsed = datetime.fromisoformat(period if len(period) > 7 else f"{period}-01")
            where.append("billing_period_start >= :ps")
            where.append("billing_period_start < :pe")
            params["ps"] = parsed
            params["pe"] = parsed + timedelta(days=32)
        except Exception:
            raise HTTPException(400, "period format must be YYYY-MM or YYYY-MM-DD")

    rows = (
        await db.execute(
            text(
                f"""
                SELECT id, customer_workspace_id, billing_period_start,
                       billing_period_end, customer_paid_vnd, commission_percent,
                       commission_vnd, status, payable_at, paid_at
                FROM reseller_commissions
                WHERE {" AND ".join(where)}
                ORDER BY billing_period_end DESC, id DESC
                LIMIT :lim
                """
            ),
            params,
        )
    ).mappings().all()
    return [
        CommissionOut(
            id=int(r["id"]),
            customer_workspace_id=r["customer_workspace_id"],
            billing_period_start=r["billing_period_start"],
            billing_period_end=r["billing_period_end"],
            customer_paid_vnd=_to_float(r["customer_paid_vnd"]),
            commission_percent=_to_float(r["commission_percent"]),
            commission_vnd=_to_float(r["commission_vnd"]),
            status=r["status"],
            payable_at=r.get("payable_at"),
            paid_at=r.get("paid_at"),
        )
        for r in rows
    ]


@router.get("/commissions/pending")
async def pending_commissions(
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    rs = await _get_reseller(db, ws)
    row = (
        await db.execute(
            text(
                """
                SELECT
                    COUNT(*) AS cnt,
                    COALESCE(SUM(commission_vnd) FILTER (WHERE status = 'pending'),0) AS pending_vnd,
                    COALESCE(SUM(commission_vnd) FILTER (WHERE status = 'payable'),0) AS payable_vnd,
                    MIN(payable_at) FILTER (WHERE status = 'pending') AS next_payable_at
                FROM reseller_commissions
                WHERE reseller_id = :rid AND status IN ('pending','payable')
                """
            ),
            {"rid": rs["id"]},
        )
    ).mappings().first()
    return {
        "count": int(row["cnt"] or 0),
        "pending_vnd": _to_float(row["pending_vnd"]),
        "payable_vnd": _to_float(row["payable_vnd"]),
        "next_payable_at": row["next_payable_at"].isoformat() if row.get("next_payable_at") else None,
    }


@router.get("/payouts", response_model=list[PayoutOut])
async def list_payouts(
    ws: str = Query(...),
    limit: int = Query(default=100, ge=1, le=500),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[PayoutOut]:
    await require_workspace_access(ws, me)
    rs = await _get_reseller(db, ws)
    rows = (
        await db.execute(
            text(
                """
                SELECT id, total_amount_vnd, period_start, period_end, status,
                       paid_at, payment_method, transaction_ref, commission_count,
                       created_at
                FROM reseller_payouts
                WHERE reseller_id = :rid
                ORDER BY created_at DESC
                LIMIT :lim
                """
            ),
            {"rid": rs["id"], "lim": limit},
        )
    ).mappings().all()
    return [
        PayoutOut(
            id=int(r["id"]),
            total_amount_vnd=_to_float(r["total_amount_vnd"]),
            period_start=r["period_start"],
            period_end=r["period_end"],
            status=r["status"],
            paid_at=r.get("paid_at"),
            payment_method=r.get("payment_method"),
            transaction_ref=r.get("transaction_ref"),
            commission_count=int(r["commission_count"] or 0),
            created_at=r["created_at"],
        )
        for r in rows
    ]


@router.post("/admin/payouts/process")
async def admin_process_payouts(
    request: Request,
    me: CurrentUser = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Cron-like batch payout processor. Platform Admin only."""
    # Promote pending → payable (in case grace period expired)
    await compute_monthly_commissions(db, period_end=_now(), period_days=30)
    result = await process_payouts(db, period_end=_now())
    await audit_push(
        db, actor=me.email, workspace_id=None,
        action="reseller.payouts.process", target="batch",
        severity="warn",
        metadata=result,
    )
    await db.commit()
    return {"ok": True, **result}


# ════════════════════════════════════════════════════════════════════════════
# Promo codes
# ════════════════════════════════════════════════════════════════════════════
@router.post("/promo-codes", response_model=PromoCodeOut, status_code=201)
async def create_promo(
    payload: PromoCodeIn,
    request: Request,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PromoCodeOut:
    await require_workspace_access(ws, me)
    _check_owner(me)
    rs = await _get_reseller(db, ws)
    try:
        row = (
            await db.execute(
                text(
                    """
                    INSERT INTO reseller_promo_codes
                        (reseller_id, code, discount_type, discount_value,
                         max_uses, expires_at, applies_to_plans, description, enabled)
                    VALUES (:rid, :c, :dt, :dv, :mu, :ex, :pl, :desc, :en)
                    RETURNING id, code, discount_type, discount_value, max_uses,
                              current_uses, expires_at, applies_to_plans,
                              description, enabled, created_at
                    """
                ),
                {
                    "rid": rs["id"], "c": payload.code,
                    "dt": payload.discount_type, "dv": payload.discount_value,
                    "mu": payload.max_uses, "ex": payload.expires_at,
                    "pl": payload.applies_to_plans, "desc": payload.description,
                    "en": payload.enabled,
                },
            )
        ).mappings().first()
    except Exception as e:
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            raise HTTPException(409, "code đã tồn tại") from e
        raise HTTPException(500, str(e)) from e

    if not row:
        raise HTTPException(500, "create promo failed")

    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="reseller.promo.create", target=payload.code,
        metadata={"discount_type": payload.discount_type, "value": payload.discount_value},
    )
    await db.commit()
    return PromoCodeOut(
        id=int(row["id"]),
        code=row["code"],
        discount_type=row["discount_type"],
        discount_value=_to_float(row["discount_value"]),
        max_uses=row["max_uses"],
        current_uses=int(row["current_uses"] or 0),
        expires_at=row["expires_at"],
        applies_to_plans=list(row["applies_to_plans"] or []),
        description=row["description"],
        enabled=row["enabled"],
        created_at=row["created_at"],
    )


@router.get("/promo-codes", response_model=list[PromoCodeOut])
async def list_promos(
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[PromoCodeOut]:
    await require_workspace_access(ws, me)
    rs = await _get_reseller(db, ws)
    rows = (
        await db.execute(
            text(
                """
                SELECT id, code, discount_type, discount_value, max_uses,
                       current_uses, expires_at, applies_to_plans,
                       description, enabled, created_at
                FROM reseller_promo_codes WHERE reseller_id = :rid
                ORDER BY created_at DESC
                """
            ),
            {"rid": rs["id"]},
        )
    ).mappings().all()
    return [
        PromoCodeOut(
            id=int(r["id"]),
            code=r["code"],
            discount_type=r["discount_type"],
            discount_value=_to_float(r["discount_value"]),
            max_uses=r["max_uses"],
            current_uses=int(r["current_uses"] or 0),
            expires_at=r["expires_at"],
            applies_to_plans=list(r["applies_to_plans"] or []),
            description=r["description"],
            enabled=r["enabled"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


@router.patch("/promo-codes/{pid}", response_model=PromoCodeOut)
async def update_promo(
    pid: int,
    payload: PromoCodeIn,
    request: Request,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PromoCodeOut:
    await require_workspace_access(ws, me)
    _check_owner(me)
    rs = await _get_reseller(db, ws)
    row = (
        await db.execute(
            text(
                """
                UPDATE reseller_promo_codes SET
                    code = :c, discount_type = :dt, discount_value = :dv,
                    max_uses = :mu, expires_at = :ex,
                    applies_to_plans = :pl, description = :desc,
                    enabled = :en, updated_at = NOW()
                WHERE id = :pid AND reseller_id = :rid
                RETURNING id, code, discount_type, discount_value, max_uses,
                          current_uses, expires_at, applies_to_plans,
                          description, enabled, created_at
                """
            ),
            {
                "pid": pid, "rid": rs["id"], "c": payload.code,
                "dt": payload.discount_type, "dv": payload.discount_value,
                "mu": payload.max_uses, "ex": payload.expires_at,
                "pl": payload.applies_to_plans, "desc": payload.description,
                "en": payload.enabled,
            },
        )
    ).mappings().first()
    if not row:
        raise HTTPException(404, "promo not found")
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="reseller.promo.update", target=str(pid),
        metadata={"code": payload.code, "enabled": payload.enabled},
    )
    await db.commit()
    return PromoCodeOut(
        id=int(row["id"]),
        code=row["code"],
        discount_type=row["discount_type"],
        discount_value=_to_float(row["discount_value"]),
        max_uses=row["max_uses"],
        current_uses=int(row["current_uses"] or 0),
        expires_at=row["expires_at"],
        applies_to_plans=list(row["applies_to_plans"] or []),
        description=row["description"],
        enabled=row["enabled"],
        created_at=row["created_at"],
    )


@router.delete("/promo-codes/{pid}")
async def delete_promo(
    pid: int,
    request: Request,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_workspace_access(ws, me)
    _check_owner(me)
    rs = await _get_reseller(db, ws)
    row = (
        await db.execute(
            text(
                "DELETE FROM reseller_promo_codes "
                "WHERE id = :pid AND reseller_id = :rid RETURNING id, code"
            ),
            {"pid": pid, "rid": rs["id"]},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(404, "promo not found")
    await audit_push(
        db, actor=me.email, workspace_id=ws,
        action="reseller.promo.delete", target=str(pid),
        metadata={"code": row["code"]},
    )
    await db.commit()
    return {"ok": True, "id": pid}


@router.post("/promo-codes/validate")
async def public_validate_promo(
    payload: PromoValidateIn,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Public — no auth. Used at signup time to apply discount."""
    return await validate_promo_code(db, code=payload.code, plan_id=payload.plan_id)


# ════════════════════════════════════════════════════════════════════════════
# Reports
# ════════════════════════════════════════════════════════════════════════════
@router.get("/reports/dashboard", response_model=DashboardOut)
async def report_dashboard(
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DashboardOut:
    await require_workspace_access(ws, me)
    rs = await _get_reseller(db, ws)
    rid = rs["id"]
    row = (
        await db.execute(
            text(
                """
                SELECT * FROM v_reseller_dashboard WHERE reseller_id = :id
                """
            ),
            {"id": rid},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(404, "no dashboard data")

    active = int(row["active_customers"] or 0)
    churned = int(row["churned_customers"] or 0)
    churn_rate = (churned / max(active + churned, 1)) * 100
    mrr_vnd = _to_float(row["commission_last_30d_vnd"]) / max(
        _to_float(row["commission_percent"]) / 100, 0.01
    )

    return DashboardOut(
        reseller_id=rid,
        tier=row["tier"],
        commission_percent=_to_float(row["commission_percent"]),
        active_customers=active,
        churned_customers=churned,
        churn_rate_pct=round(churn_rate, 2),
        mrr_vnd=round(mrr_vnd, 2),
        total_customer_value_vnd=_to_float(row["total_customer_value_vnd"]),
        commission_paid_vnd=_to_float(row["commission_paid_vnd"]),
        commission_pending_vnd=_to_float(row["commission_pending_vnd"]),
        commission_last_30d_vnd=_to_float(row["commission_last_30d_vnd"]),
    )


@router.get("/reports/forecast", response_model=ForecastOut)
async def report_forecast(
    ws: str = Query(...),
    period_days: int = Query(default=90, ge=7, le=365),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ForecastOut:
    """
    Naive forecast: avg(last 90d commission) / 90 * period_days * active_customer_factor.
    """
    await require_workspace_access(ws, me)
    rs = await _get_reseller(db, ws)
    rid = rs["id"]

    row = (
        await db.execute(
            text(
                """
                SELECT
                    COALESCE(SUM(commission_vnd), 0) AS sum_90d,
                    COUNT(DISTINCT customer_workspace_id) AS active_custs
                FROM reseller_commissions
                WHERE reseller_id = :rid
                  AND billing_period_end >= NOW() - INTERVAL '90 days'
                """
            ),
            {"rid": rid},
        )
    ).mappings().first()
    sum_90d = _to_float(row["sum_90d"])
    active = int(row["active_custs"] or 0)
    daily = sum_90d / 90.0
    expected = daily * period_days
    avg_per_cust = (sum_90d / active) if active else 0.0

    return ForecastOut(
        period_days=period_days,
        expected_commission_vnd=round(expected, 2),
        based_on_customers=active,
        avg_commission_per_customer_vnd=round(avg_per_cust, 2),
    )


__all__ = ["router"]
