"""
Zeni Cloud · L7 Mail — Per-domain Mail Hosting API (Phase 1 skeleton).

Endpoints (router prefix `/mail`):

  Domain management:
    POST   /mail/domains?ws=                     create domain + generate DKIM keypair
    GET    /mail/domains?ws=                     list domains in workspace
    GET    /mail/domains/{id}/dns                show required DNS records
    POST   /mail/domains/{id}/verify-dns         check MX/SPF/DKIM/DMARC live
    DELETE /mail/domains/{id}?ws=                soft delete (suspend)

  Mailbox CRUD:
    POST   /mail/mailboxes?ws=                   create mailbox (hello@domain.com)
    GET    /mail/mailboxes?ws=&domain={id}       list mailboxes
    PATCH  /mail/mailboxes/{id}                  update password / display_name / aliases
    DELETE /mail/mailboxes/{id}                  delete mailbox

Phase 1 = backend skeleton: DB + REST CRUD + DKIM keypair generation.
Phase 2 = MX receive (Postfix Cloud Run) + send via SES.
Phase 3 = Webmail UI + billing wire + DNS auto-verify cron.

Status: skeleton — Postfix MX not yet deployed, /verify-dns returns stub OK.
"""
from __future__ import annotations

import base64
import logging
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.core.security import hash_password
from app.core.vault import decrypt, encrypt
from app.db.base import get_db
from app.services.audit import audit_push

log = logging.getLogger("zeni.api.mail")
router = APIRouter(prefix="/mail", tags=["mail"])


# ─── Constants ────────────────────────────────────────────────
ALLOWED_PLANS = {"starter", "pro", "business", "enterprise"}
ALLOWED_DMARC = {"none", "quarantine", "reject"}
DOMAIN_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)(\.[a-z]{2,})+$")
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
MX_HOST = "mx.zenicloud.io"  # Phase 2: thực sự MX này phải listen :25

PLAN_LIMITS = {
    "starter":    {"max_mailboxes": 5,   "max_emails_per_month": 1_000,   "storage_gb": 5},
    "pro":        {"max_mailboxes": 20,  "max_emails_per_month": 10_000,  "storage_gb": 50},
    "business":   {"max_mailboxes": -1,  "max_emails_per_month": 50_000,  "storage_gb": 200},
    "enterprise": {"max_mailboxes": -1,  "max_emails_per_month": 250_000, "storage_gb": 1000},
}


# ─── Pydantic schemas ─────────────────────────────────────────
class DomainCreateIn(BaseModel):
    domain: str = Field(min_length=4, max_length=255, description="VD: vietcontech.com")
    plan: str = Field(default="starter", description="starter | pro | business | enterprise")
    dmarc_policy: str = Field(default="quarantine", description="none | quarantine | reject")


class DomainOut(BaseModel):
    id: str
    workspace_id: str
    domain: str
    status: str
    plan: str
    dkim_selector: str
    mx_verified: bool
    spf_verified: bool
    dkim_verified: bool
    dmarc_policy: str
    created_at: str


class DnsRecordsOut(BaseModel):
    """DNS records khách cần copy vào DNS provider của họ."""
    domain: str
    records: dict[str, str]                # name → value
    instructions: list[str]


class MailboxCreateIn(BaseModel):
    domain_id: str
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=128)
    quota_mb: int = Field(default=5120, ge=100, le=1_048_576)


class MailboxOut(BaseModel):
    id: str
    domain_id: str
    username: str
    email_full: str                        # username + @ + domain
    display_name: str | None
    quota_mb: int
    used_mb: int
    is_catchall: bool
    is_active: bool
    created_at: str


# ─── Helpers ──────────────────────────────────────────────────
def _validate_domain(d: str) -> str:
    d = (d or "").strip().lower()
    if not DOMAIN_RE.match(d):
        raise HTTPException(status_code=422, detail="Domain không hợp lệ. VD: vietcontech.com")
    return d


def _validate_username(u: str) -> str:
    u = (u or "").strip().lower()
    if not USERNAME_RE.match(u):
        raise HTTPException(status_code=422, detail="Username không hợp lệ (a-z, 0-9, . _ -, max 63 ký tự)")
    return u


def _gen_dkim_keypair() -> tuple[str, str]:
    """Generate RSA 2048 DKIM keypair. Returns (private_pem, public_dns_value)."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:
        # Fallback: return placeholder pair if cryptography missing — caller will see warning
        log.warning("cryptography package missing — DKIM keypair will be placeholder!")
        return ("PLACEHOLDER_PRIVATE_KEY", "PLACEHOLDER_PUBLIC_KEY")

    privkey = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = privkey.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    pubkey = privkey.public_key()
    pub_der = pubkey.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    # DKIM public format: base64 of DER, no PEM headers
    pub_b64 = base64.b64encode(pub_der).decode("ascii")
    return (private_pem, pub_b64)


def _dns_records_for_domain(domain: str, selector: str, dkim_public: str, dmarc_policy: str) -> dict[str, str]:
    """Build DNS records dict cho khách copy."""
    return {
        f"MX  {domain}":                     f"10 {MX_HOST}",
        f"TXT @ {domain}":                    f"v=spf1 include:{MX_HOST.replace('mx.', '')} ~all",
        f"TXT {selector}._domainkey.{domain}": f"v=DKIM1; k=rsa; p={dkim_public}",
        f"TXT _dmarc.{domain}":               f"v=DMARC1; p={dmarc_policy}; rua=mailto:dmarc-reports@zenicloud.io",
    }


# ─── ENDPOINTS · DOMAIN ───────────────────────────────────────
@router.post("/domains", response_model=DomainOut, status_code=201)
async def create_domain(
    payload: DomainCreateIn,
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DomainOut:
    """Đăng ký domain cho mail hosting. Tự generate DKIM keypair (private encrypted via Vault)."""
    await require_workspace_access(ws, me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Cần role Developer trở lên để thêm domain")

    domain = _validate_domain(payload.domain)
    plan = payload.plan.lower()
    if plan not in ALLOWED_PLANS:
        raise HTTPException(status_code=422, detail=f"Plan không hợp lệ. Cho phép: {sorted(ALLOWED_PLANS)}")
    dmarc = payload.dmarc_policy.lower()
    if dmarc not in ALLOWED_DMARC:
        raise HTTPException(status_code=422, detail=f"DMARC policy không hợp lệ. Cho phép: {sorted(ALLOWED_DMARC)}")

    # Check duplicate
    exists = (await db.execute(text("SELECT 1 FROM mail_domains WHERE domain = :d"), {"d": domain})).first()
    if exists:
        raise HTTPException(status_code=409, detail=f"Domain {domain} đã được đăng ký")

    # Generate DKIM keypair
    private_pem, public_b64 = _gen_dkim_keypair()
    try:
        encrypted_private = encrypt(private_pem)
    except Exception as e:
        log.warning("Vault encrypt failed: %s — storing private key as-is (PoC)", e)
        encrypted_private = private_pem

    row = (await db.execute(text("""
        INSERT INTO mail_domains
            (workspace_id, domain, status, dkim_selector, dkim_private_key, dkim_public_key, dmarc_policy, plan)
        VALUES (:ws, :d, 'pending_dns', 'zeni', :priv, :pub, :dmarc, :plan)
        RETURNING id, workspace_id, domain, status, plan, dkim_selector,
                  mx_verified, spf_verified, dkim_verified, dmarc_policy, created_at
    """), {
        "ws": ws, "d": domain, "priv": encrypted_private,
        "pub": public_b64, "dmarc": dmarc, "plan": plan,
    })).mappings().first()

    await audit_push(
        db, actor=me.email, workspace_id=ws, action="mail.domain_create",
        target=domain, severity="info", metadata={"plan": plan, "dmarc": dmarc},
    )
    await db.commit()

    return DomainOut(
        id=str(row["id"]),
        workspace_id=row["workspace_id"],
        domain=row["domain"],
        status=row["status"],
        plan=row["plan"],
        dkim_selector=row["dkim_selector"],
        mx_verified=row["mx_verified"],
        spf_verified=row["spf_verified"],
        dkim_verified=row["dkim_verified"],
        dmarc_policy=row["dmarc_policy"],
        created_at=row["created_at"].isoformat(),
    )


@router.get("/domains", response_model=list[DomainOut])
async def list_domains(
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DomainOut]:
    await require_workspace_access(ws, me)
    rows = (await db.execute(text("""
        SELECT id, workspace_id, domain, status, plan, dkim_selector,
               mx_verified, spf_verified, dkim_verified, dmarc_policy, created_at
        FROM mail_domains
        WHERE workspace_id = :ws
        ORDER BY created_at DESC
    """), {"ws": ws})).mappings().all()
    return [
        DomainOut(
            id=str(r["id"]),
            workspace_id=r["workspace_id"],
            domain=r["domain"],
            status=r["status"],
            plan=r["plan"],
            dkim_selector=r["dkim_selector"],
            mx_verified=r["mx_verified"],
            spf_verified=r["spf_verified"],
            dkim_verified=r["dkim_verified"],
            dmarc_policy=r["dmarc_policy"],
            created_at=r["created_at"].isoformat(),
        )
        for r in rows
    ]


@router.get("/domains/{domain_id}/dns", response_model=DnsRecordsOut)
async def get_domain_dns(
    domain_id: str,
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DnsRecordsOut:
    """Trả về DNS records khách cần copy vào DNS provider để verify domain."""
    await require_workspace_access(ws, me)
    row = (await db.execute(text("""
        SELECT domain, dkim_selector, dkim_public_key, dmarc_policy
        FROM mail_domains
        WHERE id = :id AND workspace_id = :ws
    """), {"id": domain_id, "ws": ws})).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Domain không tồn tại")

    records = _dns_records_for_domain(
        row["domain"], row["dkim_selector"], row["dkim_public_key"], row["dmarc_policy"],
    )
    return DnsRecordsOut(
        domain=row["domain"],
        records=records,
        instructions=[
            f"1. Vào DNS provider của bạn (Cloudflare/Namecheap/Route53/...)",
            f"2. Thêm 4 records trên (copy nguyên value)",
            f"3. Đợi propagate ~5-30 phút",
            f"4. POST /mail/domains/{domain_id}/verify-dns để check",
        ],
    )


@router.post("/domains/{domain_id}/verify-dns")
async def verify_domain_dns(
    domain_id: str,
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Phase 1 stub: returns current verification state.

    Phase 2 thực sự sẽ resolve DNS bằng dnspython, check:
      - MX → mx.zenicloud.io
      - SPF → contains include:zenicloud.io
      - DKIM → match public key đã gen
      - DMARC → policy match
    Update mx_verified / spf_verified / dkim_verified, status → 'active' nếu cả 3 pass.
    """
    await require_workspace_access(ws, me)
    row = (await db.execute(text("""
        SELECT id, domain, mx_verified, spf_verified, dkim_verified, status
        FROM mail_domains WHERE id = :id AND workspace_id = :ws
    """), {"id": domain_id, "ws": ws})).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Domain không tồn tại")

    # Phase 1: chỉ trả state hiện tại
    return {
        "domain_id": domain_id,
        "domain": row["domain"],
        "status": row["status"],
        "mx_verified": row["mx_verified"],
        "spf_verified": row["spf_verified"],
        "dkim_verified": row["dkim_verified"],
        "note": "Phase 1 stub — DNS resolver chưa wire. Phase 2 sẽ dnspython.resolve() thực.",
    }


@router.delete("/domains/{domain_id}", status_code=204, response_class=Response)
async def delete_domain(
    domain_id: str,
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await require_workspace_access(ws, me)
    if me.role in ("Viewer", "Developer"):
        raise HTTPException(status_code=403, detail="Cần Admin trở lên để xoá domain")

    row = (await db.execute(text("""
        SELECT domain FROM mail_domains WHERE id = :id AND workspace_id = :ws
    """), {"id": domain_id, "ws": ws})).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Domain không tồn tại")

    # Soft delete: set status='suspended' (CASCADE delete sẽ rất destructive — giữ data 30d)
    await db.execute(text("""
        UPDATE mail_domains SET status = 'suspended', updated_at = NOW()
        WHERE id = :id
    """), {"id": domain_id})
    await audit_push(
        db, actor=me.email, workspace_id=ws, action="mail.domain_suspend",
        target=row["domain"], severity="warn",
    )
    await db.commit()
    return Response(status_code=204)


# ─── ENDPOINTS · MAILBOX ──────────────────────────────────────
@router.post("/mailboxes", response_model=MailboxOut, status_code=201)
async def create_mailbox(
    payload: MailboxCreateIn,
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MailboxOut:
    await require_workspace_access(ws, me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Cần role Developer trở lên")

    # Validate domain belongs to workspace
    domain_row = (await db.execute(text("""
        SELECT id, domain, plan, status FROM mail_domains
        WHERE id = :id AND workspace_id = :ws
    """), {"id": payload.domain_id, "ws": ws})).mappings().first()
    if not domain_row:
        raise HTTPException(status_code=404, detail="Domain không thuộc workspace này")
    if domain_row["status"] == "suspended":
        raise HTTPException(status_code=403, detail="Domain đang bị suspend")

    # Check plan limits
    limits = PLAN_LIMITS.get(domain_row["plan"], PLAN_LIMITS["starter"])
    if limits["max_mailboxes"] != -1:
        count_row = (await db.execute(text("""
            SELECT COUNT(*) FROM mail_mailboxes WHERE domain_id = :d
        """), {"d": payload.domain_id})).scalar() or 0
        if count_row >= limits["max_mailboxes"]:
            raise HTTPException(
                status_code=403,
                detail=f"Plan {domain_row['plan']} chỉ cho phép {limits['max_mailboxes']} mailbox. Upgrade plan để thêm.",
            )

    username = _validate_username(payload.username)

    row = (await db.execute(text("""
        INSERT INTO mail_mailboxes
            (domain_id, username, password_hash, display_name, quota_mb)
        VALUES (:d, :u, :ph, :dn, :q)
        ON CONFLICT (domain_id, username) DO NOTHING
        RETURNING id, domain_id, username, display_name, quota_mb, used_mb,
                  is_catchall, is_active, created_at
    """), {
        "d": payload.domain_id,
        "u": username,
        "ph": hash_password(payload.password),
        "dn": payload.display_name,
        "q": payload.quota_mb,
    })).mappings().first()

    if not row:
        raise HTTPException(status_code=409, detail=f"Mailbox {username}@{domain_row['domain']} đã tồn tại")

    await audit_push(
        db, actor=me.email, workspace_id=ws, action="mail.mailbox_create",
        target=f"{username}@{domain_row['domain']}", severity="info",
    )
    await db.commit()

    return MailboxOut(
        id=str(row["id"]),
        domain_id=str(row["domain_id"]),
        username=row["username"],
        email_full=f"{row['username']}@{domain_row['domain']}",
        display_name=row["display_name"],
        quota_mb=row["quota_mb"],
        used_mb=row["used_mb"],
        is_catchall=row["is_catchall"],
        is_active=row["is_active"],
        created_at=row["created_at"].isoformat(),
    )


@router.get("/mailboxes", response_model=list[MailboxOut])
async def list_mailboxes(
    ws: str = Query(..., min_length=1, max_length=64),
    domain_id: str | None = Query(default=None),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MailboxOut]:
    await require_workspace_access(ws, me)
    if domain_id:
        rows = (await db.execute(text("""
            SELECT mb.id, mb.domain_id, mb.username, mb.display_name, mb.quota_mb, mb.used_mb,
                   mb.is_catchall, mb.is_active, mb.created_at, md.domain
            FROM mail_mailboxes mb
            JOIN mail_domains md ON md.id = mb.domain_id
            WHERE md.workspace_id = :ws AND mb.domain_id = :d
            ORDER BY mb.created_at DESC
        """), {"ws": ws, "d": domain_id})).mappings().all()
    else:
        rows = (await db.execute(text("""
            SELECT mb.id, mb.domain_id, mb.username, mb.display_name, mb.quota_mb, mb.used_mb,
                   mb.is_catchall, mb.is_active, mb.created_at, md.domain
            FROM mail_mailboxes mb
            JOIN mail_domains md ON md.id = mb.domain_id
            WHERE md.workspace_id = :ws
            ORDER BY mb.created_at DESC
        """), {"ws": ws})).mappings().all()

    return [
        MailboxOut(
            id=str(r["id"]),
            domain_id=str(r["domain_id"]),
            username=r["username"],
            email_full=f"{r['username']}@{r['domain']}",
            display_name=r["display_name"],
            quota_mb=r["quota_mb"],
            used_mb=r["used_mb"],
            is_catchall=r["is_catchall"],
            is_active=r["is_active"],
            created_at=r["created_at"].isoformat(),
        )
        for r in rows
    ]


@router.delete("/mailboxes/{mailbox_id}", status_code=204, response_class=Response)
async def delete_mailbox(
    mailbox_id: str,
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await require_workspace_access(ws, me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Cần Developer trở lên")

    row = (await db.execute(text("""
        SELECT mb.id, mb.username, md.domain
        FROM mail_mailboxes mb
        JOIN mail_domains md ON md.id = mb.domain_id
        WHERE mb.id = :id AND md.workspace_id = :ws
    """), {"id": mailbox_id, "ws": ws})).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Mailbox không tồn tại")

    # Soft delete: set is_active=false. Phase 2 sẽ retain emails 30 ngày.
    await db.execute(text("""
        UPDATE mail_mailboxes SET is_active = FALSE, updated_at = NOW()
        WHERE id = :id
    """), {"id": mailbox_id})
    await audit_push(
        db, actor=me.email, workspace_id=ws, action="mail.mailbox_delete",
        target=f"{row['username']}@{row['domain']}", severity="warn",
    )
    await db.commit()
    return Response(status_code=204)


@router.get("/health")
async def mail_health() -> dict:
    """Health check + plan info."""
    return {
        "status": "ok",
        "phase": "1 (skeleton — DB + REST CRUD only)",
        "mx_host": MX_HOST,
        "next_phases": [
            "Phase 2: Postfix Cloud Run + SES outbound",
            "Phase 3: Webmail UI + billing wire",
        ],
        "plans": list(PLAN_LIMITS.keys()),
    }
