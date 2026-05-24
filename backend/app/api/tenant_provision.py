"""
Zeni Cloud Core — Tenant Provisioning API.

Automated tenant onboarding for platforms deploying on Zeni Cloud.
Single call creates: workspace + admin user + wallet + API token + AI quota.

Endpoints:
  POST /api/v1/tenants/provision     — full tenant setup (Owner only)
  GET  /api/v1/tenants               — list all tenants (Owner only)
  GET  /api/v1/tenants/{ws}/status   — tenant health + resource usage
  POST /api/v1/tenants/{ws}/quota    — set AI quota for tenant
  DELETE /api/v1/tenants/{ws}        — deactivate tenant (soft delete)

Use case:
  Khi Viet-Contech, WitsAGI, ZenoTea, WellKOC muốn deploy lên Zeni Cloud,
  Chairman (Owner) gọi POST /tenants/provision → auto-create everything.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_role
from app.core.security import hash_password
from app.db.base import get_db
from app.services.audit import audit_push

router = APIRouter(prefix="/tenants", tags=["tenant-provisioning"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class TenantProvisionIn(BaseModel):
    """Full tenant provisioning request."""
    workspace_id: str = Field(min_length=2, max_length=32, pattern=r"^[a-z][a-z0-9_-]{1,31}$",
                              description="Unique workspace ID (slug). Example: vietcontech, witsagi")
    workspace_name: str = Field(min_length=2, max_length=128,
                                description="Display name. Example: Viet Contech, WitsAGI")
    admin_email: EmailStr = Field(description="Admin user email for the tenant")
    admin_name: str = Field(min_length=2, max_length=128, description="Admin user display name")
    admin_password: str | None = Field(default=None, min_length=8, max_length=128,
                                       description="Admin password (auto-generated if empty)")
    # AI configuration
    ai_monthly_quota_usd: float = Field(default=10.0, ge=0.0, le=100000.0,
                                         description="Monthly AI spending cap in USD")
    free_credit_vnd: int = Field(default=100_000, ge=0, le=10_000_000,
                                  description="Initial wallet credit in VND")
    # Subscription
    plan: str = Field(default="starter", pattern=r"^(free|starter|growth|business|enterprise)$")
    # Metadata
    industry: str | None = Field(default=None, max_length=64,
                                  description="Ngành nghề: architecture, tea, koc, healthcare, etc.")
    chakra_color: str | None = Field(default=None, max_length=32,
                                      description="C1-C7 luân xa color code")
    tagline: str | None = Field(default=None, max_length=256)
    # API token
    create_api_token: bool = Field(default=True, description="Auto-create API token for service-to-service")
    api_token_scopes: str = Field(default="ai,data,deploy", description="Comma-separated scopes")


class TenantProvisionOut(BaseModel):
    ok: bool
    workspace_id: str
    workspace_name: str
    admin_email: str
    admin_user_id: str
    admin_password_generated: bool
    admin_password: str | None = None  # Only returned if auto-generated
    wallet_balance_vnd: int
    ai_monthly_quota_usd: float
    plan: str
    api_token: str | None = None  # Only returned if create_api_token=True
    api_token_prefix: str | None = None
    dashboard_url: str
    api_base_url: str
    openai_compat_url: str


class TenantStatusOut(BaseModel):
    workspace_id: str
    workspace_name: str
    plan: str | None
    admin_email: str | None
    wallet_balance_vnd: float
    ai_quota_used_usd: float
    ai_quota_limit_usd: float
    projects_count: int
    agents_count: int
    members_count: int
    created_at: str | None


# ---------------------------------------------------------------------------
# POST /tenants/provision — Owner only
# ---------------------------------------------------------------------------
@router.post("/provision", response_model=TenantProvisionOut, status_code=201)
async def provision_tenant(
    data: TenantProvisionIn,
    me: CurrentUser = Depends(require_role("Owner")),
    db: AsyncSession = Depends(get_db),
) -> TenantProvisionOut:
    """
    Full tenant provisioning. Creates:
    1. Workspace
    2. Admin user (with auto-generated password if not provided)
    3. User↔Workspace link (Owner role)
    4. Wallet with initial credit
    5. AI quota (monthly cap)
    6. API Token (for service-to-service)
    7. Audit log entry

    Only the platform Owner can call this.
    """
    # Check workspace doesn't exist
    existing = (await db.execute(text(
        "SELECT id FROM workspaces WHERE id = :ws"
    ), {"ws": data.workspace_id})).scalar_one_or_none()
    if existing:
        raise HTTPException(409, f"Workspace '{data.workspace_id}' already exists")

    # Check admin email not taken
    existing_user = (await db.execute(text(
        "SELECT id FROM users WHERE email = :e"
    ), {"e": str(data.admin_email).lower()})).scalar_one_or_none()
    if existing_user:
        raise HTTPException(409, f"Email '{data.admin_email}' already registered")

    # 1. Create workspace
    ws_code = data.workspace_id[:8].upper()
    # Check code collision
    code_exists = (await db.execute(text(
        "SELECT id FROM workspaces WHERE code = :c"
    ), {"c": ws_code})).scalar_one_or_none()
    if code_exists:
        ws_code = (data.workspace_id[:5] + secrets.token_hex(2)).upper()[:8]

    await db.execute(text("""
        INSERT INTO workspaces (id, code, name, tagline, color, created_at)
        VALUES (:id, :code, :name, :tagline, :color, NOW())
    """), {
        "id": data.workspace_id,
        "code": ws_code,
        "name": data.workspace_name,
        "tagline": data.tagline or f"{data.workspace_name} on Zeni Cloud",
        "color": data.chakra_color or "var(--crown)",
    })

    # 2. Create admin user
    password_generated = data.admin_password is None
    raw_password = data.admin_password or secrets.token_urlsafe(16)
    user_id = str(uuid.uuid4())

    await db.execute(text("""
        INSERT INTO users (id, email, password_hash, name, role, created_at)
        VALUES (:id, :email, :hash, :name, 'Admin', NOW())
    """), {
        "id": user_id,
        "email": str(data.admin_email).lower(),
        "hash": hash_password(raw_password),
        "name": data.admin_name,
    })

    # 3. User↔Workspace link
    await db.execute(text("""
        INSERT INTO user_workspaces (user_id, workspace_id, role)
        VALUES (:uid, :ws, 'Owner')
    """), {"uid": user_id, "ws": data.workspace_id})

    # 4. Wallet with initial credit
    await db.execute(text("""
        INSERT INTO wallet_balances (workspace_id, balance_vnd, total_topped_up)
        VALUES (:ws, :credit, :credit)
        ON CONFLICT (workspace_id) DO NOTHING
    """), {"ws": data.workspace_id, "credit": data.free_credit_vnd})

    # 5. AI quota
    await db.execute(text("""
        INSERT INTO router_tenant_quotas (workspace_id, monthly_quota_usd)
        VALUES (:ws, :quota)
        ON CONFLICT (workspace_id) DO UPDATE
            SET monthly_quota_usd = EXCLUDED.monthly_quota_usd
    """), {"ws": data.workspace_id, "quota": data.ai_monthly_quota_usd})

    # 6. Subscription record (if pricing_subscriptions table exists)
    try:
        plan_row = (await db.execute(text(
            "SELECT id FROM pricing_plans WHERE slug = :s LIMIT 1"
        ), {"s": data.plan})).scalar_one_or_none()
        if plan_row:
            await db.execute(text("""
                INSERT INTO pricing_subscriptions (workspace_id, plan_id, status, current_period_start, current_period_end)
                VALUES (:ws, :pid, 'active', NOW(), NOW() + INTERVAL '30 days')
                ON CONFLICT DO NOTHING
            """), {"ws": data.workspace_id, "pid": plan_row})
    except Exception:
        pass  # pricing_subscriptions may not exist yet

    # 7. API Token
    api_token_raw = None
    api_token_prefix = None
    if data.create_api_token:
        api_token_raw = f"zeni_pat_{secrets.token_urlsafe(32)}"
        api_token_prefix = api_token_raw[:16]
        token_hash = hashlib.sha256(api_token_raw.encode()).hexdigest()
        token_id = str(uuid.uuid4())

        await db.execute(text("""
            INSERT INTO api_tokens (id, workspace_id, name, token_hash, token_prefix, scopes, created_by, expires_at)
            VALUES (:id, :ws, :name, :hash, :prefix, :scopes, :uid, :exp)
        """), {
            "id": token_id,
            "ws": data.workspace_id,
            "name": f"auto-{data.workspace_id}-provision",
            "hash": token_hash,
            "prefix": api_token_prefix,
            "scopes": data.api_token_scopes,
            "uid": user_id,
            "exp": datetime.now(timezone.utc) + timedelta(days=365),
        })

    # 8. Audit
    await audit_push(
        db, actor=me.email, workspace_id=data.workspace_id,
        action="tenant.provision", target=data.workspace_id, severity="ok",
        metadata={
            "admin_email": str(data.admin_email),
            "plan": data.plan,
            "ai_quota_usd": data.ai_monthly_quota_usd,
            "free_credit_vnd": data.free_credit_vnd,
            "industry": data.industry,
        },
    )

    await db.commit()

    return TenantProvisionOut(
        ok=True,
        workspace_id=data.workspace_id,
        workspace_name=data.workspace_name,
        admin_email=str(data.admin_email).lower(),
        admin_user_id=user_id,
        admin_password_generated=password_generated,
        admin_password=raw_password if password_generated else None,
        wallet_balance_vnd=data.free_credit_vnd,
        ai_monthly_quota_usd=data.ai_monthly_quota_usd,
        plan=data.plan,
        api_token=api_token_raw,
        api_token_prefix=api_token_prefix,
        dashboard_url=f"https://zenicloud.io/app?ws={data.workspace_id}",
        api_base_url=f"https://zenicloud.io/api/v1",
        openai_compat_url=f"https://zenicloud.io/api/v1/openai/v1",
    )


# ---------------------------------------------------------------------------
# GET /tenants — list all tenants (Owner only)
# ---------------------------------------------------------------------------
@router.get("")
async def list_tenants(
    me: CurrentUser = Depends(require_role("Owner")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List all workspaces with basic stats."""
    rows = (await db.execute(text("""
        SELECT w.id, w.name, w.tagline, w.color, w.created_at,
               COALESCE(wallet.balance_vnd, 0) as balance_vnd,
               COALESCE(quota.monthly_quota_usd, 5.0) as ai_quota_usd,
               COALESCE(quota.current_month_usage_usd, 0) as ai_usage_usd,
               (SELECT COUNT(*) FROM projects p WHERE p.workspace_id = w.id) as projects,
               (SELECT COUNT(*) FROM user_workspaces uw WHERE uw.workspace_id = w.id) as members
        FROM workspaces w
        LEFT JOIN wallet_balances wallet ON wallet.workspace_id = w.id
        LEFT JOIN router_tenant_quotas quota ON quota.workspace_id = w.id
        ORDER BY w.created_at DESC
    """))).mappings().all()

    return {
        "tenants": [
            {
                "workspace_id": r["id"],
                "name": r["name"],
                "tagline": r["tagline"],
                "color": r["color"],
                "created_at": str(r["created_at"]) if r["created_at"] else None,
                "wallet_balance_vnd": float(r["balance_vnd"]),
                "ai_quota_usd": float(r["ai_quota_usd"]),
                "ai_usage_usd": float(r["ai_usage_usd"]),
                "projects": r["projects"],
                "members": r["members"],
            }
            for r in rows
        ],
        "count": len(rows),
    }


# ---------------------------------------------------------------------------
# GET /tenants/{ws}/status — detailed tenant health
# ---------------------------------------------------------------------------
@router.get("/{ws}/status", response_model=TenantStatusOut)
async def tenant_status(
    ws: str,
    me: CurrentUser = Depends(require_role("Owner")),
    db: AsyncSession = Depends(get_db),
) -> TenantStatusOut:
    """Get detailed status for a specific tenant workspace."""
    row = (await db.execute(text("""
        SELECT w.id, w.name, w.created_at,
               COALESCE(wallet.balance_vnd, 0) as balance_vnd,
               COALESCE(quota.monthly_quota_usd, 5.0) as quota_limit,
               COALESCE(quota.current_month_usage_usd, 0) as quota_used
        FROM workspaces w
        LEFT JOIN wallet_balances wallet ON wallet.workspace_id = w.id
        LEFT JOIN router_tenant_quotas quota ON quota.workspace_id = w.id
        WHERE w.id = :ws
    """), {"ws": ws})).mappings().first()

    if not row:
        raise HTTPException(404, "Workspace not found")

    # Get counts
    projects = (await db.execute(text(
        "SELECT COUNT(*) FROM projects WHERE workspace_id = :ws"
    ), {"ws": ws})).scalar() or 0
    agents = (await db.execute(text(
        "SELECT COUNT(*) FROM agents WHERE workspace_id = :ws"
    ), {"ws": ws})).scalar() or 0
    members = (await db.execute(text(
        "SELECT COUNT(*) FROM user_workspaces WHERE workspace_id = :ws"
    ), {"ws": ws})).scalar() or 0

    # Get admin email
    admin_row = (await db.execute(text("""
        SELECT u.email FROM users u
        JOIN user_workspaces uw ON uw.user_id = u.id
        WHERE uw.workspace_id = :ws AND uw.role = 'Owner'
        LIMIT 1
    """), {"ws": ws})).scalar_one_or_none()

    # Get plan
    plan = None
    try:
        plan_row = (await db.execute(text("""
            SELECT pp.slug FROM pricing_subscriptions ps
            JOIN pricing_plans pp ON pp.id = ps.plan_id
            WHERE ps.workspace_id = :ws AND ps.status = 'active'
            LIMIT 1
        """), {"ws": ws})).scalar_one_or_none()
        plan = plan_row
    except Exception:
        pass

    return TenantStatusOut(
        workspace_id=row["id"],
        workspace_name=row["name"],
        plan=plan,
        admin_email=admin_row,
        wallet_balance_vnd=float(row["balance_vnd"]),
        ai_quota_used_usd=float(row["quota_used"]),
        ai_quota_limit_usd=float(row["quota_limit"]),
        projects_count=projects,
        agents_count=agents,
        members_count=members,
        created_at=str(row["created_at"]) if row["created_at"] else None,
    )


# ---------------------------------------------------------------------------
# POST /tenants/{ws}/quota — set AI quota
# ---------------------------------------------------------------------------
@router.post("/{ws}/quota")
async def set_tenant_quota(
    ws: str,
    monthly_quota_usd: float,
    me: CurrentUser = Depends(require_role("Owner")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Set or update the monthly AI spending quota for a tenant."""
    from app.services.router.quota import set_quota
    await set_quota(db, ws, monthly_quota_usd)
    await audit_push(db, actor=me.email, workspace_id=ws,
                     action="tenant.quota.set", target=ws, severity="ok",
                     metadata={"monthly_quota_usd": monthly_quota_usd})
    await db.commit()
    return {"ok": True, "workspace_id": ws, "monthly_quota_usd": monthly_quota_usd}
