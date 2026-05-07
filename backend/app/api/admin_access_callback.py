"""
Zeni Cloud Core — Privacy: Admin Access HMAC Callback (public).

Public endpoints linked from admin support emails (sent by another agent's
`admin_access.py` module). The customer clicks "Approve" or "Deny" in their
inbox; that link lands here, the HMAC is verified, and the corresponding
`admin_access_requests` row is updated.

Endpoints:
  GET /privacy/approve/{request_id}/approve?token=...
  GET /privacy/approve/{request_id}/deny?token=...

Token format:
  HMAC-SHA256(secret, str(request_id))[:32]   (hex, lowercased)

Configuration:
  ZENI_APPROVAL_TOKEN_SECRET — random ≥32 char secret
  ZENI_BASE_URL              — public origin used in redirects

Security:
  - HMAC verified with constant-time `hmac.compare_digest`.
  - Idempotent: a row already approved/denied returns the matching redirect
    instead of erroring out (so duplicate clicks from email clients are safe).
  - Audit pushed for both decisions; metadata includes IP hint.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text as _sql
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings  # noqa: F401
from app.db.base import get_db
from app.services.audit import audit_push

log = logging.getLogger("zeni.api.admin_access_callback")
router = APIRouter(prefix="/privacy/approve", tags=["privacy", "auth"])

# ── Config helpers ────────────────────────────────────────────
_RUNTIME_SECRET_FALLBACK: str | None = None  # generated once if env missing


def _get_secret() -> str:
    """
    Resolve token secret in priority order:
      1. settings.zeni_approval_token_secret  (if Settings adds it later)
      2. env var ZENI_APPROVAL_TOKEN_SECRET
      3. ephemeral in-process random (logs warning) — survives only this boot
    """
    global _RUNTIME_SECRET_FALLBACK
    val = getattr(settings, "zeni_approval_token_secret", None) or \
          os.environ.get("ZENI_APPROVAL_TOKEN_SECRET", "")
    if val and len(val) >= 16:
        return val
    if _RUNTIME_SECRET_FALLBACK is None:
        _RUNTIME_SECRET_FALLBACK = secrets.token_hex(32)
        log.warning(
            "[admin_access_callback] ZENI_APPROVAL_TOKEN_SECRET not set — "
            "generated ephemeral secret for this process. "
            "Approval links will NOT survive restart. Configure in production!"
        )
    return _RUNTIME_SECRET_FALLBACK


def _base_url() -> str:
    val = getattr(settings, "zeni_base_url", None) or os.environ.get("ZENI_BASE_URL", "")
    return (val or "https://zenicloud.io").rstrip("/")


def make_approval_token(request_id: int, secret: str | None = None) -> str:
    """HMAC-SHA256(secret, str(request_id)) hex, truncated to 32 chars."""
    sec = secret or _get_secret()
    return hmac.new(
        sec.encode("utf-8"), str(request_id).encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]


def _verify_token(request_id: int, supplied: str) -> bool:
    expected = make_approval_token(request_id)
    if not supplied or len(supplied) != len(expected):
        return False
    return hmac.compare_digest(expected, supplied.lower())


def _client_ip(request: Request) -> str:
    return request.headers.get("x-forwarded-for", "").split(",")[0].strip() or \
           (request.client.host if request.client else "unknown")


# ─────────────────────────────────────────────────────────────
# 1. GET /privacy/approve/{request_id}/approve?token=...
# ─────────────────────────────────────────────────────────────
@router.get("/{request_id}/approve")
async def approve_via_email(
    request: Request,
    request_id: int = Path(ge=1),
    token: str = Query(min_length=8, max_length=128),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Approve admin access via signed email link. Idempotent."""
    base = _base_url()
    ip = _client_ip(request)

    if not _verify_token(request_id, token):
        await audit_push(
            db, actor=f"email_callback:{ip[:32]}", workspace_id=None,
            action="privacy.admin_access.callback.bad_token",
            target=str(request_id), severity="warn",
            metadata={"action": "approve", "ip_hint": ip[:64]},
        )
        await db.commit()
        return RedirectResponse(
            url=f"{base}/app#privacy-approve-error=invalid_token",
            status_code=302,
        )

    row = (await db.execute(_sql("""
        SELECT id, customer_workspace_id, duration_seconds, status
          FROM admin_access_requests WHERE id = :id
    """), {"id": request_id})).mappings().first()

    if row is None:
        return RedirectResponse(
            url=f"{base}/app#privacy-approve-error=not_found",
            status_code=302,
        )

    # Idempotent — already-approved row → redirect with confirmation
    if row["status"] == "approved":
        return RedirectResponse(
            url=f"{base}/app#privacy-approved&request_id={request_id}",
            status_code=302,
        )
    if row["status"] in ("revoked", "expired"):
        return RedirectResponse(
            url=f"{base}/app#privacy-approve-error=already_{row['status']}",
            status_code=302,
        )

    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=int(row["duration_seconds"]))

    await db.execute(_sql("""
        UPDATE admin_access_requests
           SET status = 'approved', approved_at = :now, expires_at = :exp
         WHERE id = :id
    """), {"now": now, "exp": expires, "id": request_id})

    await audit_push(
        db, actor=f"email_callback:{ip[:32]}",
        workspace_id=row["customer_workspace_id"],
        action="privacy.admin_access.approve.email",
        target=str(request_id), severity="warn",
        metadata={"expires_at": expires.isoformat(), "ip_hint": ip[:64]},
    )
    await db.commit()

    return RedirectResponse(
        url=f"{base}/app#privacy-approved&request_id={request_id}",
        status_code=302,
    )


# ─────────────────────────────────────────────────────────────
# 2. GET /privacy/approve/{request_id}/deny?token=...
# ─────────────────────────────────────────────────────────────
@router.get("/{request_id}/deny")
async def deny_via_email(
    request: Request,
    request_id: int = Path(ge=1),
    token: str = Query(min_length=8, max_length=128),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Deny admin access via signed email link → marks status=revoked. Idempotent."""
    base = _base_url()
    ip = _client_ip(request)

    if not _verify_token(request_id, token):
        await audit_push(
            db, actor=f"email_callback:{ip[:32]}", workspace_id=None,
            action="privacy.admin_access.callback.bad_token",
            target=str(request_id), severity="warn",
            metadata={"action": "deny", "ip_hint": ip[:64]},
        )
        await db.commit()
        return RedirectResponse(
            url=f"{base}/app#privacy-deny-error=invalid_token",
            status_code=302,
        )

    row = (await db.execute(_sql("""
        SELECT id, customer_workspace_id, status
          FROM admin_access_requests WHERE id = :id
    """), {"id": request_id})).mappings().first()

    if row is None:
        return RedirectResponse(
            url=f"{base}/app#privacy-deny-error=not_found",
            status_code=302,
        )

    if row["status"] == "revoked":
        return RedirectResponse(
            url=f"{base}/app#privacy-denied&request_id={request_id}",
            status_code=302,
        )
    if row["status"] in ("approved", "expired"):
        # Allow deny to override approved (revoke the active grant)
        if row["status"] == "expired":
            return RedirectResponse(
                url=f"{base}/app#privacy-deny-error=already_expired",
                status_code=302,
            )

    now = datetime.now(timezone.utc)
    await db.execute(_sql("""
        UPDATE admin_access_requests
           SET status = 'revoked', revoked_at = :now
         WHERE id = :id
    """), {"now": now, "id": request_id})

    await audit_push(
        db, actor=f"email_callback:{ip[:32]}",
        workspace_id=row["customer_workspace_id"],
        action="privacy.admin_access.deny.email",
        target=str(request_id), severity="warn",
        metadata={"prev_status": row["status"], "ip_hint": ip[:64]},
    )
    await db.commit()

    return RedirectResponse(
        url=f"{base}/app#privacy-denied&request_id={request_id}",
        status_code=302,
    )
