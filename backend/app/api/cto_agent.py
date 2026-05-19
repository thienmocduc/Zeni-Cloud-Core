"""
Zeni Cloud Core — CTO Chat Assistant API.

Phase 1 (legacy regex deploy):
  POST   /deploy?ws=X     — paste image URL → em deploy hộ
  GET    /session/{id}    — poll status + messages
  GET    /sessions?ws=X   — list sessions in workspace

Phase 2 (LIVE LLM chat — Gemini 2.5 Pro + Sonnet 4.6 fallback):
  POST   /chat?ws=X       — send user message, agent calls tools + replies
                            (Background task → poll GET /session/{id} for streaming output)
  POST   /chat/new?ws=X   — create fresh chat session

Storage: cto_sessions table — messages JSONB holds mixed entries
  {ts, level: "user"|"assistant"|"info"|"warn"|"error"|"tool", text}
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import SessionLocal, get_db
from app.services.audit import audit_push

log = logging.getLogger("zeni.api.cto")
log.setLevel(logging.INFO)
router = APIRouter(prefix="/cto", tags=["cto-assistant"])

# Hold strong references to bg tasks → tránh GC cancel task lúc đang chạy LLM.
# Bug Phase 2: asyncio.create_task(...) không hold ref → GC có thể dọn → task cancelled silently.
_BG_TASKS: set = set()


def _spawn_bg(coro) -> None:
    """Schedule a background coroutine and keep a strong reference until it completes."""
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


# ─── Pydantic schemas ─────────────────────────────────────────
class DeployIn(BaseModel):
    input_text: str = Field(min_length=3, max_length=2000,
                            description="GitHub URL, image URL, or natural language description")
    project_name: str | None = Field(default=None, max_length=48,
                                     description="Optional project name; auto-derive if omitted")
    size: str = Field(default="s", pattern=r"^(xs|s|m|l)$")
    region: str = Field(default="asia-southeast1", max_length=32)


class SessionOut(BaseModel):
    session_id: str
    workspace_id: str
    status: str   # 'analyzing' | 'building' | 'deploying' | 'success' | 'failed'
    detected_input_type: str | None = None  # 'github' | 'image' | 'zip' | 'description' | 'unknown'
    project_id: str | None = None
    project_url: str | None = None
    messages: list[dict]   # [{ts, level, text}]
    created_at: datetime
    completed_at: datetime | None = None


# ─── Input detector ──────────────────────────────────────────
_GITHUB_RE = re.compile(r"^https?://github\.com/([\w.-]+)/([\w.-]+)(?:\.git)?(?:/tree/([\w./-]+))?/?$", re.IGNORECASE)
_IMAGE_RE = re.compile(
    r"^([a-z0-9][a-z0-9._\-/]*?(?:\.[a-z0-9._\-]+)?)/"  # registry host
    r"([a-z0-9][a-z0-9._\-/]*)"                          # repo
    r"(:[\w.\-]+)?"                                        # optional tag
    r"(@sha256:[a-f0-9]{64})?$",                          # optional digest
    re.IGNORECASE,
)


def detect_input_type(text: str, workspace_id: str | None = None) -> tuple[str, dict[str, Any]]:
    """Return (input_type, parsed_meta). If text is short form (no host) like
    'your-app:v1', auto-prefix với Zeni Container Registry của workspace."""
    t = text.strip()

    # GitHub URL
    m = _GITHUB_RE.match(t)
    if m:
        owner, repo, branch = m.groups()
        return ("github", {"owner": owner, "repo": repo.rstrip(".git"), "branch": branch or "main"})

    # Image URL with host (docker.io, gcr.io, ghcr.io, *.pkg.dev, etc.)
    if "/" in t and not t.startswith("http"):
        m = _IMAGE_RE.match(t)
        if m:
            return ("image", {"image_url": t})

    # Short form `your-app:v1` (no slash, no host) → auto-prefix Zeni Registry
    if ":" in t and "/" not in t and " " not in t and workspace_id:
        slug = re.sub(r"[^a-z0-9-]", "-", workspace_id.lower()).strip("-")[:30] or "ws"
        full = f"us-central1-docker.pkg.dev/zeni-cloud-core/{slug}/{t}"
        return ("image", {"image_url": full, "auto_prefixed": True, "original": t})

    # Otherwise — natural language description (Phase 2)
    return ("description", {"text": t})


# ─── DB helpers ──────────────────────────────────────────────
async def _ensure_table(db: AsyncSession) -> None:
    """Best-effort table create — idempotent."""
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS cto_sessions (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id    VARCHAR(64) NOT NULL,
            user_email      VARCHAR(255),
            input_text      TEXT NOT NULL,
            input_type      VARCHAR(32),
            status          VARCHAR(32) NOT NULL DEFAULT 'analyzing',
            project_id      UUID,
            project_url     TEXT,
            messages        JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at    TIMESTAMPTZ
        )
    """))
    await db.commit()


async def _push_message(db: AsyncSession, session_id: str, level: str, text_msg: str) -> None:
    """Append a log line to messages JSONB array."""
    ts = datetime.now(timezone.utc).isoformat()
    msg = {"ts": ts, "level": level, "text": text_msg}
    await db.execute(text("""
        UPDATE cto_sessions
        SET messages = messages || CAST(:m AS JSONB)
        WHERE id = :id
    """), {"id": session_id, "m": __import__("json").dumps([msg])})
    await db.commit()


async def _set_status(db: AsyncSession, session_id: str, status: str,
                      project_id: str | None = None, project_url: str | None = None) -> None:
    completed = "NOW()" if status in ("success", "failed") else "NULL"
    await db.execute(text(f"""
        UPDATE cto_sessions
        SET status = :s, project_id = :pid, project_url = :url, completed_at = {completed}
        WHERE id = :id
    """), {"id": session_id, "s": status, "pid": project_id, "url": project_url})
    await db.commit()


# ─── Background orchestrator ─────────────────────────────────
async def _bg_orchestrate(session_id: str, workspace_id: str, user_email: str,
                          input_type: str, parsed: dict, project_name: str,
                          size: str, region: str, jwt_token: str | None) -> None:
    """Run the deploy orchestration in background. Each step pushes a log message."""
    async with SessionLocal() as db:
        try:
            await _push_message(db, session_id, "info",
                                f"Detected input type: {input_type}. Starting orchestration...")

            if input_type == "image":
                await _orchestrate_image(db, session_id, workspace_id, user_email,
                                          parsed["image_url"], project_name, size, region, jwt_token)
            elif input_type == "github":
                await _push_message(db, session_id, "warn",
                                    "GitHub URL flow chưa implement đầy đủ ở Phase 1. "
                                    "Tạm thời dùng image URL hoặc upload ZIP qua /api/v1/upload/source.")
                await _set_status(db, session_id, "failed")
            elif input_type == "description":
                await _push_message(db, session_id, "warn",
                                    "Mô tả tự nhiên (code generation) thuộc Phase 2. "
                                    "Hãy paste GitHub URL hoặc Docker image URL.")
                await _set_status(db, session_id, "failed")
            else:
                await _push_message(db, session_id, "error", f"Unknown input type: {input_type}")
                await _set_status(db, session_id, "failed")

        except Exception as e:
            log.exception("[cto orchestrate] session=%s failed: %s", session_id, e)
            try:
                await _push_message(db, session_id, "error", f"Internal error: {e}")
                await _set_status(db, session_id, "failed")
            except Exception:
                pass


async def _orchestrate_image(db: AsyncSession, session_id: str, workspace_id: str,
                              user_email: str, image_url: str, project_name: str,
                              size: str, region: str, jwt_token: str | None) -> None:
    """Image URL → verify whitelist → auto-add prefix → POST /projects to deploy."""
    await _push_message(db, session_id, "info", f"Verifying image URL: {image_url}")

    # 1. Auto-add whitelist for the image's prefix (everything before the last `/`)
    if "/" in image_url:
        prefix = image_url.rsplit("/", 1)[0] + "/"
        try:
            await db.execute(text("""
                INSERT INTO workspace_image_whitelist (workspace_id, prefix, description, enabled)
                VALUES (:ws, :p, 'auto-added by CTO assistant', TRUE)
                ON CONFLICT (workspace_id, prefix) DO NOTHING
            """), {"ws": workspace_id, "p": prefix})
            await db.commit()
            await _push_message(db, session_id, "info", f"Whitelist prefix added: {prefix}")
        except Exception as e:
            await _push_message(db, session_id, "warn", f"Whitelist insert skipped: {e}")

    # 2. POST /api/v1/projects via internal call (reuse existing deploy logic)
    await _set_status(db, session_id, "deploying")
    await _push_message(db, session_id, "info", "Creating Cloud Run service...")

    from app.api.projects import deploy_project as projects_deploy
    from app.schemas.resources import ProjectCreateIn

    payload = ProjectCreateIn(
        name=project_name,
        type="web",
        runtime="container",
        size=size,
        region=region,
        image=image_url,
        port=8080,
        allow_unauthenticated=True,
    )

    # Mock CurrentUser since we already validated workspace access in /deploy endpoint
    class _MockUser:
        def __init__(self, email: str):
            self.email = email
            self.id = None
            self.role = "Developer"
            self.auth_scope = "full"

    me = _MockUser(user_email)
    bg = BackgroundTasks()

    try:
        # NOTE: projects.deploy_project signature is (ws, data, bg, me, db) — kwargs must match exactly
        result = await projects_deploy(ws=workspace_id, data=payload, bg=bg, me=me, db=db)
        proj_id = str(result.id)
        await _push_message(db, session_id, "info",
                            f"Project created: {result.name} (id={proj_id})")
        # Run any background tasks scheduled by projects_deploy
        for task in bg.tasks:
            asyncio.create_task(task())

        # Poll project status for up to 90s
        for _ in range(30):
            await asyncio.sleep(3)
            row = (await db.execute(text("""
                SELECT status, domain FROM projects WHERE id = :id
            """), {"id": proj_id})).mappings().first()
            if not row:
                continue
            if row["status"] == "running":
                url = row["domain"] or ""
                await _push_message(db, session_id, "info", f"✓ Live: {url}")
                await _set_status(db, session_id, "success", project_id=proj_id, project_url=url)
                return
            if row["status"] == "failed":
                await _push_message(db, session_id, "error", "Cloud Run deploy failed. Check projects page.")
                await _set_status(db, session_id, "failed", project_id=proj_id)
                return
        await _push_message(db, session_id, "warn", "Deploy taking longer than 90s — check projects page.")
        await _set_status(db, session_id, "failed", project_id=proj_id)
    except HTTPException as e:
        await _push_message(db, session_id, "error", f"Deploy rejected: {e.detail}")
        await _set_status(db, session_id, "failed")
    except Exception as e:
        await _push_message(db, session_id, "error", f"Deploy failed: {e}")
        await _set_status(db, session_id, "failed")


# ─── Endpoints ───────────────────────────────────────────────
@router.post("/deploy", response_model=SessionOut, status_code=202)
async def cto_deploy(
    payload: DeployIn,
    ws: str = Query(..., min_length=1, max_length=64),
    background_tasks: BackgroundTasks = None,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SessionOut:
    """Analyze input + kick off background orchestration."""
    await require_workspace_access(ws, me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không được trigger deploy")

    await _ensure_table(db)

    input_type, parsed = detect_input_type(payload.input_text, workspace_id=ws)

    # Auto-derive project name
    if not payload.project_name:
        if input_type == "image":
            base = payload.input_text.rsplit("/", 1)[-1].split(":")[0]
            payload.project_name = re.sub(r"[^a-z0-9-]", "-", base.lower())[:40].strip("-") or "cto-app"
        elif input_type == "github":
            payload.project_name = re.sub(r"[^a-z0-9-]", "-", parsed["repo"].lower())[:40].strip("-")
        else:
            payload.project_name = "cto-app-" + uuid.uuid4().hex[:6]

    # Create session row
    row = (await db.execute(text("""
        INSERT INTO cto_sessions (workspace_id, user_email, input_text, input_type, status)
        VALUES (:ws, :email, :inp, :it, 'analyzing')
        RETURNING id, created_at
    """), {"ws": ws, "email": me.email, "inp": payload.input_text, "it": input_type})).mappings().first()
    session_id = str(row["id"])
    await db.commit()

    await audit_push(
        db, actor=me.email, workspace_id=ws, action="cto.deploy.start",
        target=payload.project_name, severity="info",
        metadata={"input_type": input_type, "session_id": session_id},
    )
    await db.commit()

    # Schedule background work
    _spawn_bg(_bg_orchestrate(
        session_id, ws, me.email, input_type, parsed,
        payload.project_name, payload.size, payload.region,
        jwt_token=None,
    ))

    return SessionOut(
        session_id=session_id,
        workspace_id=ws,
        status="analyzing",
        detected_input_type=input_type,
        project_id=None,
        project_url=None,
        messages=[],
        created_at=row["created_at"],
        completed_at=None,
    )


@router.get("/session/{session_id}", response_model=SessionOut)
async def cto_session_status(
    session_id: str,
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SessionOut:
    """Poll session status + messages."""
    await require_workspace_access(ws, me)
    row = (await db.execute(text("""
        SELECT id, workspace_id, status, input_type, project_id, project_url,
               messages, created_at, completed_at
        FROM cto_sessions
        WHERE id = :id AND workspace_id = :ws
    """), {"id": session_id, "ws": ws})).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Session không tồn tại")

    return SessionOut(
        session_id=str(row["id"]),
        workspace_id=row["workspace_id"],
        status=row["status"],
        detected_input_type=row["input_type"],
        project_id=str(row["project_id"]) if row["project_id"] else None,
        project_url=row["project_url"],
        messages=row["messages"] or [],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


@router.get("/sessions", response_model=list[SessionOut])
async def cto_sessions_list(
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SessionOut]:
    """List recent CTO sessions in workspace."""
    await require_workspace_access(ws, me)
    rows = (await db.execute(text("""
        SELECT id, workspace_id, status, input_type, project_id, project_url,
               messages, created_at, completed_at
        FROM cto_sessions
        WHERE workspace_id = :ws
        ORDER BY created_at DESC
        LIMIT 50
    """), {"ws": ws})).mappings().all()
    return [
        SessionOut(
            session_id=str(r["id"]),
            workspace_id=r["workspace_id"],
            status=r["status"],
            detected_input_type=r["input_type"],
            project_id=str(r["project_id"]) if r["project_id"] else None,
            project_url=r["project_url"],
            messages=r["messages"] or [],
            created_at=r["created_at"],
            completed_at=r["completed_at"],
        )
        for r in rows
    ]


# ═══════════════════════════════════════════════════════════════
# Phase 2 — LIVE LLM chat (Gemini 2.5 + Sonnet 4.6)
# ═══════════════════════════════════════════════════════════════

class ChatIn(BaseModel):
    session_id: str | None = Field(default=None, description="Existing session UUID; omit to start new chat")
    message: str = Field(min_length=1, max_length=4000)


class ChatOut(BaseModel):
    session_id: str
    status: str   # 'thinking' — frontend polls GET /session/{id} for streaming progress
    accepted_at: datetime


async def _bg_chat_turn(session_id: str, workspace_id: str, user_email: str,
                         user_message: str) -> None:
    """Background: run LLM tool-use loop, write progress + final answer to messages."""
    from app.services.cto_chat import chat_turn

    log.info("[cto.chat] START session=%s ws=%s user=%s", session_id, workspace_id, user_email)

    async with SessionLocal() as db:
        try:
            # Mark thinking
            await db.execute(text("""
                UPDATE cto_sessions SET status = 'thinking' WHERE id = :id
            """), {"id": session_id})
            await db.commit()
            log.info("[cto.chat] marked thinking session=%s", session_id)

            # Load history (only user/assistant turns — filter out info/tool/error)
            row = (await db.execute(text("""
                SELECT messages FROM cto_sessions WHERE id = :id
            """), {"id": session_id})).mappings().first()
            all_msgs = (row["messages"] or []) if row else []
            history: list[dict] = []
            for m in all_msgs:
                lvl = m.get("level")
                if lvl == "user":
                    history.append({"role": "user", "content": m.get("text", "")})
                elif lvl == "assistant":
                    history.append({"role": "assistant", "content": m.get("text", "")})

            # Append user message to log
            await _push_message(db, session_id, "user", user_message)

            # Progress callback writes to messages JSONB for frontend polling
            async def progress(level: str, txt: str) -> None:
                async with SessionLocal() as cb_db:
                    await _push_message(cb_db, session_id, level, txt)

            log.info("[cto.chat] calling chat_turn session=%s history_len=%d", session_id, len(history))
            result = await chat_turn(
                workspace_id=workspace_id, user_email=user_email, db=db,
                history=history, user_message=user_message,
                progress_callback=progress,
            )
            log.info("[cto.chat] chat_turn DONE session=%s model=%s iters=%d tools=%d final_len=%d",
                      session_id, result.get("model_used"), result.get("iterations", 0),
                      len(result.get("tool_calls", [])), len(result.get("final_text", "")))

            # Write final assistant answer
            await _push_message(db, session_id, "assistant", result["final_text"])
            log.info("[cto.chat] assistant message pushed session=%s", session_id)

            # Update status + metadata
            project_id = None
            project_url = None
            for tc in result["tool_calls"]:
                if tc["tool"] == "deploy_image" and tc["result"].get("ok"):
                    project_id = tc["result"].get("project_id")
                elif tc["tool"] == "get_project_status" and tc["result"].get("ok"):
                    project_url = tc["result"].get("url") or project_url
                    if not project_id:
                        project_id = tc["result"].get("project_id")

            await db.execute(text("""
                UPDATE cto_sessions
                SET status = 'ready',
                    project_id = COALESCE(CAST(NULLIF(:pid, '') AS UUID), project_id),
                    project_url = COALESCE(NULLIF(:url, ''), project_url)
                WHERE id = :id
            """), {"id": session_id, "pid": project_id or "", "url": project_url or ""})
            await db.commit()

            await audit_push(
                db, actor=user_email, workspace_id=workspace_id,
                action="cto.chat.turn", target=session_id, severity="ok",
                metadata={"model": result["model_used"], "iterations": result["iterations"],
                          "tool_calls": len(result["tool_calls"])},
            )
            await db.commit()

        except Exception as e:
            log.exception("[cto.chat] session=%s failed: %s", session_id, e)
            # Recovery: outer db có thể đã rollback/closed → mở session mới để
            # ĐẢM BẢO frontend ko mãi mãi thấy "Em đang nghĩ..."
            import traceback
            tb = traceback.format_exc()[-1500:]
            try:
                async with SessionLocal() as rec_db:
                    await _push_message(rec_db, session_id, "error",
                                         f"Chat turn failed: {type(e).__name__}: {e}")
                    await _push_message(rec_db, session_id, "assistant",
                                         "Em gặp lỗi xử lý. Sếp thử gõ lại hoặc bấm 'Cuộc trò chuyện mới'. "
                                         f"(Lỗi: {type(e).__name__})")
                    await rec_db.execute(text("UPDATE cto_sessions SET status = 'ready' WHERE id = :id"),
                                          {"id": session_id})
                    await rec_db.commit()
                log.error("[cto.chat] recovery wrote error+assistant fallback for session=%s, tb=%s",
                           session_id, tb)
            except Exception as rec_err:
                log.exception("[cto.chat] RECOVERY ALSO FAILED session=%s err=%s orig=%s tb=%s",
                               session_id, rec_err, e, tb)


@router.post("/chat", response_model=ChatOut, status_code=202)
async def cto_chat(
    payload: ChatIn,
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatOut:
    """Send a user message to CTO chat agent. Returns immediately;
    poll GET /cto/session/{id} every 1-2s for streaming output."""
    await require_workspace_access(ws, me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không được chat với CTO Assistant")

    await _ensure_table(db)

    # Get-or-create session
    session_id = payload.session_id
    if session_id:
        row = (await db.execute(text("""
            SELECT id FROM cto_sessions WHERE id = :id AND workspace_id = :ws
        """), {"id": session_id, "ws": ws})).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Session không tồn tại trong workspace này")
    else:
        row = (await db.execute(text("""
            INSERT INTO cto_sessions (workspace_id, user_email, input_text, input_type, status)
            VALUES (:ws, :email, :msg, 'chat', 'thinking')
            RETURNING id
        """), {"ws": ws, "email": me.email, "msg": payload.message[:500]})).mappings().first()
        session_id = str(row["id"])
        await db.commit()

    # Background tool-use loop — hold strong reference!
    log.info("[cto.chat] queue session=%s ws=%s msg_len=%d", session_id, ws, len(payload.message))
    _spawn_bg(_bg_chat_turn(session_id, ws, me.email, payload.message))

    return ChatOut(
        session_id=session_id,
        status="thinking",
        accepted_at=datetime.now(timezone.utc),
    )


@router.post("/chat/new", response_model=SessionOut, status_code=201)
async def cto_chat_new(
    ws: str = Query(..., min_length=1, max_length=64),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SessionOut:
    """Create a fresh chat session (no initial message)."""
    await require_workspace_access(ws, me)
    if me.role == "Viewer":
        raise HTTPException(status_code=403, detail="Viewer không được tạo chat session")
    await _ensure_table(db)

    row = (await db.execute(text("""
        INSERT INTO cto_sessions (workspace_id, user_email, input_text, input_type, status)
        VALUES (:ws, :email, '', 'chat', 'ready')
        RETURNING id, created_at
    """), {"ws": ws, "email": me.email})).mappings().first()
    await db.commit()

    return SessionOut(
        session_id=str(row["id"]),
        workspace_id=ws,
        status="ready",
        detected_input_type="chat",
        project_id=None, project_url=None,
        messages=[], created_at=row["created_at"], completed_at=None,
    )
