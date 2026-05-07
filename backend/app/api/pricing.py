"""
Zeni Cloud Core — Pricing & Subscription API.

Public:
  GET  /api/v1/pricing/plans                    — list all public plans (for /pricing page)
  GET  /api/v1/pricing/plans/{plan_id}          — plan detail

Auth required (workspace member):
  GET  /api/v1/pricing/subscription?ws=...      — current subscription
  POST /api/v1/pricing/subscribe                — subscribe to a plan (manual payment for now)
  POST /api/v1/pricing/cancel?ws=...            — cancel at period end
  GET  /api/v1/pricing/usage?ws=...             — current month usage + quota %
  GET  /api/v1/pricing/usage/history?ws=...     — last 12 months usage

Admin (Owner role):
  POST /api/v1/pricing/admin/activate           — admin activate subscription (after manual VietQR confirm)
  POST /api/v1/pricing/admin/extend             — extend current period

Notes
-----
- Workspace identifier: external endpoints accept ``workspace_code`` which can be
  either ``workspaces.id`` (VARCHAR(32)) or ``workspaces.code`` (VARCHAR(8)).
  Helper :func:`_resolve_workspace` looks up by ``id`` first then ``code``.
- Subscribing with ``payment_method='manual'`` activates immediately
  (Zeni Holdings internal use case — anh CEO's account).
- Other payment methods (vietqr/vnpay/zeni_token) start in ``trial`` status
  and require admin/activate after confirmation.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push

log = logging.getLogger("zeni.api.pricing")

router = APIRouter(prefix="/pricing", tags=["pricing", "subscriptions"])

# ─── Pydantic schemas ────────────────────────────────────────────────────────


class PlanOut(BaseModel):
    id: str
    name: str
    price_vnd_monthly: int
    price_usd_monthly: float
    quota_requests_per_month: int
    quota_ai_tokens_per_month: int
    quota_storage_gb: int
    quota_router_usd_per_month: float
    quota_projects: int            # -1 = unlimited
    quota_dev_seats: int
    sla_uptime_percent: float
    support_level: str
    custom_domain: bool
    features: list[str]
    sort_order: int | None = None


class SubscribeIn(BaseModel):
    workspace_code: str = Field(min_length=1, max_length=32)
    plan_id: str = Field(pattern=r"^(free|starter|pro|business|enterprise)$")
    payment_method: str = Field(default="manual", pattern=r"^(manual|vietqr|vnpay|zeni_token)$")
    payment_reference: str | None = Field(default=None, max_length=255)
    notes: str | None = Field(default=None, max_length=500)


class AdminActivateIn(BaseModel):
    workspace_code: str = Field(min_length=1, max_length=32)
    plan_id: str = Field(pattern=r"^(free|starter|pro|business|enterprise)$")
    duration_months: int = Field(default=1, ge=1, le=120)
    payment_reference: str | None = Field(default=None, max_length=255)
    notes: str | None = Field(default=None, max_length=500)


class AdminExtendIn(BaseModel):
    workspace_code: str = Field(min_length=1, max_length=32)
    extra_days: int = Field(ge=1, le=3650)
    notes: str | None = Field(default=None, max_length=500)


# ─── helpers ─────────────────────────────────────────────────────────────────


async def _resolve_workspace(db: AsyncSession, workspace_code: str) -> tuple[str, str, str]:
    """Resolve a workspace_code (id OR code) → (workspace_id, code, name).

    Raises 404 if not found.
    """
    row = (await db.execute(
        text("""SELECT id, code, name FROM workspaces
                WHERE id = :w OR code = :w
                LIMIT 1"""),
        {"w": workspace_code},
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Workspace '{workspace_code}' không tồn tại")
    return row[0], row[1], row[2]


async def _require_owner_of(db: AsyncSession, me: CurrentUser, workspace_id: str) -> None:
    """User must be Owner role globally OR Owner of the specific workspace."""
    if me.role == "Owner":
        return
    # Check user_workspaces for Owner/Admin role on this workspace
    row = (await db.execute(
        text("""SELECT role FROM user_workspaces
                WHERE user_id = :u AND workspace_id = :w"""),
        {"u": me.id, "w": workspace_id},
    )).first()
    if row is None:
        raise HTTPException(status_code=403, detail="Bạn không có quyền với workspace này")
    if row[0] not in ("Owner", "Admin"):
        raise HTTPException(status_code=403, detail="Cần Owner/Admin để đăng ký gói")


def _plan_row_to_dict(r) -> dict:
    """Map mappings row → PlanOut-shaped dict."""
    return {
        "id":                          r["id"],
        "name":                        r["name"],
        "price_vnd_monthly":           int(r["price_vnd_monthly"] or 0),
        "price_usd_monthly":           float(r["price_usd_monthly"] or 0),
        "quota_requests_per_month":    int(r["quota_requests_per_month"] or 0),
        "quota_ai_tokens_per_month":   int(r["quota_ai_tokens_per_month"] or 0),
        "quota_storage_gb":            int(r["quota_storage_gb"] or 0),
        "quota_router_usd_per_month":  float(r["quota_router_usd_per_month"] or 0),
        "quota_projects":              int(r["quota_projects"] or 0),
        "quota_dev_seats":             int(r["quota_dev_seats"] or 0),
        "sla_uptime_percent":          float(r["sla_uptime_percent"] or 0),
        "support_level":               r["support_level"] or "community",
        "custom_domain":               bool(r["custom_domain"]),
        "features":                    list(r["features"] or []),
        "sort_order":                  int(r["sort_order"]) if r["sort_order"] is not None else None,
    }


def _quota_pct(used: int | float, limit: int | float) -> float:
    """Percentage used; -1 limit (unlimited) returns 0.0."""
    try:
        if limit is None or limit < 0 or limit == 0:
            return 0.0
        return round((float(used) / float(limit)) * 100.0, 2)
    except Exception:
        return 0.0


async def check_quota_for_workspace(
    db: AsyncSession,
    workspace_code: str,
    resource: str = "requests",
) -> tuple[bool, int, int]:
    """Return (within_quota, current, limit) for a workspace + resource.

    Resources supported: 'requests', 'ai_tokens', 'storage_gb', 'router_usd'.
    Limit = -1 means unlimited (always within quota).
    """
    column_map = {
        "requests":   ("requests_count",   "quota_requests_per_month"),
        "ai_tokens":  ("ai_tokens_count",  "quota_ai_tokens_per_month"),
        "storage_gb": ("storage_gb_avg",   "quota_storage_gb"),
        "router_usd": ("router_cost_usd",  "quota_router_usd_per_month"),
    }
    if resource not in column_map:
        raise ValueError(f"unknown resource: {resource}")
    usage_col, plan_col = column_map[resource]

    row = (await db.execute(text(f"""
        SELECT
          COALESCE(u.{usage_col}, 0) AS current_value,
          p.{plan_col} AS limit_value
        FROM workspaces w
        JOIN workspace_subscriptions s ON s.workspace_id = w.id
        JOIN pricing_plans p ON p.id = s.plan_id
        LEFT JOIN workspace_usage u
            ON u.workspace_id = w.id
           AND u.period_start = DATE_TRUNC('month', NOW())::DATE
        WHERE (w.id = :w OR w.code = :w)
          AND s.status IN ('active','trial')
        LIMIT 1
    """), {"w": workspace_code})).first()

    if row is None:
        # No subscription found → treat as unlimited (caller may decide otherwise)
        return True, 0, -1

    current = int(float(row[0] or 0))
    limit = int(float(row[1] or 0))
    if limit < 0:
        return True, current, limit
    return current < limit, current, limit


# ─── Public endpoints ────────────────────────────────────────────────────────


@router.get("/plans")
async def list_plans(db: AsyncSession = Depends(get_db)) -> dict:
    """Public — list all visible pricing plans (for landing /pricing page)."""
    rows = (await db.execute(text("""
        SELECT id, name, price_vnd_monthly, price_usd_monthly,
               quota_requests_per_month, quota_ai_tokens_per_month, quota_storage_gb,
               quota_router_usd_per_month, quota_projects, quota_dev_seats,
               sla_uptime_percent, support_level, custom_domain, features, sort_order
          FROM pricing_plans
         WHERE is_public = TRUE
         ORDER BY sort_order ASC NULLS LAST, price_vnd_monthly ASC
    """))).mappings().all()
    return {
        "currency": "VND",
        "count": len(rows),
        "plans": [_plan_row_to_dict(r) for r in rows],
    }


@router.get("/plans/{plan_id}")
async def get_plan(plan_id: str, db: AsyncSession = Depends(get_db)) -> PlanOut:
    """Public — single plan detail."""
    row = (await db.execute(text("""
        SELECT id, name, price_vnd_monthly, price_usd_monthly,
               quota_requests_per_month, quota_ai_tokens_per_month, quota_storage_gb,
               quota_router_usd_per_month, quota_projects, quota_dev_seats,
               sla_uptime_percent, support_level, custom_domain, features, sort_order
          FROM pricing_plans
         WHERE id = :p
    """), {"p": plan_id})).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Plan '{plan_id}' không tồn tại")
    return PlanOut(**_plan_row_to_dict(row))


# ─── Auth — workspace member endpoints ───────────────────────────────────────


@router.get("/subscription")
async def get_subscription(
    ws: str = Query(..., description="workspace_id hoặc workspace.code"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Current subscription cho workspace + plan details."""
    workspace_id, code, name = await _resolve_workspace(db, ws)
    await require_workspace_access(workspace_id, me)

    row = (await db.execute(text("""
        SELECT s.workspace_id, s.plan_id, s.status, s.started_at,
               s.current_period_start, s.current_period_end,
               s.cancel_at_period_end, s.payment_method, s.payment_reference,
               s.notes, s.updated_at,
               p.id AS p_id, p.name AS p_name, p.price_vnd_monthly, p.price_usd_monthly,
               p.quota_requests_per_month, p.quota_ai_tokens_per_month, p.quota_storage_gb,
               p.quota_router_usd_per_month, p.quota_projects, p.quota_dev_seats,
               p.sla_uptime_percent, p.support_level, p.custom_domain, p.features, p.sort_order
          FROM workspace_subscriptions s
          JOIN pricing_plans p ON p.id = s.plan_id
         WHERE s.workspace_id = :w
    """), {"w": workspace_id})).mappings().first()

    if row is None:
        return {
            "workspace_id": workspace_id,
            "workspace_code": code,
            "workspace_name": name,
            "active": False,
            "plan": None,
            "subscription": None,
        }

    plan_dict = {
        "id":                         row["p_id"],
        "name":                       row["p_name"],
        "price_vnd_monthly":          int(row["price_vnd_monthly"] or 0),
        "price_usd_monthly":          float(row["price_usd_monthly"] or 0),
        "quota_requests_per_month":   int(row["quota_requests_per_month"] or 0),
        "quota_ai_tokens_per_month":  int(row["quota_ai_tokens_per_month"] or 0),
        "quota_storage_gb":           int(row["quota_storage_gb"] or 0),
        "quota_router_usd_per_month": float(row["quota_router_usd_per_month"] or 0),
        "quota_projects":             int(row["quota_projects"] or 0),
        "quota_dev_seats":            int(row["quota_dev_seats"] or 0),
        "sla_uptime_percent":         float(row["sla_uptime_percent"] or 0),
        "support_level":              row["support_level"] or "community",
        "custom_domain":              bool(row["custom_domain"]),
        "features":                   list(row["features"] or []),
        "sort_order":                 int(row["sort_order"]) if row["sort_order"] is not None else None,
    }
    return {
        "workspace_id": workspace_id,
        "workspace_code": code,
        "workspace_name": name,
        "active": row["status"] in ("active", "trial"),
        "plan": plan_dict,
        "subscription": {
            "plan_id":              row["plan_id"],
            "status":               row["status"],
            "started_at":           row["started_at"].isoformat() if row["started_at"] else None,
            "current_period_start": row["current_period_start"].isoformat() if row["current_period_start"] else None,
            "current_period_end":   row["current_period_end"].isoformat() if row["current_period_end"] else None,
            "cancel_at_period_end": bool(row["cancel_at_period_end"]),
            "payment_method":       row["payment_method"],
            "payment_reference":    row["payment_reference"],
            "notes":                row["notes"],
            "updated_at":           row["updated_at"].isoformat() if row["updated_at"] else None,
        },
    }


@router.post("/subscribe")
async def subscribe(
    data: SubscribeIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Subscribe (or change plan). Requires Owner/Admin role of the workspace.

    Logic:
      - Lookup workspace + ensure caller is Owner/Admin
      - Lookup plan
      - Detect previous plan (for upgrade/downgrade event)
      - UPSERT workspace_subscriptions
      - If payment_method='manual' → status='active' immediately (CEO internal use)
      - Otherwise → status='trial' until admin/activate confirms payment
      - Audit + quota_event log
    """
    try:
        workspace_id, code, name = await _resolve_workspace(db, data.workspace_code)
        await _require_owner_of(db, me, workspace_id)

        plan = (await db.execute(text("""
            SELECT id, name, price_vnd_monthly, price_usd_monthly
              FROM pricing_plans WHERE id = :p
        """), {"p": data.plan_id})).mappings().first()
        if plan is None:
            raise HTTPException(status_code=400, detail=f"Plan '{data.plan_id}' không hợp lệ")

        # Previous plan (for event log)
        prev = (await db.execute(text("""
            SELECT plan_id FROM workspace_subscriptions WHERE workspace_id = :w
        """), {"w": workspace_id})).first()
        prev_plan_id = prev[0] if prev else None

        # 'manual' or 'free' → activate immediately. Other methods → trial until admin confirm.
        is_manual = data.payment_method == "manual"
        is_free = data.plan_id == "free"
        new_status = "active" if (is_manual or is_free) else "trial"

        now = datetime.now(timezone.utc)
        period_end = now + timedelta(days=30)

        await db.execute(text("""
            INSERT INTO workspace_subscriptions
                (workspace_id, plan_id, status, started_at,
                 current_period_start, current_period_end,
                 cancel_at_period_end, payment_method, payment_reference,
                 notes, updated_at)
            VALUES
                (:w, :p, :s, :now, :now, :pe, FALSE, :pm, :pr, :nt, :now)
            ON CONFLICT (workspace_id) DO UPDATE SET
                plan_id              = EXCLUDED.plan_id,
                status               = EXCLUDED.status,
                current_period_start = EXCLUDED.current_period_start,
                current_period_end   = EXCLUDED.current_period_end,
                cancel_at_period_end = FALSE,
                payment_method       = EXCLUDED.payment_method,
                payment_reference    = COALESCE(EXCLUDED.payment_reference, workspace_subscriptions.payment_reference),
                notes                = COALESCE(EXCLUDED.notes, workspace_subscriptions.notes),
                updated_at           = EXCLUDED.updated_at
        """), {
            "w": workspace_id, "p": data.plan_id, "s": new_status,
            "now": now, "pe": period_end,
            "pm": data.payment_method,
            "pr": data.payment_reference, "nt": data.notes,
        })

        # Determine event type
        event_type = "plan_subscribed"
        if prev_plan_id and prev_plan_id != data.plan_id:
            order_map = {"free": 0, "starter": 1, "pro": 2, "business": 3, "enterprise": 4}
            event_type = (
                "plan_upgraded"
                if order_map.get(data.plan_id, 0) > order_map.get(prev_plan_id, 0)
                else "plan_downgraded"
            )

        await db.execute(text("""
            INSERT INTO quota_events (workspace_id, event_type, detail)
            VALUES (:w, :ev, :dt)
        """), {
            "w": workspace_id, "ev": event_type,
            "dt": f"plan={data.plan_id} from={prev_plan_id} method={data.payment_method} status={new_status}",
        })

        await audit_push(
            db, actor=me.email, workspace_id=workspace_id,
            action="pricing.subscribe", target=data.plan_id, severity="ok",
            metadata={
                "previous_plan": prev_plan_id,
                "payment_method": data.payment_method,
                "payment_reference": data.payment_reference,
                "status": new_status,
            },
        )
        await db.commit()
        log.info("[subscribe] ws=%s plan=%s status=%s by=%s", workspace_id, data.plan_id, new_status, me.email)

        return {
            "ok": True,
            "workspace_id": workspace_id,
            "workspace_name": name,
            "plan": {
                "id":                plan["id"],
                "name":              plan["name"],
                "price_vnd_monthly": int(plan["price_vnd_monthly"] or 0),
                "price_usd_monthly": float(plan["price_usd_monthly"] or 0),
            },
            "status": new_status,
            "current_period_end": period_end.isoformat(),
            "payment_method": data.payment_method,
            "message_vi": (
                "Kích hoạt gói thành công." if new_status == "active"
                else f"Đã tạo subscription trial. Vui lòng xác nhận thanh toán {data.payment_method} để kích hoạt."
            ),
        }
    except HTTPException:
        raise
    except Exception as e:
        log.exception("subscribe failed")
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Subscribe failed: {type(e).__name__}: {e}")


@router.post("/cancel")
async def cancel_subscription(
    ws: str = Query(..., description="workspace_id hoặc workspace.code"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Cancel at period end (subscription stays active until current_period_end)."""
    workspace_id, code, name = await _resolve_workspace(db, ws)
    await _require_owner_of(db, me, workspace_id)

    row = (await db.execute(text("""
        SELECT plan_id, status, current_period_end FROM workspace_subscriptions
         WHERE workspace_id = :w
    """), {"w": workspace_id})).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Workspace chưa có subscription")

    if row[1] in ("cancelled", "suspended"):
        raise HTTPException(status_code=400, detail=f"Subscription đã ở trạng thái {row[1]}")

    await db.execute(text("""
        UPDATE workspace_subscriptions
           SET cancel_at_period_end = TRUE, updated_at = NOW()
         WHERE workspace_id = :w
    """), {"w": workspace_id})

    await db.execute(text("""
        INSERT INTO quota_events (workspace_id, event_type, detail)
        VALUES (:w, 'plan_cancelled', :dt)
    """), {"w": workspace_id, "dt": f"plan={row[0]} cancel_at_period_end=true"})

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="pricing.cancel", target=row[0], severity="info",
        metadata={"current_period_end": row[2].isoformat() if row[2] else None},
    )
    await db.commit()
    return {
        "ok": True,
        "workspace_id": workspace_id,
        "current_plan": row[0],
        "cancel_at_period_end": True,
        "expires_at": row[2].isoformat() if row[2] else None,
        "message_vi": "Đã đặt huỷ gói. Subscription vẫn hoạt động đến hết kỳ hiện tại.",
    }


@router.get("/usage")
async def get_current_usage(
    ws: str = Query(..., description="workspace_id hoặc workspace.code"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Current month usage + quota percentage per resource."""
    workspace_id, code, name = await _resolve_workspace(db, ws)
    await require_workspace_access(workspace_id, me)

    row = (await db.execute(text("""
        SELECT
          COALESCE(u.requests_count, 0)   AS req,
          COALESCE(u.ai_tokens_count, 0)  AS tok,
          COALESCE(u.storage_gb_avg, 0)   AS stor,
          COALESCE(u.router_cost_usd, 0)  AS rcost,
          u.last_request_at,
          u.period_start,
          p.id AS p_id, p.name AS p_name, p.price_vnd_monthly, p.price_usd_monthly,
          p.quota_requests_per_month, p.quota_ai_tokens_per_month, p.quota_storage_gb,
          p.quota_router_usd_per_month, p.quota_projects, p.quota_dev_seats,
          p.sla_uptime_percent, p.support_level, p.custom_domain, p.features, p.sort_order
        FROM workspaces w
        JOIN workspace_subscriptions s ON s.workspace_id = w.id
        JOIN pricing_plans p ON p.id = s.plan_id
        LEFT JOIN workspace_usage u
               ON u.workspace_id = w.id
              AND u.period_start = DATE_TRUNC('month', NOW())::DATE
        WHERE w.id = :w
    """), {"w": workspace_id})).mappings().first()

    if row is None:
        raise HTTPException(status_code=404, detail="Workspace chưa có subscription")

    plan_dict = {
        "id":                         row["p_id"],
        "name":                       row["p_name"],
        "price_vnd_monthly":          int(row["price_vnd_monthly"] or 0),
        "price_usd_monthly":          float(row["price_usd_monthly"] or 0),
        "quota_requests_per_month":   int(row["quota_requests_per_month"] or 0),
        "quota_ai_tokens_per_month":  int(row["quota_ai_tokens_per_month"] or 0),
        "quota_storage_gb":           int(row["quota_storage_gb"] or 0),
        "quota_router_usd_per_month": float(row["quota_router_usd_per_month"] or 0),
        "quota_projects":             int(row["quota_projects"] or 0),
        "quota_dev_seats":            int(row["quota_dev_seats"] or 0),
        "sla_uptime_percent":         float(row["sla_uptime_percent"] or 0),
        "support_level":              row["support_level"] or "community",
        "custom_domain":              bool(row["custom_domain"]),
        "features":                   list(row["features"] or []),
        "sort_order":                 int(row["sort_order"]) if row["sort_order"] is not None else None,
    }

    req = int(row["req"] or 0)
    tok = int(row["tok"] or 0)
    stor = float(row["stor"] or 0)
    rcost = float(row["rcost"] or 0)

    return {
        "workspace_id": workspace_id,
        "workspace_code": code,
        "period_start": row["period_start"].isoformat() if row["period_start"] else None,
        "usage": {
            "requests_count":   req,
            "ai_tokens_count":  tok,
            "storage_gb_avg":   stor,
            "router_cost_usd":  rcost,
            "last_request_at":  row["last_request_at"].isoformat() if row["last_request_at"] else None,
        },
        "plan": plan_dict,
        "quota_percent": {
            "requests":   _quota_pct(req,   plan_dict["quota_requests_per_month"]),
            "ai_tokens":  _quota_pct(tok,   plan_dict["quota_ai_tokens_per_month"]),
            "storage_gb": _quota_pct(stor,  plan_dict["quota_storage_gb"]),
            "router_usd": _quota_pct(rcost, plan_dict["quota_router_usd_per_month"]),
        },
    }


@router.get("/usage/history")
async def get_usage_history(
    ws: str = Query(..., description="workspace_id hoặc workspace.code"),
    months: int = Query(default=12, ge=1, le=36),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Last N months of usage (default 12)."""
    workspace_id, code, name = await _resolve_workspace(db, ws)
    await require_workspace_access(workspace_id, me)

    rows = (await db.execute(text("""
        SELECT period_start, requests_count, ai_tokens_count,
               storage_gb_avg, router_cost_usd, last_request_at
          FROM workspace_usage
         WHERE workspace_id = :w
         ORDER BY period_start DESC
         LIMIT :n
    """), {"w": workspace_id, "n": months})).all()

    return {
        "workspace_id": workspace_id,
        "count": len(rows),
        "history": [
            {
                "period_start":     r[0].isoformat() if r[0] else None,
                "requests_count":   int(r[1] or 0),
                "ai_tokens_count":  int(r[2] or 0),
                "storage_gb_avg":   float(r[3] or 0),
                "router_cost_usd":  float(r[4] or 0),
                "last_request_at":  r[5].isoformat() if r[5] else None,
            }
            for r in rows
        ],
    }


# ─── Admin endpoints (Owner role only) ───────────────────────────────────────


@router.post("/admin/activate")
async def admin_activate_subscription(
    data: AdminActivateIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Owner-only: activate (or extend) subscription after manual VietQR confirmation."""
    if me.role != "Owner":
        raise HTTPException(status_code=403, detail="Cần Owner role")

    workspace_id, code, name = await _resolve_workspace(db, data.workspace_code)

    plan = (await db.execute(text("""
        SELECT id, name FROM pricing_plans WHERE id = :p
    """), {"p": data.plan_id})).first()
    if plan is None:
        raise HTTPException(status_code=400, detail=f"Plan '{data.plan_id}' không hợp lệ")

    now = datetime.now(timezone.utc)
    period_end = now + timedelta(days=30 * data.duration_months)

    await db.execute(text("""
        INSERT INTO workspace_subscriptions
            (workspace_id, plan_id, status, started_at,
             current_period_start, current_period_end,
             cancel_at_period_end, payment_method, payment_reference,
             notes, updated_at)
        VALUES
            (:w, :p, 'active', :now, :now, :pe, FALSE, 'manual', :pr, :nt, :now)
        ON CONFLICT (workspace_id) DO UPDATE SET
            plan_id              = EXCLUDED.plan_id,
            status               = 'active',
            current_period_start = EXCLUDED.current_period_start,
            current_period_end   = EXCLUDED.current_period_end,
            cancel_at_period_end = FALSE,
            payment_method       = 'manual',
            payment_reference    = COALESCE(EXCLUDED.payment_reference, workspace_subscriptions.payment_reference),
            notes                = COALESCE(EXCLUDED.notes, workspace_subscriptions.notes),
            updated_at           = EXCLUDED.updated_at
    """), {
        "w": workspace_id, "p": data.plan_id, "now": now, "pe": period_end,
        "pr": data.payment_reference, "nt": data.notes,
    })

    await db.execute(text("""
        INSERT INTO quota_events (workspace_id, event_type, detail)
        VALUES (:w, 'admin_activated', :dt)
    """), {"w": workspace_id,
           "dt": f"plan={data.plan_id} months={data.duration_months} ref={data.payment_reference or ''}"})

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="pricing.admin.activate", target=data.plan_id, severity="ok",
        metadata={
            "duration_months": data.duration_months,
            "payment_reference": data.payment_reference,
            "notes": data.notes,
        },
    )
    await db.commit()
    log.info("[admin/activate] ws=%s plan=%s months=%d by=%s",
             workspace_id, data.plan_id, data.duration_months, me.email)

    return {
        "ok": True,
        "workspace_id": workspace_id,
        "workspace_name": name,
        "plan_id": data.plan_id,
        "duration_months": data.duration_months,
        "current_period_end": period_end.isoformat(),
        "status": "active",
    }


@router.post("/admin/extend")
async def admin_extend_subscription(
    data: AdminExtendIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Owner-only: extend current_period_end by N days."""
    if me.role != "Owner":
        raise HTTPException(status_code=403, detail="Cần Owner role")

    workspace_id, code, name = await _resolve_workspace(db, data.workspace_code)

    row = (await db.execute(text("""
        SELECT plan_id, current_period_end FROM workspace_subscriptions WHERE workspace_id = :w
    """), {"w": workspace_id})).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Workspace chưa có subscription")

    new_end = (row[1] or datetime.now(timezone.utc)) + timedelta(days=data.extra_days)

    await db.execute(text("""
        UPDATE workspace_subscriptions
           SET current_period_end   = :pe,
               cancel_at_period_end = FALSE,
               status               = CASE WHEN status IN ('cancelled','suspended','past_due')
                                           THEN 'active' ELSE status END,
               updated_at           = NOW(),
               notes                = COALESCE(:nt, notes)
         WHERE workspace_id = :w
    """), {"w": workspace_id, "pe": new_end, "nt": data.notes})

    await db.execute(text("""
        INSERT INTO quota_events (workspace_id, event_type, detail)
        VALUES (:w, 'plan_extended', :dt)
    """), {"w": workspace_id,
           "dt": f"plan={row[0]} extra_days={data.extra_days} new_end={new_end.isoformat()}"})

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="pricing.admin.extend", target=row[0], severity="ok",
        metadata={"extra_days": data.extra_days, "notes": data.notes},
    )
    await db.commit()
    log.info("[admin/extend] ws=%s +%dd by=%s", workspace_id, data.extra_days, me.email)

    return {
        "ok": True,
        "workspace_id": workspace_id,
        "workspace_name": name,
        "plan_id": row[0],
        "extra_days": data.extra_days,
        "new_period_end": new_end.isoformat(),
    }
