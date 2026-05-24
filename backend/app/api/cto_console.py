"""
Zeni Cloud — CTO Console API.

Pattern: Claude Code-style 3-tab workspace cho CTO operations
  Tab 1 — Chat Support     : reuse support_sessions (session_type='support')
  Tab 2 — Provisioning     : chat + AI agent propose tools (PAT/whitelist/template)
  Tab 3 — Auto Coder       : load source khách + agent propose code edit + deploy

Triết lý "không phụ thuộc Claude Code":
- AI agent dùng Anthropic tool use (qua llm_gateway)
- Mỗi tool = 1 endpoint backend Zeni đã có
- Mọi tool call có risk_level — safe/low auto-execute, medium+ phải chairman duyệt
- Audit immutable cho cross-tenant action

Authorization: CHỈ Owner (chairman + CTO) mới truy cập được.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user
from app.db.base import get_db, SessionLocal
from app.services.audit import audit_push

log = logging.getLogger("zeni.cto_console")
router = APIRouter(prefix="/cto", tags=["cto-console"])


# ═════════════════════════════════════════════════
# RBAC — Owner-only enforcement
# ═════════════════════════════════════════════════
def _require_cto(me: CurrentUser) -> None:
    if me.role != "Owner":
        raise HTTPException(403, "CTO Console chỉ dành cho Owner role (chairman/CTO)")


# ═════════════════════════════════════════════════
# SCHEMAS
# ═════════════════════════════════════════════════
class CtoSessionCreate(BaseModel):
    title: str = Field(..., max_length=255)
    session_type: str = Field("support", description="support|provisioning|coder")
    target_workspace: Optional[str] = Field(None, description="Workspace KHÁCH cần CTO support")
    target_project_id: Optional[str] = Field(None)
    initial_message: Optional[str] = None


class CtoSessionOut(BaseModel):
    id: str
    session_type: str
    title: str
    status: str
    target_workspace: Optional[str] = None
    target_project_id: Optional[str] = None
    message_count: int
    last_message_at: Optional[str] = None
    created_at: str


class ToolCallProposeIn(BaseModel):
    session_id: str
    tool_name: str = Field(..., max_length=80)
    tool_args: dict = Field(default_factory=dict)
    proposed_by_agent: str = Field("manual", max_length=80)
    target_workspace: Optional[str] = None


class ToolCallOut(BaseModel):
    id: str
    session_id: str
    tool_name: str
    tool_args: dict
    status: str
    risk_level: str
    proposed_at: str
    approved_at: Optional[str] = None
    executed_at: Optional[str] = None
    execution_result: Optional[dict] = None
    error_detail: Optional[str] = None
    target_workspace: Optional[str] = None


class ToolCallApproveIn(BaseModel):
    notes: Optional[str] = None


# ═════════════════════════════════════════════════
# TAB 1+2+3 — Sessions list/create
# ═════════════════════════════════════════════════
@router.get("/sessions", response_model=list[CtoSessionOut])
async def list_cto_sessions(
    session_type: Optional[str] = Query(None, description="support|provisioning|coder"),
    status: Optional[str] = Query(None),
    target_workspace: Optional[str] = Query(None),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CtoSessionOut]:
    """List CTO sessions (Owner-only)."""
    _require_cto(me)
    sql = (
        "SELECT id::text, COALESCE(session_type,'support') AS session_type, title, status, "
        "target_workspace, target_project_id, message_count, "
        "last_message_at::text, created_at::text "
        "FROM support_sessions"
    )
    where = []
    params: dict[str, Any] = {}
    if session_type:
        where.append("COALESCE(session_type,'support') = :st")
        params["st"] = session_type
    if status:
        where.append("status = :stt")
        params["stt"] = status
    if target_workspace:
        where.append("target_workspace = :tws")
        params["tws"] = target_workspace
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY last_message_at DESC NULLS LAST LIMIT 200"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return [CtoSessionOut(**dict(r)) for r in rows]


@router.post("/sessions", response_model=CtoSessionOut, status_code=201)
async def create_cto_session(
    body: CtoSessionCreate,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CtoSessionOut:
    """Tạo CTO session (Owner-only)."""
    _require_cto(me)
    if body.session_type not in ("support", "provisioning", "coder"):
        raise HTTPException(400, "session_type phải là support|provisioning|coder")
    sid = uuid.uuid4()
    # workspace_id của session = target_workspace (khách) hoặc 'cto-internal'
    ws_id = body.target_workspace or "cto-internal"
    await db.execute(text(
        "INSERT INTO support_sessions (id, workspace_id, customer_user_id, title, "
        "category, priority, status, message_count, session_type, "
        "target_workspace, target_project_id) "
        "VALUES (:id, :ws, :uid, :t, :cat, 'normal', 'open', 0, :stype, :tws, :tpid)"
    ), {
        "id": str(sid), "ws": ws_id, "uid": str(me.id),
        "t": body.title, "cat": body.session_type,
        "stype": body.session_type,
        "tws": body.target_workspace, "tpid": body.target_project_id,
    })
    if body.initial_message:
        msg_id = uuid.uuid4()
        await db.execute(text(
            "INSERT INTO support_messages (id, session_id, workspace_id, sender_type, "
            "sender_user_id, sender_name, content, content_format) "
            "VALUES (:id, :sid, :ws, 'chairman', :uid, :name, :c, 'markdown')"
        ), {
            "id": str(msg_id), "sid": str(sid), "ws": ws_id,
            "uid": str(me.id), "name": me.email, "c": body.initial_message,
        })
        await db.execute(text(
            "UPDATE support_sessions SET message_count=1, last_message_at=NOW() WHERE id=:id"
        ), {"id": str(sid)})
    await db.commit()
    row = (await db.execute(text(
        "SELECT id::text, session_type, title, status, target_workspace, target_project_id, "
        "message_count, last_message_at::text, created_at::text "
        "FROM support_sessions WHERE id = :id"
    ), {"id": str(sid)})).mappings().first()
    return CtoSessionOut(**dict(row))


# ═════════════════════════════════════════════════
# Tool call — propose / list / approve / reject
# ═════════════════════════════════════════════════
@router.post("/tool-calls/propose", response_model=ToolCallOut, status_code=201)
async def propose_tool_call(
    body: ToolCallProposeIn,
    bg: BackgroundTasks,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ToolCallOut:
    """Propose AI tool call. Auto-execute nếu safe/low; medium+ → wait approval."""
    _require_cto(me)
    # Lookup risk_level từ policy
    pol = (await db.execute(text(
        "SELECT risk_level, enabled FROM cto_tool_policy WHERE tool_name = :n"
    ), {"n": body.tool_name})).mappings().first()
    if not pol:
        raise HTTPException(400, f"Tool '{body.tool_name}' không có trong policy. Add vào cto_tool_policy trước.")
    if not pol["enabled"]:
        raise HTTPException(403, f"Tool '{body.tool_name}' đã disable trong policy")

    risk = pol["risk_level"]
    auto_exec = risk in ("safe", "low")
    initial_status = "approved" if auto_exec else "proposed"

    tcid = uuid.uuid4()
    await db.execute(text(
        "INSERT INTO cto_agent_tool_calls (id, session_id, tool_name, tool_args, "
        "status, proposed_by_agent, target_workspace, risk_level, "
        "approved_by, approved_at) "
        "VALUES (:id, :sid, :tn, CAST(:ta AS jsonb), :st, :ag, :tws, :rl, "
        ":apb, CASE WHEN :st='approved' THEN NOW() ELSE NULL END)"
    ), {
        "id": str(tcid), "sid": body.session_id,
        "tn": body.tool_name, "ta": json.dumps(body.tool_args),
        "st": initial_status, "ag": body.proposed_by_agent,
        "tws": body.target_workspace, "rl": risk,
        "apb": str(me.id) if auto_exec else None,
    })
    await db.commit()

    # Auto-execute safe/low tools immediately
    if auto_exec:
        bg.add_task(_execute_tool_call, str(tcid))

    row = (await db.execute(text(
        "SELECT id::text, session_id::text, tool_name, tool_args, status, risk_level, "
        "proposed_at::text, approved_at::text, executed_at::text, "
        "execution_result, error_detail, target_workspace "
        "FROM cto_agent_tool_calls WHERE id = :id"
    ), {"id": str(tcid)})).mappings().first()
    d = dict(row)
    d["tool_args"] = d["tool_args"] if isinstance(d["tool_args"], dict) else (json.loads(d["tool_args"]) if d["tool_args"] else {})
    d["execution_result"] = d["execution_result"] if isinstance(d["execution_result"], dict) else (json.loads(d["execution_result"]) if d["execution_result"] else None)
    return ToolCallOut(**d)


@router.get("/tool-calls", response_model=list[ToolCallOut])
async def list_tool_calls(
    session_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None, description="proposed|approved|executed|failed"),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ToolCallOut]:
    _require_cto(me)
    sql = (
        "SELECT id::text, session_id::text, tool_name, tool_args, status, risk_level, "
        "proposed_at::text, approved_at::text, executed_at::text, "
        "execution_result, error_detail, target_workspace "
        "FROM cto_agent_tool_calls"
    )
    where = []
    params: dict[str, Any] = {}
    if session_id:
        where.append("session_id = :sid")
        params["sid"] = session_id
    if status:
        where.append("status = :st")
        params["st"] = status
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY proposed_at DESC LIMIT 200"
    rows = (await db.execute(text(sql), params)).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["tool_args"] = d["tool_args"] if isinstance(d["tool_args"], dict) else (json.loads(d["tool_args"]) if d["tool_args"] else {})
        d["execution_result"] = d["execution_result"] if isinstance(d["execution_result"], dict) else (json.loads(d["execution_result"]) if d["execution_result"] else None)
        out.append(ToolCallOut(**d))
    return out


@router.post("/tool-calls/{tcid}/approve", response_model=ToolCallOut)
async def approve_tool_call(
    tcid: str,
    body: ToolCallApproveIn,
    bg: BackgroundTasks,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ToolCallOut:
    """Chairman approve proposed tool call → background execute."""
    _require_cto(me)
    row = (await db.execute(text(
        "SELECT status, risk_level, target_workspace FROM cto_agent_tool_calls WHERE id = :id"
    ), {"id": tcid})).mappings().first()
    if not row:
        raise HTTPException(404, "Tool call not found")
    if row["status"] != "proposed":
        raise HTTPException(409, f"Tool call status = '{row['status']}', không thể approve")

    await db.execute(text(
        "UPDATE cto_agent_tool_calls SET status='approved', approved_by=:by, approved_at=NOW() "
        "WHERE id = :id"
    ), {"by": str(me.id), "id": tcid})
    await db.commit()
    bg.add_task(_execute_tool_call, tcid)

    out = (await db.execute(text(
        "SELECT id::text, session_id::text, tool_name, tool_args, status, risk_level, "
        "proposed_at::text, approved_at::text, executed_at::text, "
        "execution_result, error_detail, target_workspace "
        "FROM cto_agent_tool_calls WHERE id = :id"
    ), {"id": tcid})).mappings().first()
    d = dict(out)
    d["tool_args"] = d["tool_args"] if isinstance(d["tool_args"], dict) else (json.loads(d["tool_args"]) if d["tool_args"] else {})
    d["execution_result"] = d["execution_result"] if isinstance(d["execution_result"], dict) else (json.loads(d["execution_result"]) if d["execution_result"] else None)
    return ToolCallOut(**d)


@router.post("/tool-calls/{tcid}/reject", status_code=200)
async def reject_tool_call(
    tcid: str,
    body: ToolCallApproveIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_cto(me)
    await db.execute(text(
        "UPDATE cto_agent_tool_calls SET status='rejected', rejected_reason=:r, "
        "approved_by=:by, approved_at=NOW() WHERE id = :id AND status='proposed'"
    ), {"r": body.notes or "Rejected by chairman", "by": str(me.id), "id": tcid})
    await db.commit()
    return {"status": "rejected"}


# ═════════════════════════════════════════════════
# Tool policy — list (cho UI render risk badge)
# ═════════════════════════════════════════════════
@router.get("/tools", response_model=list[dict])
async def list_tool_policy(
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_cto(me)
    rows = (await db.execute(text(
        "SELECT tool_name, risk_level, description, endpoint_path, enabled "
        "FROM cto_tool_policy WHERE enabled = TRUE ORDER BY risk_level, tool_name"
    ))).mappings().all()
    return [dict(r) for r in rows]


# ═════════════════════════════════════════════════
# TAB 3 — Project files (Auto Coder)
# ═════════════════════════════════════════════════
class FileSnapshotIn(BaseModel):
    session_id: str
    workspace_id: str
    project_id: Optional[str] = None
    file_path: str = Field(..., max_length=500)
    content: str = Field(..., max_length=204800, description="Max 200KB per file")
    language: Optional[str] = Field(None, max_length=40)


@router.post("/files/snapshot", status_code=201)
async def snapshot_file(
    body: FileSnapshotIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """CTO upload file content vào snapshot — agent đọc + propose edit."""
    _require_cto(me)
    fid = uuid.uuid4()
    await db.execute(text(
        "INSERT INTO cto_project_file_snapshots (id, session_id, workspace_id, project_id, "
        "file_path, content, content_size, language, fetched_from) "
        "VALUES (:id, :sid, :ws, :pid, :p, :c, :sz, :l, 'manual_paste')"
    ), {
        "id": str(fid), "sid": body.session_id, "ws": body.workspace_id,
        "pid": body.project_id, "p": body.file_path, "c": body.content,
        "sz": len(body.content), "l": body.language,
    })
    await db.commit()
    return {"id": str(fid), "size": len(body.content)}


@router.get("/files")
async def list_files(
    session_id: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_cto(me)
    rows = (await db.execute(text(
        "SELECT id::text, workspace_id, project_id, file_path, content_size, language, "
        "fetched_from, fetched_at::text "
        "FROM cto_project_file_snapshots WHERE session_id = :sid "
        "AND expires_at > NOW() ORDER BY fetched_at DESC LIMIT 100"
    ), {"sid": session_id})).mappings().all()
    return [dict(r) for r in rows]


@router.get("/files/{file_id}")
async def get_file(
    file_id: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_cto(me)
    row = (await db.execute(text(
        "SELECT id::text, workspace_id, project_id, file_path, content, content_size, "
        "language, fetched_from, fetched_at::text "
        "FROM cto_project_file_snapshots WHERE id = :id AND expires_at > NOW()"
    ), {"id": file_id})).mappings().first()
    if not row:
        raise HTTPException(404, "File snapshot expired hoặc không tồn tại")
    return dict(row)


# ═════════════════════════════════════════════════
# Tool execution (background)
# ═════════════════════════════════════════════════
TOOL_EXECUTORS: dict[str, str] = {
    # tool_name → handler function name (in this module)
    "list_workspaces":       "_exec_list_workspaces",
    "list_projects":         "_exec_list_projects",
    "get_project_logs":      "_exec_get_project_logs",
    "list_kb_faq":           "_exec_list_kb_faq",
    "send_message":          "_exec_send_message",
    "create_kb_entry":       "_exec_create_kb_entry",
    "create_pat":            "_exec_create_pat",
    "add_image_whitelist":   "_exec_add_whitelist",
    "create_project":        "_exec_create_project",
    "rotate_secret":         "_exec_rotate_secret",
    "deploy_canary":         "_exec_deploy_canary",
    "promote_traffic":       "_exec_promote_traffic",
    "trigger_build":         "_exec_trigger_build",
    "delete_project":        "_exec_delete_project",
    "delete_secret":         "_exec_delete_secret",
    "rollback_deploy":       "_exec_rollback_deploy",
}


async def _execute_tool_call(tcid: str) -> None:
    """Background executor — run tool + log result."""
    start = time.perf_counter()
    try:
        async with SessionLocal() as db:
            row = (await db.execute(text(
                "SELECT tool_name, tool_args, target_workspace FROM cto_agent_tool_calls "
                "WHERE id = :id AND status = 'approved'"
            ), {"id": tcid})).mappings().first()
            if not row:
                log.warning("[cto] tool_call %s not found or not approved", tcid)
                return

            await db.execute(text(
                "UPDATE cto_agent_tool_calls SET status='executing' WHERE id = :id"
            ), {"id": tcid})
            await db.commit()

            tool_name = row["tool_name"]
            args = row["tool_args"] if isinstance(row["tool_args"], dict) else json.loads(row["tool_args"] or "{}")
            target_ws = row["target_workspace"]

            handler_name = TOOL_EXECUTORS.get(tool_name)
            if not handler_name:
                raise RuntimeError(f"No executor wired for tool '{tool_name}'")
            handler = globals().get(handler_name)
            if handler is None:
                raise RuntimeError(f"Executor '{handler_name}' not implemented")

            result = await handler(db, args, target_ws)
            duration = int((time.perf_counter() - start) * 1000)

            await db.execute(text(
                "UPDATE cto_agent_tool_calls SET status='executed', executed_at=NOW(), "
                "execution_result = CAST(:r AS jsonb), execution_duration_ms = :d "
                "WHERE id = :id"
            ), {"r": json.dumps(result, default=str), "d": duration, "id": tcid})
            await db.commit()
            log.info("[cto] tool_call %s (%s) executed in %dms", tcid, tool_name, duration)
    except Exception as e:
        log.exception("[cto] tool_call %s failed: %s", tcid, e)
        try:
            async with SessionLocal() as db:
                await db.execute(text(
                    "UPDATE cto_agent_tool_calls SET status='failed', error_detail=:err, "
                    "executed_at=NOW() WHERE id = :id"
                ), {"err": str(e)[:1000], "id": tcid})
                await db.commit()
        except Exception:
            pass


# ═════════════════════════════════════════════════
# Tool implementations — wire to existing Zeni APIs
# ═════════════════════════════════════════════════
async def _exec_list_workspaces(db, args, target_ws):
    rows = (await db.execute(text(
        "SELECT id, name, created_at::text FROM workspaces ORDER BY created_at DESC LIMIT 200"
    ))).mappings().all()
    return {"workspaces": [dict(r) for r in rows]}


async def _exec_list_projects(db, args, target_ws):
    ws = args.get("workspace") or target_ws
    if not ws:
        raise ValueError("Tool 'list_projects' cần arg 'workspace'")
    rows = (await db.execute(text(
        "SELECT id::text, name, region, runtime, status, image, created_at::text "
        "FROM projects WHERE workspace_id = :ws ORDER BY created_at DESC LIMIT 200"
    ), {"ws": ws})).mappings().all()
    return {"workspace": ws, "projects": [dict(r) for r in rows]}


async def _exec_get_project_logs(db, args, target_ws):
    pid = args.get("project_id")
    return {"project_id": pid, "note": "Logs streaming — call gcloud logs read directly hoặc qua Observability API"}


async def _exec_list_kb_faq(db, args, target_ws):
    q = (args.get("query") or "").lower()
    if q:
        rows = (await db.execute(text(
            "SELECT category, question, answer_markdown FROM support_kb "
            "WHERE LOWER(question) LIKE :q OR LOWER(answer_markdown) LIKE :q "
            "ORDER BY use_count DESC LIMIT 10"
        ), {"q": f"%{q}%"})).mappings().all()
    else:
        rows = (await db.execute(text(
            "SELECT category, question, answer_markdown FROM support_kb ORDER BY use_count DESC LIMIT 20"
        ))).mappings().all()
    return {"results": [dict(r) for r in rows]}


async def _exec_send_message(db, args, target_ws):
    sid = args.get("session_id")
    content = args.get("content", "")
    if not sid or not content:
        raise ValueError("send_message cần session_id + content")
    msg_id = uuid.uuid4()
    sess = (await db.execute(text(
        "SELECT workspace_id FROM support_sessions WHERE id = :id"
    ), {"id": sid})).mappings().first()
    if not sess:
        raise ValueError(f"Session {sid} không tồn tại")
    await db.execute(text(
        "INSERT INTO support_messages (id, session_id, workspace_id, sender_type, "
        "sender_name, content, content_format) "
        "VALUES (:id, :sid, :ws, 'agent', 'CTO Console Agent', :c, 'markdown')"
    ), {"id": str(msg_id), "sid": sid, "ws": sess["workspace_id"], "c": content})
    await db.execute(text(
        "UPDATE support_sessions SET message_count = message_count + 1, last_message_at = NOW() "
        "WHERE id = :id"
    ), {"id": sid})
    return {"message_id": str(msg_id), "status": "sent"}


async def _exec_create_kb_entry(db, args, target_ws):
    cat = args.get("category", "general")
    q = args.get("question", "")
    a = args.get("answer", "")
    keywords = args.get("keywords", [])
    if not q or not a:
        raise ValueError("create_kb_entry cần question + answer")
    kid = uuid.uuid4()
    await db.execute(text(
        "INSERT INTO support_kb (id, category, question, answer_markdown, keywords) "
        "VALUES (:id, :c, :q, :a, CAST(:kw AS jsonb))"
    ), {"id": str(kid), "c": cat, "q": q, "a": a, "kw": json.dumps(keywords)})
    return {"id": str(kid), "category": cat}


async def _exec_create_pat(db, args, target_ws):
    """Create PAT for customer workspace (medium risk — needs approval)."""
    import hashlib, secrets as _secrets
    ws = args.get("workspace") or target_ws
    name = args.get("name", "CTO-provisioned PAT")
    scopes = args.get("scopes", "deploy,read")
    if not ws:
        raise ValueError("create_pat cần workspace")
    rand = _secrets.token_urlsafe(32)
    full = f"zeni_pat_{rand}"
    h = hashlib.sha256(full.encode()).hexdigest()
    prefix = full[:16] + "…"
    tid = uuid.uuid4()
    await db.execute(text(
        "INSERT INTO api_tokens (id, workspace_id, name, token_hash, token_prefix, "
        "scopes, created_by, revoked) "
        "VALUES (:id, :ws, :n, :h, :p, :s, NULL, FALSE)"
    ), {"id": str(tid), "ws": ws, "n": name, "h": h, "p": prefix, "s": scopes})
    return {"id": str(tid), "workspace": ws, "name": name, "scopes": scopes,
            "token": full, "warning": "TOKEN HIỂN THỊ 1 LẦN — copy ngay"}


async def _exec_add_whitelist(db, args, target_ws):
    ws = args.get("workspace") or target_ws
    prefix = args.get("prefix", "").lower().strip()
    desc = args.get("description")
    if not ws or not prefix:
        raise ValueError("add_image_whitelist cần workspace + prefix")
    if not prefix.endswith("/"):
        prefix += "/"
    wid = uuid.uuid4()
    await db.execute(text(
        "INSERT INTO workspace_image_whitelist (id, workspace_id, prefix, description, enabled) "
        "VALUES (:id, :ws, :p, :d, TRUE) ON CONFLICT DO NOTHING"
    ), {"id": str(wid), "ws": ws, "p": prefix, "d": desc})
    return {"workspace": ws, "prefix": prefix}


def _enforce_target_ws(args_ws: str | None, target_ws: str | None, tool: str) -> str:
    """Cross-tenant safety: if target_workspace is set on the tool call, args.workspace
    must match (or be omitted). Prevents an agent from overriding the approved target.
    """
    if target_ws and args_ws and args_ws != target_ws:
        raise ValueError(
            f"{tool}: cross-tenant violation — args.workspace='{args_ws}' "
            f"≠ target_workspace='{target_ws}' (chairman approved cho target_workspace)"
        )
    return args_ws or target_ws or ""


async def _exec_create_project(db, args, target_ws):
    """Create project DB row + (optional) trigger background Cloud Run deploy.

    Args: {workspace, name, image, region?, size?, port?, env_vars?, secrets?,
           runtime?, type?, deploy?: bool=True, allow_unauth?: bool=True}
    """
    from sqlalchemy import select as _select
    from app.db.models import Project
    from app.api.projects import (
        _validate_image_with_workspace,
        _bg_deploy,
        SIZE_DISPLAY,
        MAX_PROJECTS_PER_WS,
    )
    from app.services.cloud_run import SIZE_TO_RESOURCES, service_name_for

    ws = _enforce_target_ws(args.get("workspace"), target_ws, "create_project")
    name = (args.get("name") or "").strip().lower()
    image = (args.get("image") or "").strip()
    if not ws or not name or not image:
        raise ValueError("create_project cần workspace + name + image")

    region = args.get("region") or "asia-southeast1"
    size = args.get("size") or "s"
    if size not in SIZE_TO_RESOURCES:
        raise ValueError(f"create_project: size '{size}' không hợp lệ — phải là {list(SIZE_TO_RESOURCES.keys())}")
    port = int(args.get("port") or 8080)
    env_vars = args.get("env_vars") or {}
    secrets_map = args.get("secrets") or {}
    runtime = args.get("runtime") or "container"
    proj_type = args.get("type") or "web"
    do_deploy = bool(args.get("deploy", True))
    allow_unauth = bool(args.get("allow_unauth", True))

    if not isinstance(env_vars, dict) or not isinstance(secrets_map, dict):
        raise ValueError("create_project: env_vars + secrets phải là dict")

    # Image whitelist (global + per-workspace) — same gate as POST /projects
    try:
        await _validate_image_with_workspace(db, ws, image)
    except HTTPException as he:
        raise ValueError(f"create_project image rejected: {he.detail}") from he

    # Cap per workspace
    ws_count = (await db.execute(
        _select(Project).where(Project.workspace_id == ws)
    )).all()
    if len(ws_count) >= MAX_PROJECTS_PER_WS:
        raise ValueError(f"Workspace '{ws}' đã đạt giới hạn {MAX_PROJECTS_PER_WS} projects")

    # Upsert project row
    existing = (await db.execute(
        _select(Project).where(Project.workspace_id == ws, Project.name == name)
    )).scalar_one_or_none()

    cpu_display, mem_display, unit_cost = SIZE_DISPLAY[size]
    resources = SIZE_TO_RESOURCES[size]
    action = "compute.redeploy" if existing else "compute.deploy"

    if existing:
        existing.image = image
        existing.size = size
        existing.region = region
        existing.runtime = runtime
        existing.type = proj_type
        existing.cpu = cpu_display
        existing.memory = mem_display
        existing.status = "deploying" if do_deploy else "pending"
        project = existing
    else:
        project = Project(
            workspace_id=ws,
            name=name,
            type=proj_type,
            runtime=runtime,
            size=size,
            region=region,
            status="deploying" if do_deploy else "pending",
            instances=resources["max"],
            cpu=cpu_display,
            memory=mem_display,
            domain=None,
            last_deploy=None,
            version="rev-pending",
            git_ref="main",
            image=image,
            cloud_run_service=service_name_for(ws, name),
            current_revision=None,
            created_by=None,
        )
        db.add(project)

    await db.flush()
    project_id = project.id
    await db.commit()

    result = {
        "status": "created" if not existing else "updated",
        "project_id": str(project_id),
        "workspace": ws,
        "name": name,
        "image": image,
        "region": region,
        "size": size,
        "cloud_run_service": project.cloud_run_service,
        "deploy_started": do_deploy,
    }

    # Optional background deploy — same _bg_deploy pattern as POST /projects
    if do_deploy:
        import asyncio as _asyncio
        _asyncio.create_task(_bg_deploy(
            project_id=project_id,
            ws=ws,
            name=name,
            image=image,
            size=size,
            region=region,
            env_vars=env_vars,
            secrets=secrets_map,
            port=port,
            allow_unauth=allow_unauth,
            actor_email="cto-console-agent",
            action=action,
            unit_cost=unit_cost,
            resources=resources,
            cpu_display=cpu_display,
            mem_display=mem_display,
            git_ref="main",
        ))
        result["note"] = "Cloud Run deploy chạy background — poll GET /projects/{id} để thấy status=running + URL"

    return result


async def _exec_rotate_secret(db, args, target_ws):
    """Rotate a workspace secret in Identity Vault.

    Args: {workspace, secret_id, new_value?: str}
    If new_value omitted → generate sk_zeni_live_<24-byte-hex>.
    """
    import random as _random
    from uuid import UUID as _UUID
    from sqlalchemy import select as _select
    from app.db.models import Secret
    from app.core.vault import encrypt as _vault_encrypt

    ws = _enforce_target_ws(args.get("workspace"), target_ws, "rotate_secret")
    secret_id_raw = args.get("secret_id") or args.get("id")
    if not ws or not secret_id_raw:
        raise ValueError("rotate_secret cần workspace + secret_id")

    try:
        sid = _UUID(str(secret_id_raw))
    except (ValueError, AttributeError) as e:
        raise ValueError(f"rotate_secret: secret_id '{secret_id_raw}' không phải UUID hợp lệ") from e

    secret = (await db.execute(
        _select(Secret).where(Secret.id == sid, Secret.workspace_id == ws)
    )).scalar_one_or_none()
    if secret is None:
        raise ValueError(f"rotate_secret: secret {sid} không tìm thấy trong workspace '{ws}'")

    new_value = args.get("new_value")
    gen = new_value or f"sk_zeni_live_{_random.randbytes(24).hex()}"
    try:
        secret.value_encrypted = _vault_encrypt(gen)
    except Exception as e:
        raise RuntimeError(f"rotate_secret: vault encrypt thất bại: {e}") from e
    secret.rotations += 1
    secret.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(secret)

    return {
        "status": "rotated",
        "secret_id": str(secret.id),
        "workspace": ws,
        "name": secret.name,
        "env": secret.env,
        "rotation_no": secret.rotations,
        "new_value_returned": new_value is None,  # only echo "generated value present" flag
        # NOTE: never return plaintext in audit/result — caller must call /reveal explicitly
    }


async def _exec_deploy_canary(db, args, target_ws):
    """Deploy a project's image to Cloud Run with --no-traffic (canary revision).

    Args: {workspace, project_id_or_name, image?, no_traffic?: bool=True,
           env_vars?, secrets?, port?}
    If image not supplied → reuse project.image (re-deploy current).
    """
    from uuid import UUID as _UUID
    from sqlalchemy import select as _select
    from app.db.models import Project
    from app.api.projects import _validate_image_with_workspace, SIZE_DISPLAY
    from app.services.cloud_run import SIZE_TO_RESOURCES, deploy_service, CloudRunError

    ws = _enforce_target_ws(args.get("workspace"), target_ws, "deploy_canary")
    pid_or_name = args.get("project_id_or_name") or args.get("project_id") or args.get("name")
    if not ws or not pid_or_name:
        raise ValueError("deploy_canary cần workspace + project_id_or_name")

    # Resolve project
    project = None
    try:
        uid = _UUID(str(pid_or_name))
        project = (await db.execute(
            _select(Project).where(Project.id == uid, Project.workspace_id == ws)
        )).scalar_one_or_none()
    except (ValueError, AttributeError):
        pass
    if project is None:
        project = (await db.execute(
            _select(Project).where(Project.name == str(pid_or_name), Project.workspace_id == ws)
        )).scalar_one_or_none()
    if project is None:
        raise ValueError(f"deploy_canary: project '{pid_or_name}' không tìm thấy trong workspace '{ws}'")

    image = (args.get("image") or project.image or "").strip()
    if not image:
        raise ValueError(f"deploy_canary: project '{project.name}' chưa có image — pass args.image")

    # Validate image (workspace whitelist + global)
    try:
        await _validate_image_with_workspace(db, ws, image)
    except HTTPException as he:
        raise ValueError(f"deploy_canary image rejected: {he.detail}") from he

    size = project.size or "s"
    region = project.region or "asia-southeast1"
    resources = SIZE_TO_RESOURCES.get(size, SIZE_TO_RESOURCES["s"])
    cpu_display, mem_display, unit_cost = SIZE_DISPLAY.get(size, SIZE_DISPLAY["s"])

    env_vars = args.get("env_vars") or {}
    secrets_map = args.get("secrets") or {}
    port = int(args.get("port") or 8080)
    no_traffic = bool(args.get("no_traffic", True))  # default True for canary safety

    # Mark deploying
    project.image = image
    project.status = "deploying"
    await db.commit()

    # Synchronous deploy (Cloud Run create/update). Caller already runs async via BG task.
    # NOTE: google-cloud-run v2 Service spec doesn't expose --no-traffic as a flag in
    # the Python SDK (only via gcloud CLI). We deploy then immediately set traffic split
    # to 0% on the new revision via promote_traffic if no_traffic=True. Document for caller.
    try:
        result = await deploy_service(
            workspace=ws,
            project_name=project.name,
            image=image,
            size=size,
            region=region,
            env_vars=env_vars,
            secrets=secrets_map,
            port=port,
            allow_unauthenticated=True,
            created_by="cto-console-agent",
        )
    except CloudRunError as e:
        project.status = "failed"
        await db.commit()
        raise RuntimeError(f"deploy_canary: Cloud Run deploy thất bại: {e}") from e

    project.status = "running"
    project.region = result.region
    project.domain = result.url
    project.cloud_run_service = result.service_name
    project.current_revision = result.revision
    project.version = (f"rev-{result.revision}"[:48]) if result.revision else "rev-unknown"
    project.last_deploy = datetime.now(timezone.utc)
    await db.commit()

    return {
        "status": "deployed",
        "project_id": str(project.id),
        "workspace": ws,
        "name": project.name,
        "service_url": result.url,
        "cloud_run_service": result.service_name,
        "revision": result.revision,
        "region": result.region,
        "no_traffic_requested": no_traffic,
        "note": (
            "Cloud Run v2 Python SDK không hỗ trợ --no-traffic native. Revision deployed nhận 100% traffic."
            " Chairman gọi promote_traffic(percent=10) ngay sau để rollback nếu cần."
            if no_traffic else "Revision deployed at 100% traffic"
        ),
    }


async def _exec_promote_traffic(db, args, target_ws):
    """Set Cloud Run traffic split: 10/50/100% on latest revision.

    Args: {workspace?, project_id_or_name, percent: 10|50|100}
    Uses google-cloud-run v2 SDK (UpdateServiceRequest with traffic) — NOT subprocess gcloud.
    """
    from uuid import UUID as _UUID
    from sqlalchemy import select as _select
    from app.db.models import Project
    from app.services.cloud_run import _client, _service_full_name
    from google.api_core import exceptions as _gcp_exc
    from google.cloud import run_v2 as _run_v2

    ws = _enforce_target_ws(args.get("workspace"), target_ws, "promote_traffic")
    pid_or_name = args.get("project_id_or_name") or args.get("project_id") or args.get("name")
    percent = int(args.get("percent", 10))
    if percent not in (10, 50, 100):
        raise ValueError(f"promote_traffic: percent phải là 10|50|100, nhận '{percent}'")
    if not pid_or_name:
        raise ValueError("promote_traffic cần project_id_or_name")

    # Resolve project (workspace optional — we'll resolve by name across all if absent,
    # but only if target_ws set then enforce)
    project = None
    if ws:
        try:
            uid = _UUID(str(pid_or_name))
            project = (await db.execute(
                _select(Project).where(Project.id == uid, Project.workspace_id == ws)
            )).scalar_one_or_none()
        except (ValueError, AttributeError):
            pass
        if project is None:
            project = (await db.execute(
                _select(Project).where(Project.name == str(pid_or_name), Project.workspace_id == ws)
            )).scalar_one_or_none()
    else:
        try:
            uid = _UUID(str(pid_or_name))
            project = (await db.execute(
                _select(Project).where(Project.id == uid)
            )).scalar_one_or_none()
        except (ValueError, AttributeError):
            pass
    if project is None:
        raise ValueError(f"promote_traffic: project '{pid_or_name}' không tìm thấy")

    region = project.region or "asia-southeast1"
    service_name = project.cloud_run_service
    if not service_name:
        raise ValueError(f"promote_traffic: project '{project.name}' không có cloud_run_service — chưa deploy")

    client = _client()
    full_name = _service_full_name(service_name, region)

    try:
        svc = client.get_service(request=_run_v2.GetServiceRequest(name=full_name))
    except _gcp_exc.NotFound:
        raise ValueError(f"promote_traffic: Cloud Run service '{service_name}' không tồn tại tại {region}")
    except _gcp_exc.GoogleAPICallError as e:
        raise RuntimeError(f"promote_traffic: get service thất bại: {e.message}") from e

    latest_rev = svc.latest_ready_revision.split("/")[-1] if svc.latest_ready_revision else None
    if not latest_rev:
        raise RuntimeError(f"promote_traffic: service '{service_name}' không có ready revision")

    # Build traffic split: route `percent` to latest revision, rest to previous LATEST tag
    if percent == 100:
        traffic = [_run_v2.TrafficTarget(
            type_=_run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST,
            percent=100,
        )]
    else:
        # Find the previously-serving revision (anything ≠ latest_rev currently in traffic)
        prev_rev = None
        for t in svc.traffic_statuses or []:
            rname = (t.revision or "").split("/")[-1] if t.revision else None
            if rname and rname != latest_rev:
                prev_rev = rname
                break
        if not prev_rev:
            # No previous revision — split impossible; fall back to 100%
            traffic = [_run_v2.TrafficTarget(
                type_=_run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST,
                percent=100,
            )]
        else:
            traffic = [
                _run_v2.TrafficTarget(
                    type_=_run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION,
                    revision=latest_rev,
                    percent=percent,
                ),
                _run_v2.TrafficTarget(
                    type_=_run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION,
                    revision=prev_rev,
                    percent=100 - percent,
                ),
            ]

    # Apply via UpdateServiceRequest (only traffic field changed; SDK does diff merge)
    svc.traffic = traffic
    try:
        op = client.update_service(request=_run_v2.UpdateServiceRequest(service=svc))
        updated = op.result(timeout=300)
    except _gcp_exc.GoogleAPICallError as e:
        raise RuntimeError(f"promote_traffic: update traffic thất bại: {e.message}") from e

    return {
        "status": "traffic_updated",
        "project_id": str(project.id),
        "workspace": project.workspace_id,
        "service_name": service_name,
        "region": region,
        "latest_revision": latest_rev,
        "percent": percent,
        "service_url": updated.uri,
        "traffic": [
            {"revision": (t.revision or "LATEST"), "percent": t.percent}
            for t in (updated.traffic or [])
        ],
    }


async def _exec_trigger_build(db, args, target_ws):
    """Trigger a Build Farm job (toolchain compile native binaries).

    Args: {workspace, framework|toolchain, source_zip_path|source_ref,
           project_name?, target_platforms?: list[str], build_config?: dict,
           source_type?: zip|github|gcs}
    """
    ws = _enforce_target_ws(args.get("workspace"), target_ws, "trigger_build")
    toolchain = args.get("toolchain") or args.get("framework") or ""
    source_ref = args.get("source_ref") or args.get("source_zip_path") or ""
    if not ws or not toolchain or not source_ref:
        raise ValueError("trigger_build cần workspace + framework(toolchain) + source_zip_path/source_ref")

    target_platforms = args.get("target_platforms") or ["linux-x64"]
    if not isinstance(target_platforms, list):
        raise ValueError("trigger_build: target_platforms phải là list")
    build_config = args.get("build_config") or {}
    if "binary_name" not in build_config and args.get("project_name"):
        build_config["binary_name"] = args["project_name"]
    source_type = args.get("source_type") or ("gcs" if str(source_ref).startswith("gs://") else "zip")

    # Validate toolchain exists
    tc_row = (await db.execute(text(
        "SELECT id, supported_targets, estimated_duration_sec FROM build_farm_toolchains "
        "WHERE id = :id AND is_active = TRUE"
    ), {"id": toolchain})).mappings().first()
    if not tc_row:
        raise ValueError(f"trigger_build: toolchain '{toolchain}' không có hoặc disabled — call list_toolchains")

    supported = tc_row["supported_targets"] if isinstance(tc_row["supported_targets"], list) else json.loads(tc_row["supported_targets"] or "[]")
    invalid = [p for p in target_platforms if p not in supported]
    if invalid:
        raise ValueError(f"trigger_build: toolchain '{toolchain}' không support {invalid}. Supported: {supported}")

    # Quota check
    quota = (await db.execute(text(
        "SELECT max_concurrent, max_minutes_per_month, used_minutes_this_month "
        "FROM build_farm_quotas WHERE workspace_id = :ws"
    ), {"ws": ws})).mappings().first()
    if not quota:
        await db.execute(text(
            "INSERT INTO build_farm_quotas (workspace_id) VALUES (:ws) ON CONFLICT DO NOTHING"
        ), {"ws": ws})
        max_concurrent, max_min, used = 2, 500, 0
    else:
        max_concurrent = quota["max_concurrent"]
        max_min = quota["max_minutes_per_month"]
        used = quota["used_minutes_this_month"]

    if used >= max_min:
        raise ValueError(f"trigger_build: workspace '{ws}' vượt quota build {used}/{max_min} min/tháng")

    running_count = (await db.execute(text(
        "SELECT COUNT(*) FROM build_jobs WHERE workspace_id = :ws AND status IN ('queued','running')"
    ), {"ws": ws})).scalar() or 0
    if running_count >= max_concurrent:
        raise ValueError(f"trigger_build: workspace '{ws}' có {running_count}/{max_concurrent} jobs đang chạy")

    # Insert job row
    from datetime import timedelta as _td
    job_id = uuid.uuid4()
    expires_at = datetime.now(timezone.utc) + _td(days=30)

    await db.execute(text(
        "INSERT INTO build_jobs (id, workspace_id, user_id, job_type, source_type, source_ref, "
        "target_platforms, build_config, status, expires_at) "
        "VALUES (:id, :ws, NULL, :jt, :st, :sr, CAST(:tp AS jsonb), CAST(:bc AS jsonb), 'queued', :exp)"
    ), {
        "id": str(job_id), "ws": ws, "jt": toolchain, "st": source_type, "sr": source_ref,
        "tp": json.dumps(target_platforms), "bc": json.dumps(build_config), "exp": expires_at,
    })
    await db.commit()

    # Schedule worker (background — don't block tool execution)
    import asyncio as _asyncio
    try:
        from app.services.build_farm_worker import run_build_job
        _asyncio.create_task(run_build_job(str(job_id)))
    except Exception as e:
        log.warning("[cto] build_farm_worker unavailable: %s — job queued only", e)

    return {
        "status": "queued",
        "job_id": str(job_id),
        "workspace": ws,
        "toolchain": toolchain,
        "target_platforms": target_platforms,
        "estimated_duration_sec": tc_row["estimated_duration_sec"],
        "poll_url": f"/api/v1/build-farm/jobs/{job_id}?ws={ws}",
    }


async def _exec_delete_project(db, args, target_ws):
    return {"note": "delete_project DESTRUCTIVE — Phase 2 wiring + 2-step confirmation required"}


async def _exec_delete_secret(db, args, target_ws):
    return {"note": "delete_secret DESTRUCTIVE — Phase 2 wiring"}


async def _exec_rollback_deploy(db, args, target_ws):
    return {"note": "rollback_deploy DESTRUCTIVE — Phase 2 wiring"}


# ═════════════════════════════════════════════════
# AI agent reasoning — chairman gọi để generate next tool call
# ═════════════════════════════════════════════════
class AgentReasonIn(BaseModel):
    session_id: str
    user_message: str
    model: str = Field("claude-3-5-haiku-20241022")


@router.post("/agent/reason", status_code=200)
async def agent_reason(
    body: AgentReasonIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Agent reasoning step — gọi LLM với danh sách tools available, return:
    - reply text (markdown)
    - proposed tool calls (chairman duyệt sau)

    Pattern lấy cảm hứng Anthropic tool use, đơn giản hóa: agent return JSON
    với { "reply": "...", "tool_calls": [{"name":"create_pat","args":{...}}] }
    """
    _require_cto(me)
    # Load tool catalog
    tools = (await db.execute(text(
        "SELECT tool_name, risk_level, description FROM cto_tool_policy WHERE enabled = TRUE"
    ))).mappings().all()
    tool_catalog = "\n".join([f"- {t['tool_name']} ({t['risk_level']}): {t['description']}" for t in tools])

    system = (
        "Bạn là Zeni Cloud CTO Console Agent — assistant cho chairman để support khách + code + deploy.\n"
        "Khi user yêu cầu task cụ thể, RETURN JSON FORMAT:\n"
        '{"reply": "<markdown trả lời ngắn>", "tool_calls": [{"name":"<tool>","args":{...},"reason":"<why>"}]}\n\n'
        "Tools có sẵn:\n" + tool_catalog + "\n\n"
        "Quy tắc:\n"
        "- Chỉ propose tool, KHÔNG tự execute (chairman duyệt).\n"
        "- Tool 'safe'/'low' chairman có thể auto-approve.\n"
        "- Tool 'destructive' phải warn chairman trong reply.\n"
        "- Trả lời tiếng Việt, ngắn gọn ≤200 từ.\n"
        "- Nếu không cần tool → return tool_calls: []"
    )
    try:
        from app.services.llm_gateway import run_inference
        result = await run_inference(
            model=body.model,
            prompt=body.user_message,
            system=system,
            temperature=0.3,
            max_tokens=1024,
        )
        # Try parse JSON
        text_out = (result.output or "").strip()
        # Strip code fence if present
        if text_out.startswith("```"):
            text_out = text_out.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            parsed = json.loads(text_out)
            return {
                "reply": parsed.get("reply", ""),
                "tool_calls": parsed.get("tool_calls", []),
                "model": result.model,
                "tokens": result.input_tokens + result.output_tokens,
            }
        except Exception:
            # LLM didn't return JSON → treat whole output as reply
            return {
                "reply": text_out,
                "tool_calls": [],
                "model": result.model,
                "tokens": result.input_tokens + result.output_tokens,
            }
    except Exception as e:
        log.warning("agent_reason fallback: %s", e)
        return {
            "reply": "AI agent tạm offline. Chairman propose tool thủ công bằng UI.",
            "tool_calls": [],
            "model": "fallback",
            "tokens": 0,
            "error": str(e),
        }
