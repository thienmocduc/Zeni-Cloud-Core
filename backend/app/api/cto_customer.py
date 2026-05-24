"""
Zeni Cloud — CTO Portal (Customer-facing).

Khách hàng có workspace trên Zeni đăng nhập → chat với CTO AI để được hỗ trợ
deploy. Khác với /cto/sessions (Owner-only, admin của Zeni), endpoint này phục
vụ KHÁCH HÀNG.

Endpoints:
  POST   /api/v1/cto/customer/chat              — gửi message, nhận AI reply
  GET    /api/v1/cto/customer/sessions          — list sessions của customer
  POST   /api/v1/cto/customer/sessions          — tạo session mới
  GET    /api/v1/cto/customer/sessions/{id}     — chi tiết + messages
  POST   /api/v1/cto/customer/deploy-assist     — request hướng dẫn deploy
  GET    /api/v1/cto/customer/lock-status       — check workspace có bị lock không
  GET    /api/v1/cto/customer/charter-status    — public charter integrity check
  POST   /api/v1/cto/admin/unlock               — Chairman unlock workspace
  GET    /api/v1/cto/admin/violations           — Chairman view violations
  GET    /api/v1/cto/admin/locks                — Chairman view active locks

Security model (defense in depth):
  Layer A: require_workspace_access — customer chỉ truy cập ws họ là member
  Layer B: CtoAutoLock.check — chặn nếu ws/IP đang bị lock
  Layer C: CtoInputFilter — chặn jailbreak/secret-extract/cross-tenant/oos
  Layer D: CHARTER_LOCK system prompt → LLM (DeepSeek V4 Pro / Claude / mock)
  Layer E: OutputFilter — chặn leak PII/cross-tenant/suspicious phrase
  Layer F: Audit log mọi request, mọi violation, mọi lock

Rate limit (basic in-memory; production nên Redis):
  - 30 messages / phút / workspace
  - 200 messages / ngày / workspace
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push
from app.services.cto_auto_lock import CtoAutoLock, LockStatus
from app.services.cto_charter import (
    CHARTER_VERSION,
    CharterTamperError,
    charter_status as _charter_status,
    get_charter_prompt,
)
from app.services.cto_input_filter import CtoInputFilter, FilterDecision
from app.services.llm_gateway import run_inference
from app.services.output_filter import OutputFilter

log = logging.getLogger("zeni.cto_customer")
router = APIRouter(prefix="/cto", tags=["cto-customer"])


# ─────────────────────────────────────────────────────────────
# Singletons (init once)
# ─────────────────────────────────────────────────────────────
_input_filter = CtoInputFilter()
_output_filter = OutputFilter()

# Models allowed for customer CTO (chairman lock — daily driver DeepSeek)
ALLOWED_MODELS = {
    "deepseek-chat",        # V4 Pro — default
    "deepseek-flash",       # V4 Flash — quick
    "deepseek-reasoner",    # R1 — deep dive
    "claude-haiku-4-5",     # fallback
}
DEFAULT_MODEL = "deepseek-chat"

MAX_MESSAGE_LEN = 16_000      # before filter trims
MAX_HISTORY_MSGS = 8           # last 8 messages from this session
MAX_RESPONSE_TOKENS = 1500


# ─────────────────────────────────────────────────────────────
# Rate limiter (in-memory; replace with Redis in production)
# ─────────────────────────────────────────────────────────────
_rl_lock = asyncio.Lock()
_rl_minute: dict[str, deque[float]] = defaultdict(deque)
_rl_day: dict[str, deque[float]] = defaultdict(deque)
RL_PER_MIN = 30
RL_PER_DAY = 200


async def _rate_limit_check(workspace_id: str) -> tuple[bool, str]:
    """Returns (ok, reason). Sliding window."""
    now = time.time()
    async with _rl_lock:
        m = _rl_minute[workspace_id]
        while m and now - m[0] > 60:
            m.popleft()
        if len(m) >= RL_PER_MIN:
            return False, f"vượt {RL_PER_MIN} message/phút — chờ 1 phút"
        d = _rl_day[workspace_id]
        while d and now - d[0] > 86_400:
            d.popleft()
        if len(d) >= RL_PER_DAY:
            return False, f"vượt {RL_PER_DAY} message/ngày — chờ qua ngày"
        m.append(now)
        d.append(now)
    return True, ""


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def _user_agent(request: Request) -> str:
    return (request.headers.get("user-agent") or "")[:256]


# ─── Cache other-workspace-names (per ws_id, TTL 5 min) ────
_other_ws_cache: dict[str, tuple[float, list[str]]] = {}
_OTHER_WS_TTL = 300.0  # 5 minutes


async def _get_other_workspace_names(db: AsyncSession, my_ws: str) -> list[str]:
    """List name của workspaces KHÁC để filter cross-tenant. Cached 5min."""
    now = time.time()
    cached = _other_ws_cache.get(my_ws)
    if cached and now - cached[0] < _OTHER_WS_TTL:
        return cached[1]
    try:
        rows = (await db.execute(text(
            "SELECT name FROM workspaces WHERE id != :me LIMIT 200"
        ), {"me": my_ws})).all()
        names = [r[0] for r in rows if r[0] and len(r[0]) >= 4]
        _other_ws_cache[my_ws] = (now, names)
        # Prune cache occasionally
        if len(_other_ws_cache) > 500:
            for k in list(_other_ws_cache.keys())[:200]:
                _other_ws_cache.pop(k, None)
        return names
    except Exception:
        return cached[1] if cached else []


async def _ensure_session(
    db: AsyncSession,
    workspace_id: str,
    user: CurrentUser,
    session_id: Optional[str],
    title_hint: str,
    project_id: Optional[str],
    model: str,
) -> str:
    """Get-or-create customer_cto_session. Returns session_id (uuid string)."""
    if session_id:
        # Verify ownership
        row = (await db.execute(text(
            "SELECT id::text, workspace_id, user_id::text FROM customer_cto_sessions "
            "WHERE id = :id AND workspace_id = :ws"
        ), {"id": session_id, "ws": workspace_id})).mappings().first()
        if not row:
            raise HTTPException(404, "Session không tồn tại trong workspace của anh")
        # Allow any workspace member to continue session (not just creator)
        return row["id"]

    sid = uuid.uuid4()
    await db.execute(text("""
        INSERT INTO customer_cto_sessions
            (id, workspace_id, user_id, title, status, project_id, model)
        VALUES
            (:id, :ws, :uid, :t, 'open', :pid, :m)
    """), {
        "id": str(sid),
        "ws": workspace_id,
        "uid": str(user.id),
        "t": title_hint[:255] if title_hint else "Hỗ trợ deploy",
        "pid": project_id,
        "m": model,
    })
    await db.commit()
    return str(sid)


async def _load_history(db: AsyncSession, session_id: str, workspace_id: str, limit: int) -> list[dict]:
    """Load last N messages from session (oldest first for LLM context). Workspace-scoped defense-in-depth."""
    rows = (await db.execute(text("""
        SELECT sender_type, COALESCE(content_filtered, content) AS content
        FROM customer_cto_messages
        WHERE session_id = :sid AND workspace_id = :ws
        ORDER BY created_at DESC
        LIMIT :lim
    """), {"sid": session_id, "ws": workspace_id, "lim": limit})).mappings().all()
    return list(reversed([{"sender": r["sender_type"], "content": r["content"]} for r in rows]))


def _build_user_prompt(history: list[dict], current_message: str) -> str:
    """Compose multi-turn prompt as plain text — LLM gateway hiện single-turn."""
    parts: list[str] = []
    for h in history:
        role = "Customer" if h["sender"] == "customer" else "CTO AI"
        parts.append(f"{role}: {h['content']}")
    parts.append(f"Customer: {current_message}")
    parts.append("CTO AI:")
    return "\n\n".join(parts)


async def _store_message(
    db: AsyncSession,
    session_id: str,
    workspace_id: str,
    sender_type: str,
    content: str,
    content_filtered: Optional[str] = None,
    filter_warnings: Optional[list[str]] = None,
    model: Optional[str] = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    latency_ms: int = 0,
) -> str:
    mid = uuid.uuid4()
    await db.execute(text("""
        INSERT INTO customer_cto_messages
            (id, session_id, workspace_id, sender_type, content, content_filtered,
             filter_warnings, model, input_tokens, output_tokens, cost_usd, latency_ms)
        VALUES
            (:id, :sid, :ws, :st, :c, :cf, :fw, :m, :it, :ot, :cost, :lat)
    """), {
        "id": str(mid), "sid": session_id, "ws": workspace_id,
        "st": sender_type, "c": content[:64000],
        "cf": (content_filtered or "")[:64000] if content_filtered is not None else None,
        "fw": json.dumps(filter_warnings or []),
        "m": model, "it": input_tokens, "ot": output_tokens,
        "cost": cost_usd, "lat": latency_ms,
    })
    await db.execute(text("""
        UPDATE customer_cto_sessions
        SET message_count = message_count + 1, last_message_at = NOW()
        WHERE id = :sid
    """), {"sid": session_id})
    await db.commit()
    return str(mid)


# ═════════════════════════════════════════════════════════════
# Schemas
# ═════════════════════════════════════════════════════════════
class ChatIn(BaseModel):
    workspace_id: str = Field(..., max_length=64)
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LEN)
    session_id: Optional[str] = Field(None, max_length=64)
    project_id: Optional[str] = Field(None, max_length=64)
    model: str = Field(DEFAULT_MODEL, max_length=64)


class ChatOut(BaseModel):
    session_id: str
    message_id: str
    reply: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    filter_warnings: list[str] = []


class DeployAssistIn(BaseModel):
    workspace_id: str = Field(..., max_length=64)
    project_id: Optional[str] = Field(None, max_length=64)
    stack: Optional[str] = Field(None, max_length=80, description="next.js | django | fastapi | go | rust | ...")
    issue: str = Field(..., min_length=10, max_length=4000, description="Mô tả vấn đề deploy")
    error_log: Optional[str] = Field(None, max_length=8000)


class SessionOut(BaseModel):
    id: str
    workspace_id: str
    title: str
    status: str
    message_count: int
    project_id: Optional[str]
    model: str
    created_at: str
    last_message_at: Optional[str] = None


class MessageOut(BaseModel):
    id: str
    sender_type: str
    content: str
    model: Optional[str] = None
    created_at: str


# ═════════════════════════════════════════════════════════════
# Public — health/charter status (no auth)
# ═════════════════════════════════════════════════════════════
@router.get("/customer/charter-status")
async def get_charter_status_endpoint() -> dict:
    """Public — kiểm tra Charter integrity. Không lộ prompt."""
    return _charter_status()


# ═════════════════════════════════════════════════════════════
# Customer — lock status
# ═════════════════════════════════════════════════════════════
@router.get("/customer/lock-status")
async def get_lock_status(
    workspace_id: str = Query(..., max_length=64),
    request: Request = None,  # type: ignore
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(workspace_id, me)
    ip = _client_ip(request) if request else None
    status = await CtoAutoLock.check(db, workspace_id, ip)
    return {
        "locked": status.locked,
        "workspace_id": status.workspace_id,
        "unlock_at": status.unlock_at.isoformat() if status.unlock_at else None,
        "severity": status.severity,
        "reason": status.reason,
    }


# ═════════════════════════════════════════════════════════════
# Customer — sessions list/create/detail
# ═════════════════════════════════════════════════════════════
@router.get("/customer/sessions", response_model=list[SessionOut])
async def list_customer_sessions(
    workspace_id: str = Query(..., max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SessionOut]:
    await require_workspace_access(workspace_id, me)
    rows = (await db.execute(text("""
        SELECT id::text AS id, workspace_id, title, status, message_count,
               project_id, model, created_at::text AS created_at,
               last_message_at::text AS last_message_at
        FROM customer_cto_sessions
        WHERE workspace_id = :ws
        ORDER BY last_message_at DESC NULLS LAST, created_at DESC
        LIMIT 100
    """), {"ws": workspace_id})).mappings().all()
    return [SessionOut(**dict(r)) for r in rows]


@router.get("/customer/sessions/{session_id}/messages", response_model=list[MessageOut])
async def list_session_messages(
    session_id: str,
    workspace_id: str = Query(..., max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MessageOut]:
    await require_workspace_access(workspace_id, me)
    # Verify ownership
    own = (await db.execute(text(
        "SELECT id FROM customer_cto_sessions WHERE id = :id AND workspace_id = :ws"
    ), {"id": session_id, "ws": workspace_id})).first()
    if not own:
        raise HTTPException(404, "Session không thuộc workspace của anh")
    rows = (await db.execute(text("""
        SELECT id::text AS id, sender_type,
               COALESCE(content_filtered, content) AS content,
               model, created_at::text AS created_at
        FROM customer_cto_messages
        WHERE session_id = :sid
        ORDER BY created_at ASC
        LIMIT 500
    """), {"sid": session_id})).mappings().all()
    return [MessageOut(**dict(r)) for r in rows]


# ═════════════════════════════════════════════════════════════
# Customer — main chat endpoint
# ═════════════════════════════════════════════════════════════
@router.post("/customer/chat", response_model=ChatOut)
async def customer_chat(
    body: ChatIn,
    request: Request,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatOut:
    """Customer chat với CTO AI. Full security pipeline."""
    # Layer A: workspace access
    await require_workspace_access(body.workspace_id, me)
    ip = _client_ip(request)
    ua = _user_agent(request)

    # Layer B: lock check
    lock = await CtoAutoLock.check(db, body.workspace_id, ip)
    if lock.locked:
        raise HTTPException(
            status_code=423,
            detail={
                "error": "workspace_locked",
                "reason": lock.reason or "Workspace tạm khóa do vi phạm bảo mật",
                "unlock_at": lock.unlock_at.isoformat() if lock.unlock_at else None,
                "severity": lock.severity,
            },
        )

    # Rate limit
    ok, rl_reason = await _rate_limit_check(body.workspace_id)
    if not ok:
        raise HTTPException(429, f"Rate limit: {rl_reason}")

    # Model whitelist
    model = body.model if body.model in ALLOWED_MODELS else DEFAULT_MODEL

    # Charter (raises if tampered)
    try:
        system_prompt = get_charter_prompt(
            workspace_id=body.workspace_id,
            customer_email=me.email,
        )
    except CharterTamperError as e:
        log.critical("[cto-customer] CHARTER TAMPER detected: %s", e)
        try:
            await audit_push(
                db, actor="system:cto", workspace_id=body.workspace_id,
                action="cto.charter_tamper", target="charter",
                severity="critical", metadata={"error": str(e)},
            )
            await db.commit()
        except Exception:
            await db.rollback()
        raise HTTPException(503, "CTO AI tạm dừng (charter integrity). Đã báo Chairman.")

    # Layer C: input filter
    other_names = await _get_other_workspace_names(db, body.workspace_id)
    decision = _input_filter.analyze(body.message, body.workspace_id, other_names)

    if decision.action == "block":
        # Log violation
        await CtoInputFilter.log_violation(
            db, body.workspace_id, me.id, body.session_id,
            decision, ip_address=ip, user_agent=ua,
            excerpt=body.message[:300],
        )
        # Evaluate auto-lock
        applied = await CtoAutoLock.evaluate_and_lock(db, body.workspace_id, ip)
        if applied:
            log.warning(
                "[cto-customer] auto-locked ws=%s sev=%s reason=%s",
                body.workspace_id, applied.severity, applied.reason,
            )

        # Ensure session for stable UX
        sid = await _ensure_session(
            db, body.workspace_id, me, body.session_id,
            title_hint=body.message[:80], project_id=body.project_id, model=model,
        )
        # Store customer raw + AI refusal
        await _store_message(
            db, sid, body.workspace_id, "customer", body.message,
            filter_warnings=decision.reasons,
        )
        refusal = decision.refusal_message or "Yêu cầu vi phạm chính sách bảo mật của Zeni Cloud."
        mid = await _store_message(
            db, sid, body.workspace_id, "cto_ai", refusal,
            content_filtered=refusal, filter_warnings=["blocked_by_input_filter"],
            model=model,
        )
        return ChatOut(
            session_id=sid, message_id=mid, reply=refusal,
            model=model, input_tokens=0, output_tokens=0,
            cost_usd=0.0, latency_ms=0,
            filter_warnings=["input_blocked"] + decision.matched_patterns[:5],
        )

    # If sanitize: use sanitized input
    user_message = decision.sanitized_input or body.message

    # Session
    sid = await _ensure_session(
        db, body.workspace_id, me, body.session_id,
        title_hint=user_message[:80], project_id=body.project_id, model=model,
    )

    # Store customer message
    await _store_message(
        db, sid, body.workspace_id, "customer", body.message,
        content_filtered=(user_message if decision.action == "sanitize" else None),
        filter_warnings=decision.reasons if decision.reasons else None,
    )

    # Load history (workspace-scoped)
    history = await _load_history(db, sid, body.workspace_id, MAX_HISTORY_MSGS)
    # Note: history vừa thêm message customer → bỏ message cuối nếu trùng
    if history and history[-1].get("sender") == "customer":
        history = history[:-1]
    user_prompt = _build_user_prompt(history, user_message)

    # Layer D: call LLM
    start = time.perf_counter()
    try:
        result = await run_inference(
            model=model,
            prompt=user_prompt,
            system=system_prompt,
            temperature=0.4,    # bảo thủ — không quá creative
            max_tokens=MAX_RESPONSE_TOKENS,
        )
        reply_raw = result.output or ""
        latency = result.latency_ms
        in_tok, out_tok = result.input_tokens, result.output_tokens
        cost = result.cost_usd
    except Exception as e:
        log.exception("[cto-customer] LLM error: %s", e)
        reply_raw = "Tôi đang gặp sự cố kỹ thuật khi xử lý. Anh thử lại sau 1-2 phút nhé."
        latency = int((time.perf_counter() - start) * 1000)
        in_tok = out_tok = 0
        cost = 0.0

    # Layer E: output filter
    reply_filtered, warnings = await _output_filter.filter(
        response=reply_raw,
        user_workspace_id=body.workspace_id,
        agent_name=f"cto_ai_{model}",
        db=db, user_id=me.id,
    )

    # Store AI message
    mid = await _store_message(
        db, sid, body.workspace_id, "cto_ai",
        content=reply_raw, content_filtered=reply_filtered,
        filter_warnings=warnings, model=model,
        input_tokens=in_tok, output_tokens=out_tok,
        cost_usd=cost, latency_ms=latency,
    )

    # Audit — KHÔNG log full message content (PII risk), chỉ metadata
    try:
        await audit_push(
            db, actor=f"user:{me.id}", workspace_id=body.workspace_id,
            action="cto.customer_chat", target=sid, severity="info",
            metadata={
                "model": model, "in_tok": in_tok, "out_tok": out_tok,
                "cost_usd": float(cost), "latency_ms": latency,
                "warnings": (warnings or [])[:5],  # cap warnings list
                "msg_len": len(body.message),
                "reply_len": len(reply_filtered or ""),
            },
        )
        await db.commit()
    except Exception:
        await db.rollback()

    return ChatOut(
        session_id=sid, message_id=mid,
        reply=reply_filtered, model=model,
        input_tokens=in_tok, output_tokens=out_tok,
        cost_usd=float(cost), latency_ms=latency,
        filter_warnings=warnings,
    )


# ═════════════════════════════════════════════════════════════
# Customer — deploy assist (specialized chat)
# ═════════════════════════════════════════════════════════════
@router.post("/customer/deploy-assist", response_model=ChatOut)
async def deploy_assist(
    body: DeployAssistIn,
    request: Request,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatOut:
    """
    Wrapper trên /customer/chat: tổng hợp stack + issue + error_log thành 1 message
    có cấu trúc rõ ràng → AI dễ diagnose hơn.
    """
    composed = "**Deploy Assistance Request**\n\n"
    if body.stack:
        composed += f"- Stack: `{body.stack}`\n"
    if body.project_id:
        composed += f"- Project ID: `{body.project_id}`\n"
    composed += f"\n**Vấn đề:**\n{body.issue}\n"
    if body.error_log:
        composed += f"\n**Error log:**\n```\n{body.error_log[:6000]}\n```\n"
    composed += "\nVui lòng diagnose và đề xuất giải pháp cụ thể trong scope Zeni Cloud."

    chat_body = ChatIn(
        workspace_id=body.workspace_id,
        message=composed,
        project_id=body.project_id,
        model=DEFAULT_MODEL,
    )
    return await customer_chat(chat_body, request, me, db)  # reuse pipeline


# ═════════════════════════════════════════════════════════════
# Admin (Owner) — view violations / locks / manual unlock
# ═════════════════════════════════════════════════════════════
def _require_owner(me: CurrentUser) -> None:
    if me.role != "Owner":
        raise HTTPException(403, "Chỉ Owner truy cập được")


@router.get("/admin/violations")
async def admin_list_violations(
    workspace_id: Optional[str] = Query(None, max_length=64),
    severity: Optional[str] = Query(None),
    hours: int = Query(24, ge=1, le=720),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    _require_owner(me)
    sql = """
      SELECT id::text, workspace_id, user_id::text, session_id, action, severity,
             reasons, matched_patterns, ip_address, user_agent, excerpt,
             created_at::text
      FROM cto_security_violations
      WHERE created_at > NOW() - (:h || ' hours')::interval
    """
    params: dict[str, Any] = {"h": str(hours)}
    if workspace_id:
        sql += " AND workspace_id = :ws"
        params["ws"] = workspace_id
    if severity:
        sql += " AND severity = :sev"
        params["sev"] = severity
    sql += " ORDER BY created_at DESC LIMIT 500"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


@router.get("/admin/locks")
async def admin_list_locks(
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    _require_owner(me)
    rows = (await db.execute(text("""
        SELECT id::text, workspace_id, ip_address, locked_at::text AS locked_at,
               unlock_at::text AS unlock_at, severity, reason
        FROM cto_workspace_locks
        WHERE unlock_at > NOW()
        ORDER BY unlock_at DESC
        LIMIT 200
    """))).mappings().all()
    return [dict(r) for r in rows]


class UnlockIn(BaseModel):
    workspace_id: str = Field(..., max_length=64)
    reason: Optional[str] = Field(None, max_length=500)


@router.post("/admin/unlock")
async def admin_unlock(
    body: UnlockIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_owner(me)
    ok = await CtoAutoLock.manual_unlock(
        db, body.workspace_id, str(me.id),
        reason=body.reason or "Chairman manual unlock",
    )
    return {"unlocked": ok, "workspace_id": body.workspace_id}


__all__ = ["router"]
t db.execute(text("""
        SELECT id::text, workspace_id, ip_address, locked_at::text AS locked_at,
               unlock_at::text AS unlock_at, severity, reason
        FROM cto_workspace_locks
        WHERE unlock_at > NOW()
        ORDER BY unlock_at DESC
        LIMIT 200
    """))).mappings().all()
    return [dict(r) for r in rows]


class UnlockIn(BaseModel):
    workspace_id: str = Field(..., max_length=64)
    reason: Optional[str] = Field(None, max_length=500)


@router.post("/admin/unlock")
async def admin_unlock(
    body: UnlockIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_owner(me)
    ok = await CtoAutoLock.manual_unlock(
        db, body.workspace_id, str(me.id),
        reason=body.reason or "Chairman manual unlock",
    )
    return {"unlocked": ok, "workspace_id": body.workspace_id}


__all__ = ["router"]
