"""
Zeni Cloud Core — Platform Admin API.

Endpoints super-admin cho nền tảng Zeni Cloud (caotuanphat581@gmail.com,
ceo@zeni-holdings.vn, hoặc bất kỳ user nào có role='PlatformAdmin').

Phân biệt với /admin-access (CAAA — Customer-Authorized Admin Access):
    * /admin-access/*       : flow xin phép khách để vào workspace cụ thể
    * /admin/platform/*     : aggregate stats + governance toàn platform

NGUYÊN TẮC AN TOÀN:
    1. STRICT gate trên MỌI endpoint (require_platform_admin).
    2. KHÔNG bao giờ trả raw data của customer — chỉ aggregate / summary.
    3. Mọi sensitive action ghi vào platform_admin_actions (audit trail riêng).
    4. Impersonate phải đi qua CAAA flow (Sprint A3) — không direct bypass.

Endpoints (prefix /admin/platform, tag admin-platform):

  GET  /dashboard
  GET  /customers
  GET  /customers/{ws_id}/summary
  POST /customers/{ws_id}/impersonate

  GET  /revenue
  GET  /cost
  GET  /system/health

  GET  /alerts
  POST /alerts/{id}/resolve

  GET  /announcements
  POST /announcements

  GET  /feature-flags
  PATCH /feature-flags/{key}

  GET  /support-tickets
  POST /support-tickets/{id}/assign
  POST /support-tickets/{id}/resolve

  GET  /admin-actions-log

Schema match: backend/migrations/037_admin_platform.sql
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_role import require_platform_admin
from app.core.config import settings  # noqa: F401  (kept for future env switching)
from app.core.deps import CurrentUser
from app.db.base import get_db

log = logging.getLogger("zeni.api.admin_platform")

router = APIRouter(prefix="/admin/platform", tags=["admin-platform"])


# ════════════════════════════════════════════════════════════════════════════
# Pydantic schemas
# ════════════════════════════════════════════════════════════════════════════
class DashboardStats(BaseModel):
    total_customers: int
    active_workspaces: int
    mau: int                    # Monthly Active Users (last 30 days)
    signups_today: int
    signups_7d: int
    signups_30d: int
    mrr_usd: float              # Monthly Recurring Revenue (last 30 days spend)
    arr_usd: float              # Annualized
    total_revenue_usd: float    # Lifetime
    total_revenue_vnd: float    # ~ usd * 25_000
    churn_rate_pct: float       # 30d churn estimate
    open_alerts: int
    open_tickets: int


class CustomerListItem(BaseModel):
    workspace_id: str
    workspace_code: str | None
    workspace_name: str
    owner_email: str | None
    member_count: int
    spend_usd_30d: float
    spend_usd_lifetime: float
    last_active_at: datetime | None
    created_at: datetime | None


class CustomerSummary(BaseModel):
    workspace_id: str
    workspace_code: str | None
    workspace_name: str
    owner_email: str | None
    member_count: int
    tier: str                  # 'free','starter','pro','enterprise' (best-effort)
    mrr_usd: float
    spend_usd_30d: float
    spend_usd_lifetime: float
    last_active_at: datetime | None
    created_at: datetime | None
    # NOTE: cố tình KHÔNG expose project list, db tables, file count chi tiết.
    project_count: int
    db_count: int


class ImpersonateRequest(BaseModel):
    reason: str = Field(..., min_length=4, max_length=2000)
    duration_hours: int = Field(default=6, ge=6, le=24)
    ticket_url: str | None = Field(default=None, max_length=500)


class ImpersonateResponse(BaseModel):
    request_id: int
    status: str
    customer_email: str | None
    expires_in_hours: int
    note: str


class RevenuePoint(BaseModel):
    day: str                   # ISO date YYYY-MM-DD
    revenue_usd: float
    new_signups: int


class RevenueResponse(BaseModel):
    from_: str = Field(..., alias="from")
    to: str
    mrr_usd: float
    arr_usd: float
    mom_change_pct: float
    churn_rate_pct: float
    series: list[RevenuePoint]

    model_config = {"populate_by_name": True}


class CostBreakdownItem(BaseModel):
    layer: str                 # 'L1','L2','L3','L4','L5','L6'
    label: str
    cost_usd: float
    pct: float


class CostResponse(BaseModel):
    from_: str = Field(..., alias="from")
    to: str
    total_usd: float
    breakdown: list[CostBreakdownItem]

    model_config = {"populate_by_name": True}


class HealthService(BaseModel):
    name: str
    kind: str                  # 'cloud_run','cloud_sql','memorystore','gcs'
    status: str                # 'ok','degraded','down','unknown'
    latency_ms: float | None
    region: str | None
    note: str | None = None


class HealthResponse(BaseModel):
    overall: str
    services: list[HealthService]
    checked_at: datetime


class AlertOut(BaseModel):
    id: int
    alert_type: str
    severity: str
    message: str
    source: str | None
    details: dict[str, Any]
    occurred_at: datetime
    resolved_at: datetime | None
    resolved_by: str | None


class AnnouncementIn(BaseModel):
    title: str = Field(..., min_length=2, max_length=255)
    content: str = Field(..., min_length=2)
    target_role: str = Field(default="all", max_length=64)
    scheduled_at: datetime | None = None
    expires_at: datetime | None = None
    is_pinned: bool = False
    severity: str = Field(default="info", pattern=r"^(info|warn|critical)$")


class AnnouncementOut(BaseModel):
    id: int
    title: str
    content: str
    target_role: str
    scheduled_at: datetime
    expires_at: datetime | None
    is_pinned: bool
    severity: str
    created_by: str | None
    created_at: datetime
    updated_at: datetime


class FeatureFlagOut(BaseModel):
    key: str
    value: Any
    description: str | None
    environment: str
    updated_at: datetime
    updated_by: str | None


class FeatureFlagPatch(BaseModel):
    value: Any
    description: str | None = None


class SupportTicketOut(BaseModel):
    id: int
    customer_workspace_id: str | None
    customer_email: str
    subject: str
    description: str
    status: str
    priority: str
    assigned_admin: str | None
    source: str | None
    tags: list[str]
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None


class AdminActionOut(BaseModel):
    id: int
    admin_email: str
    action_type: str
    target_type: str | None
    target_id: str | None
    details: dict[str, Any]
    ip_address: str | None
    user_agent: str | None
    occurred_at: datetime


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════
USD_TO_VND = 25_000.0  # Best-effort fixed; thay bằng FX feed sau.


def _json(obj: Any) -> str:
    """JSON serialize for ::jsonb cast (handles datetime → iso)."""
    def _default(o: Any) -> Any:
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, Decimal):
            return float(o)
        return str(o)
    return json.dumps(obj, default=_default, ensure_ascii=False)


async def _log_admin_action(
    db: AsyncSession,
    *,
    admin_email: str,
    action_type: str,
    target_type: str | None = None,
    target_id: str | None = None,
    details: dict[str, Any] | None = None,
    request: Request | None = None,
) -> None:
    """Audit trail riêng cho Platform Admin — KHÔNG dùng audit_logs chung."""
    ip = None
    ua = None
    if request is not None:
        ip = (request.client.host if request.client else None)
        ua = request.headers.get("user-agent")
    try:
        await db.execute(
            text(
                """
                INSERT INTO platform_admin_actions
                    (admin_email, action_type, target_type, target_id, details, ip_address, user_agent)
                VALUES (:e, :a, :tt, :ti, :d::jsonb, :ip, :ua)
                """
            ),
            {
                "e": admin_email,
                "a": action_type,
                "tt": target_type,
                "ti": target_id,
                "d": _json(details or {}),
                "ip": ip,
                "ua": ua,
            },
        )
    except Exception as e:
        # Không break flow nếu audit fail; chỉ log warn.
        log.warning("[admin_platform] log_admin_action failed: %s", e)


def _to_float(x: Any) -> float:
    if x is None:
        return 0.0
    if isinstance(x, Decimal):
        return float(x)
    try:
        return float(x)
    except Exception:
        return 0.0


def _detect_tier(spend_30d_usd: float) -> str:
    """Best-effort tier detection cho đến khi có pricing_subscriptions table."""
    if spend_30d_usd <= 0:
        return "free"
    if spend_30d_usd < 50:
        return "starter"
    if spend_30d_usd < 500:
        return "pro"
    return "enterprise"


# ════════════════════════════════════════════════════════════════════════════
# GET /dashboard
# ════════════════════════════════════════════════════════════════════════════
@router.get("/dashboard", response_model=DashboardStats)
async def dashboard(
    request: Request,
    me: CurrentUser = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> DashboardStats:
    """Aggregate stats — KHÔNG raw data."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    last_7d = now - timedelta(days=7)
    last_30d = now - timedelta(days=30)

    # Customers / workspaces
    total_customers = (
        await db.execute(text("SELECT COUNT(*) FROM workspaces"))
    ).scalar_one()
    active_workspaces = (
        await db.execute(
            text(
                """
                SELECT COUNT(DISTINCT workspace_id) FROM billing_events
                WHERE ts >= :since
                """
            ),
            {"since": last_30d},
        )
    ).scalar_one()

    # MAU = unique users with refresh tokens / login in 30d (proxy)
    try:
        mau = (
            await db.execute(
                text(
                    """
                    SELECT COUNT(DISTINCT u.id) FROM users u
                    WHERE COALESCE(u.last_login, u.created_at) >= :since
                    """
                ),
                {"since": last_30d},
            )
        ).scalar_one()
    except Exception:
        mau = 0

    # Signups
    signups_today = (
        await db.execute(
            text("SELECT COUNT(*) FROM users WHERE created_at >= :s"),
            {"s": today_start},
        )
    ).scalar_one()
    signups_7d = (
        await db.execute(
            text("SELECT COUNT(*) FROM users WHERE created_at >= :s"),
            {"s": last_7d},
        )
    ).scalar_one()
    signups_30d = (
        await db.execute(
            text("SELECT COUNT(*) FROM users WHERE created_at >= :s"),
            {"s": last_30d},
        )
    ).scalar_one()

    # Revenue (sum cost_usd ~ what customers paid; for MVP treat cost as revenue)
    mrr_usd = _to_float(
        (
            await db.execute(
                text(
                    "SELECT COALESCE(SUM(cost_usd),0) FROM billing_events WHERE ts >= :s"
                ),
                {"s": last_30d},
            )
        ).scalar_one()
    )
    total_revenue_usd = _to_float(
        (
            await db.execute(
                text("SELECT COALESCE(SUM(cost_usd),0) FROM billing_events")
            )
        ).scalar_one()
    )

    # Churn = workspaces có spend trước 60d nhưng KHÔNG spend trong 30d gần đây
    try:
        churned = _to_float(
            (
                await db.execute(
                    text(
                        """
                        WITH old_active AS (
                          SELECT DISTINCT workspace_id FROM billing_events
                          WHERE ts >= :start AND ts < :mid
                        ),
                        new_active AS (
                          SELECT DISTINCT workspace_id FROM billing_events
                          WHERE ts >= :mid
                        )
                        SELECT COUNT(*)::float / NULLIF((SELECT COUNT(*) FROM old_active),0) * 100
                        FROM old_active oa
                        WHERE oa.workspace_id NOT IN (SELECT workspace_id FROM new_active)
                        """
                    ),
                    {"start": now - timedelta(days=60), "mid": last_30d},
                )
            ).scalar_one()
        )
    except Exception:
        churned = 0.0

    # Alerts / tickets
    try:
        open_alerts = (
            await db.execute(
                text(
                    "SELECT COUNT(*) FROM platform_alerts WHERE resolved_at IS NULL"
                )
            )
        ).scalar_one()
    except Exception:
        open_alerts = 0
    try:
        open_tickets = (
            await db.execute(
                text(
                    "SELECT COUNT(*) FROM platform_support_tickets WHERE status IN ('open','pending')"
                )
            )
        ).scalar_one()
    except Exception:
        open_tickets = 0

    await _log_admin_action(
        db, admin_email=me.email, action_type="view_dashboard", request=request
    )
    await db.commit()

    return DashboardStats(
        total_customers=int(total_customers or 0),
        active_workspaces=int(active_workspaces or 0),
        mau=int(mau or 0),
        signups_today=int(signups_today or 0),
        signups_7d=int(signups_7d or 0),
        signups_30d=int(signups_30d or 0),
        mrr_usd=round(mrr_usd, 4),
        arr_usd=round(mrr_usd * 12, 4),
        total_revenue_usd=round(total_revenue_usd, 4),
        total_revenue_vnd=round(total_revenue_usd * USD_TO_VND, 2),
        churn_rate_pct=round(churned, 2),
        open_alerts=int(open_alerts or 0),
        open_tickets=int(open_tickets or 0),
    )


# ════════════════════════════════════════════════════════════════════════════
# GET /customers
# ════════════════════════════════════════════════════════════════════════════
@router.get("/customers", response_model=list[CustomerListItem])
async def list_customers(
    request: Request,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    plan: str | None = None,
    status_: str | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    me: CurrentUser = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> list[CustomerListItem]:
    """List workspaces (NOT detail data). Aggregate-only."""
    where: list[str] = []
    params: dict[str, Any] = {"lim": limit, "off": offset}
    if from_:
        where.append("v.workspace_created_at >= :from")
        params["from"] = from_
    if to:
        where.append("v.workspace_created_at <= :to")
        params["to"] = to
    if search:
        where.append(
            "(v.workspace_name ILIKE :q OR v.workspace_id ILIKE :q OR v.owner_email ILIKE :q)"
        )
        params["q"] = f"%{search}%"
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    rows = (
        await db.execute(
            text(
                f"""
                SELECT v.workspace_id, v.workspace_code, v.workspace_name,
                       v.owner_email, v.member_count,
                       v.spend_usd_30d, v.spend_usd_lifetime,
                       v.last_billing_event_at, v.workspace_created_at
                FROM v_platform_customer_summary v
                {where_sql}
                ORDER BY v.spend_usd_30d DESC NULLS LAST,
                         v.workspace_created_at DESC NULLS LAST
                LIMIT :lim OFFSET :off
                """
            ),
            params,
        )
    ).mappings().all()

    items = [
        CustomerListItem(
            workspace_id=r["workspace_id"],
            workspace_code=r.get("workspace_code"),
            workspace_name=r["workspace_name"],
            owner_email=r.get("owner_email"),
            member_count=int(r.get("member_count") or 0),
            spend_usd_30d=_to_float(r.get("spend_usd_30d")),
            spend_usd_lifetime=_to_float(r.get("spend_usd_lifetime")),
            last_active_at=r.get("last_billing_event_at"),
            created_at=r.get("workspace_created_at"),
        )
        for r in rows
    ]

    # Plan filter (best-effort post-filter vì tier là computed)
    if plan:
        items = [x for x in items if _detect_tier(x.spend_usd_30d) == plan.lower()]

    await _log_admin_action(
        db,
        admin_email=me.email,
        action_type="list_customers",
        details={"count": len(items), "filters": {"from": from_, "to": to, "plan": plan, "search": search}},
        request=request,
    )
    await db.commit()
    return items


# ════════════════════════════════════════════════════════════════════════════
# GET /customers/{ws_id}/summary
# ════════════════════════════════════════════════════════════════════════════
@router.get("/customers/{ws_id}/summary", response_model=CustomerSummary)
async def customer_summary(
    ws_id: str,
    request: Request,
    me: CurrentUser = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> CustomerSummary:
    """High-level summary — owner email, MRR, last_active, total_spend. NOT raw data."""
    row = (
        await db.execute(
            text(
                """
                SELECT v.* FROM v_platform_customer_summary v
                WHERE v.workspace_id = :ws
                """
            ),
            {"ws": ws_id},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="workspace not found")

    # Project / DB counts (chỉ count, không trả tên/details)
    proj_count = 0
    db_count = 0
    try:
        proj_count = (
            await db.execute(
                text("SELECT COUNT(*) FROM projects WHERE workspace_id = :ws"),
                {"ws": ws_id},
            )
        ).scalar_one()
    except Exception:
        pass
    try:
        db_count = (
            await db.execute(
                text("SELECT COUNT(*) FROM databases WHERE workspace_id = :ws"),
                {"ws": ws_id},
            )
        ).scalar_one()
    except Exception:
        pass

    spend_30d = _to_float(row.get("spend_usd_30d"))
    out = CustomerSummary(
        workspace_id=row["workspace_id"],
        workspace_code=row.get("workspace_code"),
        workspace_name=row["workspace_name"],
        owner_email=row.get("owner_email"),
        member_count=int(row.get("member_count") or 0),
        tier=_detect_tier(spend_30d),
        mrr_usd=round(spend_30d, 4),
        spend_usd_30d=round(spend_30d, 4),
        spend_usd_lifetime=round(_to_float(row.get("spend_usd_lifetime")), 4),
        last_active_at=row.get("last_billing_event_at"),
        created_at=row.get("workspace_created_at"),
        project_count=int(proj_count or 0),
        db_count=int(db_count or 0),
    )

    await _log_admin_action(
        db,
        admin_email=me.email,
        action_type="view_customer_summary",
        target_type="workspace",
        target_id=ws_id,
        request=request,
    )
    await db.commit()
    return out


# ════════════════════════════════════════════════════════════════════════════
# POST /customers/{ws_id}/impersonate
# ════════════════════════════════════════════════════════════════════════════
@router.post("/customers/{ws_id}/impersonate", response_model=ImpersonateResponse)
async def impersonate_customer(
    ws_id: str,
    payload: ImpersonateRequest,
    request: Request,
    me: CurrentUser = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> ImpersonateResponse:
    """
    Tạo CAAA request (Sprint A3) cho support — KHÔNG bypass approval của khách.

    Flow:
      1. Validate workspace tồn tại.
      2. Lookup Owner email của workspace.
      3. Insert admin_access_requests (status=pending) tương tự /admin-access/request.
      4. Email gửi tới Owner với approve/deny link (best-effort).
      5. Trả request_id để admin theo dõi.
    """
    ws = (
        await db.execute(
            text("SELECT id, name FROM workspaces WHERE id = :id"),
            {"id": ws_id},
        )
    ).mappings().first()
    if not ws:
        raise HTTPException(status_code=404, detail="workspace not found")

    # Resolve owner email
    owner_email = (
        await db.execute(
            text(
                """
                SELECT u.email FROM users u
                JOIN user_workspaces uw ON uw.user_id = u.id
                WHERE uw.workspace_id = :ws AND uw.role = 'Owner'
                ORDER BY u.created_at ASC NULLS LAST LIMIT 1
                """
            ),
            {"ws": ws_id},
        )
    ).scalar_one_or_none()

    duration_seconds = max(21600, min(payload.duration_hours * 3600, 86400))

    try:
        row = (
            await db.execute(
                text(
                    """
                    INSERT INTO admin_access_requests
                        (admin_user_id, customer_workspace_id, scope, reason,
                         reason_detail, duration_seconds)
                    VALUES (:uid, :ws, :scope, 'customer_support', :detail, :dur)
                    RETURNING id, status
                    """
                ),
                {
                    "uid": str(me.id),
                    "ws": ws_id,
                    "scope": f"{ws_id}.support_session",
                    "detail": payload.reason,
                    "dur": duration_seconds,
                },
            )
        ).mappings().first()
    except Exception as e:
        log.exception("[admin_platform] impersonate insert failed")
        raise HTTPException(
            status_code=500, detail=f"impersonate request failed: {e}"
        ) from e

    if not row:
        raise HTTPException(status_code=500, detail="impersonate insert returned nothing")

    request_id = int(row["id"])

    await _log_admin_action(
        db,
        admin_email=me.email,
        action_type="impersonate.request",
        target_type="workspace",
        target_id=ws_id,
        details={
            "request_id": request_id,
            "duration_hours": payload.duration_hours,
            "ticket_url": payload.ticket_url,
            "reason": payload.reason[:200],
        },
        request=request,
    )
    await db.commit()

    return ImpersonateResponse(
        request_id=request_id,
        status=row["status"],
        customer_email=owner_email,
        expires_in_hours=payload.duration_hours,
        note=(
            "CAAA request đã được tạo. Khách phải approve qua email/portal trước khi "
            "bạn có quyền truy cập. Theo dõi trạng thái tại /admin-access/{id}."
        ),
    )


# ════════════════════════════════════════════════════════════════════════════
# GET /revenue
# ════════════════════════════════════════════════════════════════════════════
@router.get("/revenue", response_model=RevenueResponse)
async def revenue(
    request: Request,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    me: CurrentUser = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> RevenueResponse:
    """MRR chart 30d (default), ARR, MoM change, churn rate."""
    now = datetime.now(timezone.utc)
    end = datetime.fromisoformat(to) if to else now
    start = (
        datetime.fromisoformat(from_)
        if from_
        else (end - timedelta(days=30))
    )

    # Daily revenue series
    rows = (
        await db.execute(
            text(
                """
                SELECT DATE(ts) AS d,
                       COALESCE(SUM(cost_usd),0) AS rev
                FROM billing_events
                WHERE ts >= :s AND ts <= :e
                GROUP BY DATE(ts)
                ORDER BY d ASC
                """
            ),
            {"s": start, "e": end},
        )
    ).mappings().all()
    series_map = {str(r["d"]): _to_float(r["rev"]) for r in rows}

    # Daily signups overlay
    signup_rows = (
        await db.execute(
            text(
                """
                SELECT DATE(created_at) AS d, COUNT(*) AS c
                FROM users
                WHERE created_at >= :s AND created_at <= :e
                GROUP BY DATE(created_at)
                """
            ),
            {"s": start, "e": end},
        )
    ).mappings().all()
    signup_map = {str(r["d"]): int(r["c"]) for r in signup_rows}

    # Build full daily series (gap-fill 0)
    series: list[RevenuePoint] = []
    cursor = start
    while cursor.date() <= end.date():
        key = cursor.date().isoformat()
        series.append(
            RevenuePoint(
                day=key,
                revenue_usd=series_map.get(key, 0.0),
                new_signups=signup_map.get(key, 0),
            )
        )
        cursor += timedelta(days=1)

    mrr = sum(p.revenue_usd for p in series)

    # MoM: same length window before `start`
    prev_start = start - (end - start)
    prev_total = _to_float(
        (
            await db.execute(
                text(
                    "SELECT COALESCE(SUM(cost_usd),0) FROM billing_events "
                    "WHERE ts >= :s AND ts < :e"
                ),
                {"s": prev_start, "e": start},
            )
        ).scalar_one()
    )
    mom = ((mrr - prev_total) / prev_total * 100.0) if prev_total > 0 else 0.0

    # Churn
    try:
        churn = _to_float(
            (
                await db.execute(
                    text(
                        """
                        WITH old_active AS (
                          SELECT DISTINCT workspace_id FROM billing_events
                          WHERE ts >= :pstart AND ts < :start
                        ),
                        new_active AS (
                          SELECT DISTINCT workspace_id FROM billing_events
                          WHERE ts >= :start
                        )
                        SELECT COUNT(*)::float
                               / NULLIF((SELECT COUNT(*) FROM old_active),0) * 100
                        FROM old_active oa
                        WHERE oa.workspace_id NOT IN (SELECT workspace_id FROM new_active)
                        """
                    ),
                    {"pstart": prev_start, "start": start},
                )
            ).scalar_one()
        )
    except Exception:
        churn = 0.0

    await _log_admin_action(
        db, admin_email=me.email, action_type="view_revenue", request=request
    )
    await db.commit()

    return RevenueResponse.model_validate(
        {
            "from": start.date().isoformat(),
            "to": end.date().isoformat(),
            "mrr_usd": round(mrr, 4),
            "arr_usd": round(mrr * 12, 4),
            "mom_change_pct": round(mom, 2),
            "churn_rate_pct": round(churn, 2),
            "series": [p.model_dump() for p in series],
        }
    )


# ════════════════════════════════════════════════════════════════════════════
# GET /cost
# ════════════════════════════════════════════════════════════════════════════
@router.get("/cost", response_model=CostResponse)
async def cost(
    request: Request,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    me: CurrentUser = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> CostResponse:
    """GCP-style cost breakdown by Zeni layer (L1-L6)."""
    now = datetime.now(timezone.utc)
    end = datetime.fromisoformat(to) if to else now
    start = (
        datetime.fromisoformat(from_)
        if from_
        else (end - timedelta(days=30))
    )

    rows = (
        await db.execute(
            text(
                """
                SELECT layer, COALESCE(SUM(cost_usd),0) AS total
                FROM billing_events
                WHERE ts >= :s AND ts <= :e
                GROUP BY layer
                """
            ),
            {"s": start, "e": end},
        )
    ).mappings().all()

    by_layer = {r["layer"]: _to_float(r["total"]) for r in rows}
    total = sum(by_layer.values())

    layer_labels = {
        "L1": "Compute (Cloud Run)",
        "L2": "Data (Cloud SQL + Vector)",
        "L3": "AI (Router + LLMs)",
        "L4": "Automation (Crons + Queue)",
        "L5": "Identity & Security",
        "L6": "Web3 / Token Layer",
    }
    breakdown: list[CostBreakdownItem] = []
    for layer, label in layer_labels.items():
        amt = by_layer.get(layer, 0.0)
        pct = (amt / total * 100.0) if total > 0 else 0.0
        breakdown.append(
            CostBreakdownItem(
                layer=layer,
                label=label,
                cost_usd=round(amt, 4),
                pct=round(pct, 2),
            )
        )

    await _log_admin_action(
        db, admin_email=me.email, action_type="view_cost", request=request
    )
    await db.commit()

    return CostResponse.model_validate(
        {
            "from": start.date().isoformat(),
            "to": end.date().isoformat(),
            "total_usd": round(total, 4),
            "breakdown": [b.model_dump() for b in breakdown],
        }
    )


# ════════════════════════════════════════════════════════════════════════════
# GET /system/health
# ════════════════════════════════════════════════════════════════════════════
@router.get("/system/health", response_model=HealthResponse)
async def system_health(
    request: Request,
    me: CurrentUser = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> HealthResponse:
    """Health check cho services chính: Cloud SQL, Memorystore (proxy), Cloud Run."""
    services: list[HealthService] = []

    # Cloud SQL — measure SELECT 1 latency
    db_status = "ok"
    db_latency = None
    try:
        t0 = datetime.now(timezone.utc)
        await db.execute(text("SELECT 1"))
        db_latency = (datetime.now(timezone.utc) - t0).total_seconds() * 1000
        if db_latency > 500:
            db_status = "degraded"
    except Exception as e:
        db_status = "down"
        log.warning("[admin_platform] DB health failed: %s", e)
    services.append(
        HealthService(
            name="zeni-pg-prod",
            kind="cloud_sql",
            status=db_status,
            latency_ms=round(db_latency, 2) if db_latency else None,
            region=settings.gcp_region,
            note=None,
        )
    )

    # Cloud Run API — proxy via DB recent request count (best-effort)
    try:
        recent_requests = (
            await db.execute(
                text(
                    "SELECT COUNT(*) FROM audit_logs WHERE ts >= NOW() - INTERVAL '5 minutes'"
                )
            )
        ).scalar_one()
        api_status = "ok" if recent_requests is not None else "unknown"
    except Exception:
        api_status = "unknown"
        recent_requests = None
    services.append(
        HealthService(
            name="zeni-cloud-api",
            kind="cloud_run",
            status=api_status,
            latency_ms=None,
            region=settings.gcp_region,
            note=(
                f"~{recent_requests} audit events / 5min"
                if recent_requests is not None
                else "no audit signal"
            ),
        )
    )

    # Memorystore (Redis) — chỉ xác định nếu có config
    redis_url = getattr(settings, "redis_url", "") or ""
    redis_status = "ok" if redis_url else "unknown"
    services.append(
        HealthService(
            name="zeni-redis-prod",
            kind="memorystore",
            status=redis_status,
            latency_ms=None,
            region=settings.gcp_region,
            note=("configured" if redis_url else "in-memory fallback"),
        )
    )

    # GCS bucket prefix probe — chỉ ở mức config
    services.append(
        HealthService(
            name=f"{settings.gcs_bucket_prefix}*",
            kind="gcs",
            status="ok",
            latency_ms=None,
            region=settings.gcp_region,
            note="configured",
        )
    )

    overall = "ok"
    if any(s.status == "down" for s in services):
        overall = "down"
    elif any(s.status == "degraded" for s in services):
        overall = "degraded"

    await _log_admin_action(
        db, admin_email=me.email, action_type="view_system_health", request=request
    )
    await db.commit()

    return HealthResponse(
        overall=overall,
        services=services,
        checked_at=datetime.now(timezone.utc),
    )


# ════════════════════════════════════════════════════════════════════════════
# GET /alerts ; POST /alerts/{id}/resolve
# ════════════════════════════════════════════════════════════════════════════
@router.get("/alerts", response_model=list[AlertOut])
async def list_alerts(
    request: Request,
    status_: str = Query(default="open", alias="status", pattern=r"^(open|resolved|all)$"),
    severity: str | None = Query(default=None, pattern=r"^(info|warn|error|critical)$"),
    limit: int = Query(default=200, ge=1, le=1000),
    me: CurrentUser = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> list[AlertOut]:
    where: list[str] = []
    params: dict[str, Any] = {"lim": limit}
    if status_ == "open":
        where.append("resolved_at IS NULL")
    elif status_ == "resolved":
        where.append("resolved_at IS NOT NULL")
    if severity:
        where.append("severity = :sev")
        params["sev"] = severity
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    rows = (
        await db.execute(
            text(
                f"""
                SELECT id, alert_type, severity, message, source, details,
                       occurred_at, resolved_at, resolved_by
                FROM platform_alerts
                {where_sql}
                ORDER BY occurred_at DESC
                LIMIT :lim
                """
            ),
            params,
        )
    ).mappings().all()

    return [
        AlertOut(
            id=int(r["id"]),
            alert_type=r["alert_type"],
            severity=r["severity"],
            message=r["message"],
            source=r.get("source"),
            details=r.get("details") or {},
            occurred_at=r["occurred_at"],
            resolved_at=r.get("resolved_at"),
            resolved_by=r.get("resolved_by"),
        )
        for r in rows
    ]


@router.post("/alerts/{alert_id}/resolve")
async def resolve_alert(
    alert_id: int,
    request: Request,
    me: CurrentUser = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    row = (
        await db.execute(
            text(
                """
                UPDATE platform_alerts
                SET resolved_at = NOW(), resolved_by = :em
                WHERE id = :id AND resolved_at IS NULL
                RETURNING id, resolved_at
                """
            ),
            {"id": alert_id, "em": me.email},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="alert not found or already resolved")

    await _log_admin_action(
        db,
        admin_email=me.email,
        action_type="alert.resolve",
        target_type="alert",
        target_id=str(alert_id),
        request=request,
    )
    await db.commit()
    return {"ok": True, "id": alert_id, "resolved_at": row["resolved_at"]}


# ════════════════════════════════════════════════════════════════════════════
# GET /announcements ; POST /announcements
# ════════════════════════════════════════════════════════════════════════════
@router.get("/announcements", response_model=list[AnnouncementOut])
async def list_announcements(
    me: CurrentUser = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[AnnouncementOut]:
    rows = (
        await db.execute(
            text(
                """
                SELECT id, title, content, target_role, scheduled_at, expires_at,
                       is_pinned, severity, created_by, created_at, updated_at
                FROM platform_announcements
                ORDER BY is_pinned DESC, scheduled_at DESC
                LIMIT :lim
                """
            ),
            {"lim": limit},
        )
    ).mappings().all()
    return [AnnouncementOut(**dict(r)) for r in rows]


@router.post("/announcements", response_model=AnnouncementOut, status_code=201)
async def create_announcement(
    payload: AnnouncementIn,
    request: Request,
    me: CurrentUser = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> AnnouncementOut:
    scheduled_at = payload.scheduled_at or datetime.now(timezone.utc)
    row = (
        await db.execute(
            text(
                """
                INSERT INTO platform_announcements
                    (title, content, target_role, scheduled_at, expires_at,
                     is_pinned, severity, created_by)
                VALUES (:t, :c, :r, :sa, :ea, :p, :sev, :cb)
                RETURNING id, title, content, target_role, scheduled_at, expires_at,
                          is_pinned, severity, created_by, created_at, updated_at
                """
            ),
            {
                "t": payload.title,
                "c": payload.content,
                "r": payload.target_role,
                "sa": scheduled_at,
                "ea": payload.expires_at,
                "p": payload.is_pinned,
                "sev": payload.severity,
                "cb": me.email,
            },
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=500, detail="insert returned nothing")

    await _log_admin_action(
        db,
        admin_email=me.email,
        action_type="announcement.create",
        target_type="announcement",
        target_id=str(row["id"]),
        details={"title": payload.title, "target_role": payload.target_role},
        request=request,
    )
    await db.commit()
    return AnnouncementOut(**dict(row))


# ════════════════════════════════════════════════════════════════════════════
# GET /feature-flags ; PATCH /feature-flags/{key}
# ════════════════════════════════════════════════════════════════════════════
@router.get("/feature-flags", response_model=list[FeatureFlagOut])
async def list_feature_flags(
    environment: str = Query(default="prod", pattern=r"^(dev|staging|prod)$"),
    me: CurrentUser = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> list[FeatureFlagOut]:
    rows = (
        await db.execute(
            text(
                """
                SELECT key, value, description, environment, updated_at, updated_by
                FROM platform_feature_flags
                WHERE environment = :env
                ORDER BY key ASC
                """
            ),
            {"env": environment},
        )
    ).mappings().all()
    return [FeatureFlagOut(**dict(r)) for r in rows]


@router.patch("/feature-flags/{key}", response_model=FeatureFlagOut)
async def update_feature_flag(
    key: str,
    payload: FeatureFlagPatch,
    request: Request,
    environment: str = Query(default="prod", pattern=r"^(dev|staging|prod)$"),
    me: CurrentUser = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> FeatureFlagOut:
    """Upsert feature flag — value là JSONB (bool/string/object đều OK)."""
    row = (
        await db.execute(
            text(
                """
                INSERT INTO platform_feature_flags
                    (key, value, description, environment, updated_by)
                VALUES (:k, :v::jsonb, :d, :env, :u)
                ON CONFLICT (key, environment) DO UPDATE
                SET value = EXCLUDED.value,
                    description = COALESCE(EXCLUDED.description, platform_feature_flags.description),
                    updated_at = NOW(),
                    updated_by = EXCLUDED.updated_by
                RETURNING key, value, description, environment, updated_at, updated_by
                """
            ),
            {
                "k": key,
                "v": _json(payload.value),
                "d": payload.description,
                "env": environment,
                "u": me.email,
            },
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=500, detail="upsert returned nothing")

    await _log_admin_action(
        db,
        admin_email=me.email,
        action_type="feature_flag.update",
        target_type="feature_flag",
        target_id=key,
        details={"environment": environment, "value": payload.value},
        request=request,
    )
    await db.commit()
    return FeatureFlagOut(**dict(row))


# ════════════════════════════════════════════════════════════════════════════
# Support Tickets
# ════════════════════════════════════════════════════════════════════════════
@router.get("/support-tickets", response_model=list[SupportTicketOut])
async def list_support_tickets(
    status_: str | None = Query(default=None, alias="status", pattern=r"^(open|pending|resolved|closed)$"),
    priority: str | None = Query(default=None, pattern=r"^(low|normal|high|urgent)$"),
    assigned: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    me: CurrentUser = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> list[SupportTicketOut]:
    where: list[str] = []
    params: dict[str, Any] = {"lim": limit}
    if status_:
        where.append("status = :st")
        params["st"] = status_
    if priority:
        where.append("priority = :pr")
        params["pr"] = priority
    if assigned:
        where.append("assigned_admin = :ad")
        params["ad"] = assigned
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    rows = (
        await db.execute(
            text(
                f"""
                SELECT id, customer_workspace_id, customer_email, subject, description,
                       status, priority, assigned_admin, source, tags,
                       created_at, updated_at, resolved_at
                FROM platform_support_tickets
                {where_sql}
                ORDER BY
                    CASE priority WHEN 'urgent' THEN 1 WHEN 'high' THEN 2
                                  WHEN 'normal' THEN 3 ELSE 4 END,
                    created_at DESC
                LIMIT :lim
                """
            ),
            params,
        )
    ).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = list(d.get("tags") or [])
        out.append(SupportTicketOut(**d))
    return out


@router.post("/support-tickets/{ticket_id}/assign")
async def assign_ticket(
    ticket_id: int,
    request: Request,
    admin: str = Query(..., min_length=3, max_length=255, description="email of platform admin"),
    me: CurrentUser = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    row = (
        await db.execute(
            text(
                """
                UPDATE platform_support_tickets
                SET assigned_admin = :a, updated_at = NOW()
                WHERE id = :id
                RETURNING id, assigned_admin, status
                """
            ),
            {"id": ticket_id, "a": admin},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="ticket not found")

    await _log_admin_action(
        db,
        admin_email=me.email,
        action_type="ticket.assign",
        target_type="ticket",
        target_id=str(ticket_id),
        details={"assigned_admin": admin},
        request=request,
    )
    await db.commit()
    return {"ok": True, "id": ticket_id, "assigned_admin": row["assigned_admin"]}


@router.post("/support-tickets/{ticket_id}/resolve")
async def resolve_ticket(
    ticket_id: int,
    request: Request,
    me: CurrentUser = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    row = (
        await db.execute(
            text(
                """
                UPDATE platform_support_tickets
                SET status = 'resolved', resolved_at = NOW(), updated_at = NOW()
                WHERE id = :id AND status NOT IN ('resolved','closed')
                RETURNING id, status, resolved_at
                """
            ),
            {"id": ticket_id},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="ticket not found or already resolved")

    await _log_admin_action(
        db,
        admin_email=me.email,
        action_type="ticket.resolve",
        target_type="ticket",
        target_id=str(ticket_id),
        request=request,
    )
    await db.commit()
    return {"ok": True, "id": ticket_id, "status": "resolved", "resolved_at": row["resolved_at"]}


# ════════════════════════════════════════════════════════════════════════════
# GET /admin-actions-log
# ════════════════════════════════════════════════════════════════════════════
@router.get("/admin-actions-log", response_model=list[AdminActionOut])
async def admin_actions_log(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    admin: str | None = None,
    action_type: str | None = None,
    limit: int = Query(default=300, ge=1, le=2000),
    me: CurrentUser = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> list[AdminActionOut]:
    """Audit trail của Platform Admin — only Platform Admin có quyền xem."""
    where: list[str] = []
    params: dict[str, Any] = {"lim": limit}
    if from_:
        where.append("occurred_at >= :from")
        params["from"] = from_
    if to:
        where.append("occurred_at <= :to")
        params["to"] = to
    if admin:
        where.append("admin_email = :ad")
        params["ad"] = admin
    if action_type:
        where.append("action_type = :at")
        params["at"] = action_type
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    rows = (
        await db.execute(
            text(
                f"""
                SELECT id, admin_email, action_type, target_type, target_id,
                       details, ip_address, user_agent, occurred_at
                FROM platform_admin_actions
                {where_sql}
                ORDER BY occurred_at DESC
                LIMIT :lim
                """
            ),
            params,
        )
    ).mappings().all()
    return [
        AdminActionOut(
            id=int(r["id"]),
            admin_email=r["admin_email"],
            action_type=r["action_type"],
            target_type=r.get("target_type"),
            target_id=r.get("target_id"),
            details=r.get("details") or {},
            ip_address=r.get("ip_address"),
            user_agent=r.get("user_agent"),
            occurred_at=r["occurred_at"],
        )
        for r in rows
    ]


__all__ = ["router"]
