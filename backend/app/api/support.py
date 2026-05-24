"""
Zeni Cloud Support — Chat workspace ↔ chairman/agent + AI auto-reply.

Pattern lấy cảm hứng từ ClawWits coder.* + Intercom + Claude Code:
- Khách Owner workspace tạo session → chat với chairman/agent
- AI agent (Claude/Gemini) auto-reply task simple (FAQ, hướng dẫn)
- Task phức tạp / proposed action (deploy, secret, billing) → escalate
  → chairman approve trước khi agent execute
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db, SessionLocal

log = logging.getLogger("zeni.support")
router = APIRouter(prefix="/support", tags=["support"])


# ═════════ SCHEMAS ═════════
class SessionCreate(BaseModel):
    title: str = Field(..., max_length=255)
    category: Optional[str] = Field("deploy", max_length=40)
    priority: str = Field("normal", description="low|normal|high|urgent")
    initial_message: Optional[str] = None


class SessionOut(BaseModel):
    id: str
    workspace_id: str
    title: str
    status: str
    priority: str
    category: Optional[str]
    message_count: int
    last_message_at: Optional[str]
    created_at: str


class MessageCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=10000)
    content_format: str = Field("markdown")
    attachments: list[dict] = Field(default_factory=list)
    request_ai_reply: bool = Field(True, description="Trigger AI agent auto-reply")


class MessageOut(BaseModel):
    id: str
    session_id: str
    sender_type: str
    sender_name: Optional[str]
    content: str
    content_format: str
    ai_model: Optional[str] = None
    ai_confidence: Optional[float] = None
    proposed_action: Optional[dict] = None
    action_status: Optional[str] = None
    attachments: list = Field(default_factory=list)
    created_at: str


class ApproveActionIn(BaseModel):
    notes: Optional[str] = None


# ═════════ ENDPOINTS — Customer side ═════════
@router.post("/sessions", response_model=SessionOut, status_code=201)
async def create_session(
    body: SessionCreate,
    bg: BackgroundTasks,
    ws: str = Query(..., description="workspace_id"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SessionOut:
    """Tạo support session mới. Owner/Admin của workspace mới được tạo."""
    await require_workspace_access(ws, me)
    if me.role not in ("Owner", "Admin", "Developer"):
        raise HTTPException(403, "Cần Developer/Admin/Owner để tạo support ticket")

    sid = uuid.uuid4()
    if body.priority not in ("low", "normal", "high", "urgent"):
        body.priority = "normal"

    await db.execute(text(
        "INSERT INTO support_sessions (id, workspace_id, customer_user_id, title, "
        "category, priority, status, message_count) "
        "VALUES (:id, :ws, :uid, :t, :cat, :pri, 'open', 0)"
    ), {
        "id": str(sid), "ws": ws, "uid": str(me.id),
        "t": body.title, "cat": body.category, "pri": body.priority,
    })

    if body.initial_message:
        msg_id = uuid.uuid4()
        await db.execute(text(
            "INSERT INTO support_messages (id, session_id, workspace_id, sender_type, "
            "sender_user_id, sender_name, content, content_format) "
            "VALUES (:id, :sid, :ws, 'customer', :uid, :name, :c, 'markdown')"
        ), {
            "id": str(msg_id), "sid": str(sid), "ws": ws,
            "uid": str(me.id), "name": me.email,
            "c": body.initial_message,
        })
        await db.execute(text(
            "UPDATE support_sessions SET message_count=1, last_message_at=NOW() WHERE id=:id"
        ), {"id": str(sid)})

    await db.commit()

    # Background: AI agent auto-reply (FAQ retrieval first, fallback Claude)
    if body.initial_message:
        bg.add_task(_agent_auto_reply, str(sid), ws, body.initial_message)

    row = (await db.execute(text(
        "SELECT id::text, workspace_id, title, status, priority, category, message_count, "
        "last_message_at::text, created_at::text FROM support_sessions WHERE id = :id"
    ), {"id": str(sid)})).mappings().first()
    return SessionOut(**dict(row))


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(
    ws: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SessionOut]:
    """List sessions. Customer thấy của workspace mình. Owner role chairman thấy tất cả.

    Security:
    - ws specified → require_workspace_access enforces tenant boundary
    - ws None + Owner → super-admin view (chairman thấy tất cả workspaces)
    - ws None + non-Owner → restrict to user's joined workspaces (NEVER leak cross-tenant)
    """
    sql = (
        "SELECT id::text, workspace_id, title, status, priority, category, "
        "message_count, last_message_at::text, created_at::text FROM support_sessions"
    )
    params: dict[str, Any] = {}
    where = []
    if ws:
        await require_workspace_access(ws, me)
        where.append("workspace_id = :ws")
        params["ws"] = ws
    elif me.role != "Owner":
        # Non-Owner without ws filter — restrict to user's joined workspaces only
        ws_list = list(getattr(me, "workspaces", []) or [])
        if not ws_list:
            return []
        where.append("workspace_id = ANY(:ws_list)")
        params["ws_list"] = ws_list
    # Owner role + no ws → see all (chairman super-admin view)
    if status:
        where.append("status = :st")
        params["st"] = status
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY last_message_at DESC NULLS LAST LIMIT 200"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return [SessionOut(**dict(r)) for r in rows]


@router.get("/sessions/{session_id}/messages", response_model=list[MessageOut])
async def list_messages(
    session_id: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MessageOut]:
    """Get all messages in session."""
    sess = (await db.execute(text(
        "SELECT workspace_id FROM support_sessions WHERE id = :id"
    ), {"id": session_id})).mappings().first()
    if not sess:
        raise HTTPException(404, "Session not found")
    await require_workspace_access(sess["workspace_id"], me)

    rows = (await db.execute(text(
        "SELECT id::text, session_id::text, sender_type, sender_name, content, content_format, "
        "ai_model, ai_confidence, proposed_action, action_status, attachments, created_at::text "
        "FROM support_messages WHERE session_id = :sid ORDER BY created_at ASC LIMIT 500"
    ), {"sid": session_id})).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["proposed_action"] = d["proposed_action"] if isinstance(d["proposed_action"], dict) else (json.loads(d["proposed_action"]) if d["proposed_action"] else None)
        d["attachments"] = d["attachments"] if isinstance(d["attachments"], list) else (json.loads(d["attachments"]) if d["attachments"] else [])
        out.append(MessageOut(**d))
    return out


@router.post("/sessions/{session_id}/messages", response_model=MessageOut, status_code=201)
async def send_message(
    session_id: str,
    body: MessageCreate,
    bg: BackgroundTasks,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    """Send message. Customer hoặc chairman đều dùng endpoint này."""
    sess = (await db.execute(text(
        "SELECT workspace_id, status FROM support_sessions WHERE id = :id"
    ), {"id": session_id})).mappings().first()
    if not sess:
        raise HTTPException(404, "Session not found")
    await require_workspace_access(sess["workspace_id"], me)

    sender_type = "chairman" if me.role == "Owner" else "customer"
    msg_id = uuid.uuid4()

    await db.execute(text(
        "INSERT INTO support_messages (id, session_id, workspace_id, sender_type, "
        "sender_user_id, sender_name, content, content_format, attachments) "
        "VALUES (:id, :sid, :ws, :stype, :uid, :name, :c, :cf, CAST(:att AS jsonb))"
    ), {
        "id": str(msg_id), "sid": session_id, "ws": sess["workspace_id"],
        "stype": sender_type, "uid": str(me.id), "name": me.email,
        "c": body.content, "cf": body.content_format,
        "att": json.dumps(body.attachments),
    })
    await db.execute(text(
        "UPDATE support_sessions SET message_count = message_count + 1, "
        "last_message_at = NOW() WHERE id = :id"
    ), {"id": session_id})
    await db.commit()

    # AI auto-reply only when customer asks (not chairman)
    if sender_type == "customer" and body.request_ai_reply:
        bg.add_task(_agent_auto_reply, session_id, sess["workspace_id"], body.content)

    return MessageOut(
        id=str(msg_id), session_id=session_id,
        sender_type=sender_type, sender_name=me.email,
        content=body.content, content_format=body.content_format,
        attachments=body.attachments,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


@router.post("/sessions/{session_id}/resolve", status_code=200)
async def resolve_session(
    session_id: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark session resolved (chairman or customer)."""
    sess = (await db.execute(text(
        "SELECT workspace_id FROM support_sessions WHERE id = :id"
    ), {"id": session_id})).mappings().first()
    if not sess:
        raise HTTPException(404, "Session not found")
    await require_workspace_access(sess["workspace_id"], me)
    await db.execute(text(
        "UPDATE support_sessions SET status = 'resolved', resolved_at = NOW() WHERE id = :id"
    ), {"id": session_id})
    await db.commit()
    return {"status": "resolved"}


@router.post("/messages/{msg_id}/approve-action", status_code=200)
async def approve_action(
    msg_id: str,
    body: ApproveActionIn,
    bg: BackgroundTasks,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Chairman approve proposed_action → agent execute."""
    if me.role != "Owner":
        raise HTTPException(403, "Chỉ Owner workspace duyệt action (human-in-the-loop)")
    msg = (await db.execute(text(
        "SELECT session_id::text, workspace_id, proposed_action, action_status "
        "FROM support_messages WHERE id = :id"
    ), {"id": msg_id})).mappings().first()
    if not msg or msg["action_status"] != "proposed":
        raise HTTPException(404, "Action not pending")
    await require_workspace_access(msg["workspace_id"], me)

    await db.execute(text(
        "UPDATE support_messages SET action_status='approved', "
        "action_executed_by = :uid WHERE id = :id"
    ), {"uid": str(me.id), "id": msg_id})
    await db.commit()
    bg.add_task(_execute_action, msg_id)
    return {"status": "approved", "execution_queued": True}


# ═════════ AI Agent helpers (background) ═════════
async def _agent_auto_reply(session_id: str, workspace_id: str, user_content: str) -> None:
    """Pattern: FAQ retrieval first → Claude fallback. Save AI message to DB."""
    try:
        async with SessionLocal() as db:
            # Step 1: keyword match against support_kb
            kb_match = (await db.execute(text(
                "SELECT category, question, answer_markdown FROM support_kb "
                "WHERE EXISTS (SELECT 1 FROM jsonb_array_elements_text(keywords) AS kw "
                "              WHERE LOWER(:msg) LIKE '%' || LOWER(kw) || '%') "
                "ORDER BY use_count DESC LIMIT 1"
            ), {"msg": user_content})).mappings().first()

            if kb_match:
                content = (
                    f"**FAQ match — {kb_match['category']}:**\n\n"
                    f"_{kb_match['question']}_\n\n{kb_match['answer_markdown']}\n\n"
                    f"---\n_Reply tự động từ KB. Cần thêm hỗ trợ → tag chairman bằng `@cto`._"
                )
                await _save_agent_message(db, session_id, workspace_id, content,
                                          ai_model="zeni-kb-retrieval",
                                          ai_confidence=0.95)
                # increment KB use_count
                await db.execute(text(
                    "UPDATE support_kb SET use_count = use_count + 1 "
                    "WHERE question = :q"
                ), {"q": kb_match["question"]})
                await db.commit()
                return

            # Step 2: Fallback LLM Gateway (anthropic / openai / gemini / mock)
            try:
                from app.services.llm_gateway import run_inference
                system_prompt = (
                    "Bạn là Zeni Cloud Support agent — trả lời ngắn gọn (≤200 từ), "
                    "tiếng Việt, dùng markdown. Nếu task cần execute (deploy/billing/secret) — "
                    "hỏi user thêm detail. Không tự execute action chưa duyệt. "
                    "Nếu không chắc → bảo user 'tag @cto'."
                )
                # Default cheap Claude Haiku, fall back to mock if API key not set
                result = await run_inference(
                    model="claude-3-5-haiku-20241022",
                    prompt=user_content,
                    system=system_prompt,
                    temperature=0.4,
                    max_tokens=512,
                )
                ai_content = (result.output or "").strip()
                if not ai_content:
                    ai_content = "Em chưa hiểu rõ task. Có thể bạn mô tả chi tiết hơn? Hoặc tag `@cto` để chairman hỗ trợ trực tiếp."
                await _save_agent_message(db, session_id, workspace_id, ai_content,
                                          ai_model=result.model,
                                          ai_confidence=0.7,
                                          ai_tokens_used=(result.input_tokens + result.output_tokens))
            except Exception as e:
                log.warning("AI fallback unavailable: %s", e)
                fallback = (
                    "Xin chào, em là Zeni Support agent. Task của bạn cần CTO duyệt. "
                    "Tag `@cto` trong message tiếp theo để chairman hỗ trợ trực tiếp."
                )
                await _save_agent_message(db, session_id, workspace_id, fallback,
                                          ai_model="zeni-fallback",
                                          ai_confidence=0.3)
            await db.commit()
    except Exception as e:
        log.exception("[support] _agent_auto_reply crash: %s", e)


async def _save_agent_message(db, session_id: str, ws: str, content: str,
                              ai_model: str = "", ai_confidence: float = 0.5,
                              ai_tokens_used: int = 0) -> None:
    msg_id = uuid.uuid4()
    await db.execute(text(
        "INSERT INTO support_messages (id, session_id, workspace_id, sender_type, "
        "sender_name, content, content_format, ai_model, ai_confidence, ai_tokens_used) "
        "VALUES (:id, :sid, :ws, 'agent', 'Zeni Agent', :c, 'markdown', :m, :conf, :tok)"
    ), {
        "id": str(msg_id), "sid": session_id, "ws": ws,
        "c": content, "m": ai_model,
        "conf": ai_confidence, "tok": ai_tokens_used,
    })
    await db.execute(text(
        "UPDATE support_sessions SET message_count = message_count + 1, "
        "last_message_at = NOW() WHERE id = :id"
    ), {"id": session_id})


async def _execute_action(msg_id: str) -> None:
    """Execute proposed_action sau khi chairman approve. Phase 2 — chỉ stub."""
    log.info("[support] execute action %s — Phase 2 stub (Edge Sandbox integration pending)", msg_id)
    async with SessionLocal() as db:
        await db.execute(text(
            "UPDATE support_messages SET action_status='executed', action_executed_at=NOW(), "
            "action_result = CAST(:r AS jsonb) WHERE id = :id"
        ), {"r": json.dumps({"status": "stub", "note": "Phase 2 wiring pending"}), "id": msg_id})
        await db.commit()
