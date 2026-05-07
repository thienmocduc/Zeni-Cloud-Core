"""
Zeni Cloud Core — GitHub Integration API.

Phase 1 (MVP): Khách connect public GitHub repo → auto deploy lên Zeni Cloud
khi có push event mới.

Endpoints (prefix /github, tag github-integration):

  Connections:
    POST   /connections?ws=             — Connect repo (input: repo_url, framework)
    GET    /connections?ws=             — List connections của workspace
    GET    /connections/{id}?ws=        — Detail
    PATCH  /connections/{id}?ws=        — Update config (build cmd, env vars, branch)
    DELETE /connections/{id}?ws=        — Disconnect repo

  Webhooks:
    POST   /webhook                     — GitHub webhook receiver (push events)
                                          Verifies HMAC signature with webhook_secret

  Deploys:
    GET    /deploys/{conn_id}?ws=       — List deploy history
    POST   /deploys/{conn_id}/redeploy?ws= — Manual redeploy from latest commit

  Frameworks:
    GET    /frameworks                  — List supported frameworks (Next.js, React, FastAPI...)

Architecture:
  1. Customer: paste GitHub repo URL + framework hint → POST /connections
  2. Zeni: register webhook on GitHub (or instruct user to add manually for Phase 1)
  3. GitHub push → /webhook → verify HMAC → enqueue build
  4. Build worker: clone repo → detect framework → generate Dockerfile if needed
                 → submit Cloud Build → push to Artifact Registry
                 → deploy to L1 Compute (Cloud Run)
  5. Customer sees live URL in dashboard

Phase 2 (later): GitHub OAuth App for private repos + auto-register webhook.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Header, Request, BackgroundTasks
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db

router = APIRouter(prefix="/github", tags=["github-integration"])


# ─── Schemas ────────────────────────────────────────────
class ConnectionCreateIn(BaseModel):
    repo_url: str = Field(min_length=10, max_length=300, pattern=r"^https://github\.com/[\w.-]+/[\w.-]+/?$")
    branch: str = Field(default="main", min_length=1, max_length=64)
    framework: str = Field(default="auto", pattern=r"^(auto|nextjs|react|vue|static|fastapi|express)$")
    is_private: bool = Field(default=False)
    access_token: str | None = Field(default=None, min_length=20, max_length=300, description="GitHub PAT for private repos")
    auto_deploy: bool = Field(default=True)
    build_command: str | None = Field(default=None, max_length=300)
    install_command: str | None = Field(default=None, max_length=300)
    output_dir: str | None = Field(default=None, max_length=100)
    port: int = Field(default=8080, ge=1, le=65535)
    env_vars: dict[str, str] | None = Field(default=None)


class ConnectionUpdateIn(BaseModel):
    branch: str | None = Field(default=None, max_length=64)
    auto_deploy: bool | None = None
    build_command: str | None = None
    install_command: str | None = None
    output_dir: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    env_vars: dict[str, str] | None = None


# ─── Helpers ────────────────────────────────────────────
def _parse_repo_url(repo_url: str) -> tuple[str, str]:
    """Extract owner/repo from https://github.com/owner/repo URL."""
    m = re.match(r"^https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", repo_url)
    if not m:
        raise HTTPException(status_code=400, detail="repo_url không hợp lệ. Format: https://github.com/owner/repo")
    return m.group(1), m.group(2)


def _verify_webhook_signature(secret: str, payload: bytes, signature: str | None) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature."""
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ─── Connect repo ───────────────────────────────────────
@router.post("/connections", status_code=201)
async def connect_repo(
    data: ConnectionCreateIn,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Connect a GitHub repo to this workspace for auto-deploy."""
    await require_workspace_access(ws, me)

    owner, repo = _parse_repo_url(data.repo_url)

    # Check duplicate
    existing = (await db.execute(
        text("SELECT id FROM github_connections WHERE workspace_id = :ws AND repo_owner = :o AND repo_name = :r"),
        {"ws": ws, "o": owner, "r": repo}
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail=f"Repo {owner}/{repo} đã connect rồi.")

    # Generate webhook secret
    webhook_secret = secrets.token_urlsafe(32)

    # Insert connection
    row = (await db.execute(
        text("""
            INSERT INTO github_connections (
                workspace_id, repo_url, repo_owner, repo_name, default_branch,
                is_private, webhook_secret, auto_deploy, build_command,
                install_command, output_dir, framework, port, env_vars
            ) VALUES (
                :ws, :url, :o, :r, :branch,
                :priv, :sec, :auto, :build,
                :install, :out, :fw, :port, :env
            )
            RETURNING id, repo_owner, repo_name, default_branch, framework, port, status, created_at
        """),
        {
            "ws": ws, "url": data.repo_url, "o": owner, "r": repo, "branch": data.branch,
            "priv": data.is_private, "sec": webhook_secret, "auto": data.auto_deploy,
            "build": data.build_command, "install": data.install_command,
            "out": data.output_dir, "fw": data.framework, "port": data.port,
            "env": __import__("json").dumps(data.env_vars or {})
        }
    )).first()

    await db.execute(
        text("INSERT INTO audit_log (workspace_id, actor, action, target, severity, metadata) "
             "VALUES (:w, :a, 'github.connect', :t, 'ok', :m)"),
        {"w": ws, "a": me.email, "t": f"{owner}/{repo}",
         "m": __import__("json").dumps({"branch": data.branch, "framework": data.framework})}
    )
    await db.commit()

    return {
        "id": row[0],
        "repo": f"{owner}/{repo}",
        "branch": row[3],
        "framework": row[4],
        "port": row[5],
        "status": row[6],
        "webhook_url": f"https://zenicloud.io/api/v1/github/webhook",
        "webhook_secret": webhook_secret,
        "instructions": [
            f"1. Vào Settings → Webhooks của repo {owner}/{repo} trên GitHub",
            "2. Add webhook với:",
            "   - Payload URL: https://zenicloud.io/api/v1/github/webhook",
            "   - Content type: application/json",
            f"   - Secret: {webhook_secret}",
            "   - Events: 'Just the push event'",
            "3. Save → push code → Zeni tự build & deploy"
        ]
    }


# ─── List connections ───────────────────────────────────
@router.get("/connections")
async def list_connections(
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    await require_workspace_access(ws, me)
    rows = (await db.execute(
        text("""SELECT id, repo_url, repo_owner, repo_name, default_branch, framework,
                       auto_deploy, status, last_deploy_at, last_deploy_sha, last_deploy_status,
                       project_id, created_at
                FROM github_connections WHERE workspace_id = :ws ORDER BY created_at DESC"""),
        {"ws": ws}
    )).all()
    return [
        {
            "id": r[0], "repo_url": r[1], "repo_owner": r[2], "repo_name": r[3],
            "default_branch": r[4], "framework": r[5], "auto_deploy": r[6], "status": r[7],
            "last_deploy_at": r[8].isoformat() if r[8] else None,
            "last_deploy_sha": r[9], "last_deploy_status": r[10],
            "project_id": str(r[11]) if r[11] else None,
            "created_at": r[12].isoformat() if r[12] else None,
        }
        for r in rows
    ]


# ─── Detail ─────────────────────────────────────────────
@router.get("/connections/{conn_id}")
async def get_connection(
    conn_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    row = (await db.execute(
        text("""SELECT id, repo_url, repo_owner, repo_name, default_branch, framework,
                       auto_deploy, status, last_deploy_at, last_deploy_sha, last_deploy_status,
                       build_command, install_command, output_dir, port, env_vars,
                       webhook_secret, created_at
                FROM github_connections WHERE id = :id AND workspace_id = :ws"""),
        {"id": conn_id, "ws": ws}
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    return {
        "id": row[0], "repo_url": row[1], "repo_owner": row[2], "repo_name": row[3],
        "default_branch": row[4], "framework": row[5], "auto_deploy": row[6], "status": row[7],
        "last_deploy_at": row[8].isoformat() if row[8] else None,
        "last_deploy_sha": row[9], "last_deploy_status": row[10],
        "build_command": row[11], "install_command": row[12], "output_dir": row[13],
        "port": row[14], "env_vars": row[15], "webhook_secret": row[16],
        "created_at": row[17].isoformat() if row[17] else None,
    }


# ─── Update ─────────────────────────────────────────────
@router.patch("/connections/{conn_id}")
async def update_connection(
    conn_id: int,
    ws: str,
    data: ConnectionUpdateIn,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_workspace_access(ws, me)
    fields = {}
    if data.branch is not None: fields["default_branch"] = data.branch
    if data.auto_deploy is not None: fields["auto_deploy"] = data.auto_deploy
    if data.build_command is not None: fields["build_command"] = data.build_command
    if data.install_command is not None: fields["install_command"] = data.install_command
    if data.output_dir is not None: fields["output_dir"] = data.output_dir
    if data.port is not None: fields["port"] = data.port
    if data.env_vars is not None: fields["env_vars"] = __import__("json").dumps(data.env_vars)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    fields["id"] = conn_id; fields["ws"] = ws
    res = await db.execute(
        text(f"UPDATE github_connections SET {set_clause}, updated_at = NOW() WHERE id = :id AND workspace_id = :ws"),
        fields
    )
    if (res.rowcount or 0) == 0:
        raise HTTPException(status_code=404, detail="Connection not found")
    await db.commit()
    return {"id": conn_id, "updated": True}


# ─── Delete ─────────────────────────────────────────────
@router.delete("/connections/{conn_id}", status_code=204, response_model=None)
async def delete_connection(
    conn_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await require_workspace_access(ws, me)
    res = await db.execute(
        text("DELETE FROM github_connections WHERE id = :id AND workspace_id = :ws"),
        {"id": conn_id, "ws": ws}
    )
    if (res.rowcount or 0) == 0:
        raise HTTPException(status_code=404, detail="Connection not found")
    await db.commit()


# ─── Webhook receiver ────────────────────────────────────
@router.post("/webhook")
async def github_webhook(
    request: Request,
    bg: BackgroundTasks,
    x_github_event: str | None = Header(default=None, alias="X-GitHub-Event"),
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    GitHub webhook receiver. Handles push events for connected repos.

    Workflow:
      1. Read body (raw bytes for HMAC verify)
      2. Parse JSON, extract repo + commit info
      3. Look up github_connections by repo
      4. Verify HMAC signature with webhook_secret
      5. Enqueue background build & deploy
    """
    payload = await request.body()
    import json as _json
    try:
        data = _json.loads(payload.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Only handle push events
    if x_github_event != "push":
        return {"ok": True, "ignored": f"event_type={x_github_event}"}

    # Extract repo + commit
    repo_full = (data.get("repository") or {}).get("full_name", "")
    if "/" not in repo_full:
        raise HTTPException(status_code=400, detail="Missing repository.full_name")
    owner, repo = repo_full.split("/", 1)

    branch = (data.get("ref") or "").replace("refs/heads/", "")
    head = data.get("head_commit") or {}
    commit_sha = head.get("id") or data.get("after", "")
    commit_msg = head.get("message", "")
    commit_author = (head.get("author") or {}).get("name", "")

    # Look up connection
    row = (await db.execute(
        text("""SELECT id, workspace_id, default_branch, webhook_secret, auto_deploy, status
                FROM github_connections
                WHERE repo_owner = :o AND repo_name = :r AND status = 'connected'"""),
        {"o": owner, "r": repo}
    )).first()
    if row is None:
        return {"ok": True, "ignored": f"no connection for {repo_full}"}

    conn_id, ws_id, default_branch, secret, auto_deploy, status = row[0], row[1], row[2], row[3], row[4], row[5]

    # Verify HMAC
    if not _verify_webhook_signature(secret, payload, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid HMAC signature")

    # Skip if not target branch
    if branch != default_branch:
        return {"ok": True, "skipped": f"branch={branch} != default={default_branch}"}

    # Skip if auto_deploy disabled
    if not auto_deploy:
        return {"ok": True, "skipped": "auto_deploy=False"}

    # Insert deploy record
    deploy_row = (await db.execute(
        text("""INSERT INTO github_deploys (connection_id, workspace_id, trigger_type,
                       commit_sha, commit_message, commit_author, branch, status)
                VALUES (:c, :ws, 'webhook', :sha, :msg, :auth, :br, 'queued')
                RETURNING id"""),
        {"c": conn_id, "ws": ws_id, "sha": commit_sha, "msg": commit_msg[:500],
         "auth": commit_author, "br": branch}
    )).first()
    await db.commit()

    # PHASE 2: Trigger background build & deploy worker
    from app.services.github_build import run_github_build_and_deploy
    from app.db.base import SessionLocal
    async def _bg_gh_build():
        async with SessionLocal() as new_db:
            await run_github_build_and_deploy(
                db=new_db, deploy_id=deploy_row[0], connection_id=conn_id,
                workspace_id=ws_id, repo_owner=owner, repo_name=repo,
                branch=branch, framework=row[6] if len(row) > 6 else "auto",
                port=8080, commit_sha=commit_sha,
            )
    bg.add_task(_bg_gh_build)

    return {
        "ok": True,
        "deploy_id": deploy_row[0],
        "commit": commit_sha[:8],
        "message": commit_msg[:80],
        "queued": True,
        "note": "Build worker will clone, build, and deploy. Poll deploys endpoint to track.",
    }


# ─── Deploy history ──────────────────────────────────────
@router.get("/deploys/{conn_id}")
async def list_deploys(
    conn_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    await require_workspace_access(ws, me)
    rows = (await db.execute(
        text("""SELECT id, trigger_type, commit_sha, commit_message, commit_author, branch,
                       status, started_at, completed_at, duration_sec, deploy_url, error_message
                FROM github_deploys WHERE connection_id = :c AND workspace_id = :ws
                ORDER BY started_at DESC LIMIT 50"""),
        {"c": conn_id, "ws": ws}
    )).all()
    return [
        {
            "id": r[0], "trigger": r[1], "commit": r[2][:8] if r[2] else None,
            "message": r[3], "author": r[4], "branch": r[5], "status": r[6],
            "started_at": r[7].isoformat() if r[7] else None,
            "completed_at": r[8].isoformat() if r[8] else None,
            "duration_sec": r[9], "deploy_url": r[10], "error": r[11],
        }
        for r in rows
    ]


# ─── Manual redeploy ─────────────────────────────────────
@router.post("/deploys/{conn_id}/redeploy")
async def redeploy(
    conn_id: int,
    ws: str,
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Trigger manual redeploy from latest commit on default branch."""
    await require_workspace_access(ws, me)
    conn = (await db.execute(
        text("SELECT default_branch FROM github_connections WHERE id = :id AND workspace_id = :ws"),
        {"id": conn_id, "ws": ws}
    )).scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    deploy = (await db.execute(
        text("""INSERT INTO github_deploys (connection_id, workspace_id, trigger_type,
                       commit_sha, commit_message, branch, status)
                VALUES (:c, :ws, 'manual', 'HEAD', 'Manual redeploy', :br, 'queued')
                RETURNING id"""),
        {"c": conn_id, "ws": ws, "br": conn}
    )).first()
    await db.commit()
    return {"deploy_id": deploy[0], "queued": True}


# ─── Frameworks ──────────────────────────────────────────
@router.get("/frameworks")
async def list_frameworks(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(
        text("""SELECT framework, display_name, detect_files, install_cmd, build_cmd,
                       output_dir, default_port FROM github_framework_templates
                ORDER BY display_name""")
    )).all()
    return [
        {
            "framework": r[0], "display_name": r[1], "detect_files": r[2],
            "install_cmd": r[3], "build_cmd": r[4], "output_dir": r[5], "default_port": r[6]
        }
        for r in rows
    ]
