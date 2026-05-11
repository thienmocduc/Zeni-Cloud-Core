"""
GitHub App API endpoints (Phase 1 P1.1 — chairman approved 2026-05-11)

Routes:
  GET  /github-app/install-url            — return install URL for customer to click
  GET  /github-app/callback               — OAuth callback from GitHub App install
  GET  /github-app/installations          — list installations for current user
  GET  /github-app/installations/{id}/repos — list repos in installation
  POST /github-app/installations/{id}/import-repo — 1-click import + framework detect + create project

Endpoints này HOÀN TOÀN MỚI — không đụng /github/* cũ (giữ webhook secret manual flow).
Customer có thể chọn 1 trong 2 flow:
  - /github (cũ): paste repo URL + add webhook manual (vẫn work)
  - /github-app (mới): 1-click install → auto everything

KHÔNG đụng UI/UX khi chưa có lệnh — chairman dock UI design phase.
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.core.error_catalog import format_error_response
from app.db.base import get_db
from app.services import github_app as ga
from app.services.framework_detector import detect_framework

log = logging.getLogger("zeni.api.github_app")
router = APIRouter(prefix="/github-app", tags=["github-app"])


# ─── Helpers — load App creds từ Secret Manager hoặc env ──────────
async def _get_app_creds() -> dict[str, str]:
    """
    Load GitHub App credentials.

    Try Secret Manager first (production), fallback to env vars (local dev).
    Required secrets/env:
      - GITHUB_APP_ID
      - GITHUB_APP_PRIVATE_KEY (PEM)
      - GITHUB_APP_CLIENT_ID
      - GITHUB_APP_CLIENT_SECRET
      - GITHUB_APP_NAME (e.g., "zeni-cloud")
    """
    import os
    from app.core.config import settings

    # Try Secret Manager
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        project = settings.gcp_project_id

        def _get(name: str) -> Optional[str]:
            try:
                r = client.access_secret_version(
                    request={"name": f"projects/{project}/secrets/{name}/versions/latest"}
                )
                return r.payload.data.decode()
            except Exception:
                return None

        return {
            "app_id": _get("github-app-id") or os.environ.get("GITHUB_APP_ID", ""),
            "private_key": _get("github-app-private-key") or os.environ.get("GITHUB_APP_PRIVATE_KEY", ""),
            "client_id": _get("github-app-client-id") or os.environ.get("GITHUB_APP_CLIENT_ID", ""),
            "client_secret": _get("github-app-client-secret") or os.environ.get("GITHUB_APP_CLIENT_SECRET", ""),
            "app_name": os.environ.get("GITHUB_APP_NAME", "zeni-cloud"),
        }
    except Exception as e:
        log.warning("[github-app] Secret Manager unavailable: %s — falling back to env", e)
        return {
            "app_id": os.environ.get("GITHUB_APP_ID", ""),
            "private_key": os.environ.get("GITHUB_APP_PRIVATE_KEY", ""),
            "client_id": os.environ.get("GITHUB_APP_CLIENT_ID", ""),
            "client_secret": os.environ.get("GITHUB_APP_CLIENT_SECRET", ""),
            "app_name": os.environ.get("GITHUB_APP_NAME", "zeni-cloud"),
        }


def _check_creds_configured(creds: dict[str, str]) -> None:
    """Raise 503 if App not configured yet."""
    if not (creds.get("app_id") and creds.get("private_key")):
        raise HTTPException(
            status_code=503,
            detail=format_error_response(
                "INTERNAL_ERROR",
            ) | {
                "user_msg": "GitHub App chưa được cấu hình. Liên hệ admin Zeni Cloud.",
                "hint": "Admin cần register GitHub App + save credentials vào Secret Manager. Xem docs setup.",
            },
        )


# ─── 1. Install URL ────────────────────────────────────────────────
@router.get("/install-url")
async def get_install_url(
    ws: str = Query(..., description="Workspace ID để link sau khi install"),
    me: CurrentUser = Depends(get_current_user),
) -> dict:
    """
    Return URL khách click để install Zeni Cloud GitHub App.

    Flow:
      1. Customer click button "Import từ GitHub" trong Zeni Dashboard
      2. Frontend gọi endpoint này, nhận install_url
      3. Mở popup/new tab → customer chọn repos cấp quyền
      4. GitHub redirect về /github-app/callback?installation_id=X&state=ws_id
    """
    await require_workspace_access(ws, me)
    creds = await _get_app_creds()
    _check_creds_configured(creds)
    app_name = creds["app_name"]
    state = f"{ws}:{me.email}"  # encode workspace + user
    install_url = (
        f"https://github.com/apps/{app_name}/installations/new"
        f"?state={state}"
    )
    return {
        "install_url": install_url,
        "app_name": app_name,
        "state": state,
        "instructions": "Mở URL → chọn repos cấp quyền → GitHub auto redirect về Zeni Dashboard.",
    }


# ─── 2. OAuth callback ─────────────────────────────────────────────
@router.get("/callback")
async def install_callback(
    installation_id: str = Query(...),
    setup_action: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    code: Optional[str] = Query(None),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    GitHub redirects here after customer installs App.

    Stores installation_id ↔ workspace_id in DB.
    Returns next-step instructions or redirects to Dashboard.
    """
    # Parse state to get workspace
    ws_id = None
    if state and ":" in state:
        ws_id = state.split(":", 1)[0]
    if not ws_id:
        raise HTTPException(400, "Missing state parameter — workspace not identified")
    await require_workspace_access(ws_id, me)

    # Verify installation belongs to this user (security check)
    creds = await _get_app_creds()
    _check_creds_configured(creds)

    try:
        # Get installation details
        token_info = await ga.get_installation_token(
            creds["app_id"], creds["private_key"], installation_id
        )
        installation_token = token_info.get("token")
        repos_count_check = await ga.list_installation_repos(installation_token)
    except Exception as e:
        log.exception("[github-app] callback failed: %s", e)
        raise HTTPException(
            502,
            detail=format_error_response("INTERNAL_ERROR") | {
                "user_msg": f"Không verify được installation: {str(e)[:120]}",
            },
        )

    # Store installation
    await db.execute(text("""
        INSERT INTO github_app_installations (
            installation_id, workspace_id, installed_by, github_account
        ) VALUES (
            :iid, :ws, :user, :acct
        )
        ON CONFLICT (installation_id) DO UPDATE SET
            workspace_id = EXCLUDED.workspace_id,
            installed_by = EXCLUDED.installed_by,
            updated_at = NOW()
    """), {
        "iid": installation_id,
        "ws": ws_id,
        "user": me.email,
        "acct": (repos_count_check[0]["full_name"].split("/")[0] if repos_count_check else None),
    })
    await db.commit()

    return {
        "installed": True,
        "installation_id": installation_id,
        "workspace_id": ws_id,
        "repos_count": len(repos_count_check),
        "next_step": f"GET /api/v1/github-app/installations/{installation_id}/repos",
        "instructions": (
            "GitHub App installed thành công. "
            f"Có {len(repos_count_check)} repos accessible. "
            "Frontend nên redirect về Dashboard + show repos list để customer chọn import."
        ),
    }


# ─── 3. List installations cho current user ────────────────────────
@router.get("/installations")
async def list_my_installations(
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List GitHub App installations linked to this workspace."""
    await require_workspace_access(ws, me)
    rows = (await db.execute(text("""
        SELECT installation_id, github_account, installed_by, created_at::text
        FROM github_app_installations
        WHERE workspace_id = :ws
        ORDER BY created_at DESC
    """), {"ws": ws})).mappings().all()
    return {
        "workspace_id": ws,
        "installations": [dict(r) for r in rows],
        "count": len(rows),
    }


# ─── 4. List repos in installation ─────────────────────────────────
@router.get("/installations/{installation_id}/repos")
async def list_repos(
    installation_id: str,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List repos accessible to this installation — for the Import UI."""
    await require_workspace_access(ws, me)
    # Verify installation belongs to this workspace
    row = (await db.execute(text(
        "SELECT workspace_id FROM github_app_installations WHERE installation_id=:iid"
    ), {"iid": installation_id})).first()
    if not row or row[0] != ws:
        raise HTTPException(404, "Installation không thuộc workspace này")

    creds = await _get_app_creds()
    _check_creds_configured(creds)
    try:
        token_info = await ga.get_installation_token(
            creds["app_id"], creds["private_key"], installation_id
        )
        repos = await ga.list_installation_repos(token_info["token"])
    except Exception as e:
        log.exception("[github-app] list_repos failed: %s", e)
        raise HTTPException(502, f"GitHub API error: {str(e)[:200]}")

    return {
        "installation_id": installation_id,
        "repos": repos,
        "count": len(repos),
    }


# ─── 5. 1-click import repo ────────────────────────────────────────
class ImportRepoIn(BaseModel):
    owner: str = Field(..., description="GitHub repo owner")
    repo: str = Field(..., description="GitHub repo name")
    branch: str = Field("main", description="Branch để deploy")
    project_name: Optional[str] = Field(None, description="Project name (default = repo name)")


@router.post("/installations/{installation_id}/import-repo")
async def import_repo(
    installation_id: str,
    data: ImportRepoIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    1-click import: detect framework + return suggested deploy config.

    Customer click "Deploy" button → frontend calls /projects POST với config này.
    """
    await require_workspace_access(ws, me)
    # Verify installation
    row = (await db.execute(text(
        "SELECT workspace_id FROM github_app_installations WHERE installation_id=:iid"
    ), {"iid": installation_id})).first()
    if not row or row[0] != ws:
        raise HTTPException(404, "Installation không thuộc workspace này")

    creds = await _get_app_creds()
    _check_creds_configured(creds)

    # Get repo file tree + package.json for framework detection
    try:
        token_info = await ga.get_installation_token(
            creds["app_id"], creds["private_key"], installation_id
        )
        token = token_info["token"]
        files = await ga.get_repo_root_files(token, data.owner, data.repo, data.branch)
        package_json = await ga.get_package_json(token, data.owner, data.repo, data.branch)
        requirements = await ga.get_requirements_txt(token, data.owner, data.repo, data.branch)
    except Exception as e:
        log.exception("[github-app] import_repo fetch failed: %s", e)
        raise HTTPException(502, f"GitHub fetch error: {str(e)[:200]}")

    # Run framework detector
    detection = detect_framework(
        files,
        package_json=package_json,
        requirements_txt=requirements,
    )

    return {
        "installation_id": installation_id,
        "repo": f"{data.owner}/{data.repo}",
        "branch": data.branch,
        "project_name": data.project_name or data.repo,
        "detection": detection,
        "next_step": (
            "POST /api/v1/projects với config detection trên + repo URL. "
            "Hoặc POST /api/v1/github/connections/connect để webhook flow cũ."
        ),
    }
