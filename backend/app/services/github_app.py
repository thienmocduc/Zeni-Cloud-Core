"""
GitHub App Integration (Phase 1 P1.1 — chairman approved 2026-05-11)

Mục đích: thay vì khách phải tự add webhook + secret vào GitHub repo Settings,
khách install "Zeni Cloud" GitHub App 1 lần → mọi repo của họ tự auto-deploy
qua Zeni.

Pattern lấy cảm hứng:
  - Vercel GitHub App
  - Cloudflare Pages GitHub integration
  - Railway GitHub auto-deploy

Setup steps (admin one-time):
  1. Create GitHub App tại https://github.com/settings/apps/new
     - Name: Zeni Cloud
     - Homepage URL: https://zenicloud.io
     - Callback URL: https://zenicloud.io/api/v1/github-app/callback
     - Webhook URL: https://zenicloud.io/api/v1/github-app/webhook
     - Permissions:
         Contents: Read
         Metadata: Read
         Pull requests: Read
         Webhooks: Read & Write
         Deployments: Read & Write
     - Subscribe events: push, pull_request, deployment_status
  2. Save App ID + Private Key in Secret Manager:
     - GITHUB_APP_ID
     - GITHUB_APP_PRIVATE_KEY (PEM)
     - GITHUB_APP_WEBHOOK_SECRET
     - GITHUB_APP_CLIENT_ID
     - GITHUB_APP_CLIENT_SECRET

Customer flow:
  1. Customer click "Import từ GitHub" trong Zeni Dashboard
  2. Redirect → https://github.com/apps/zeni-cloud/installations/new
  3. Customer chọn repos để grant access
  4. Callback → /github-app/callback?installation_id=XXX
  5. Zeni stores installation_id ↔ workspace_id
  6. Customer thấy danh sách repos → click 1 → Zeni auto-detect framework + deploy

KHÔNG đụng github_integration.py cũ — file mới hoàn toàn. Endpoints mới
sẽ ở `/github-app/*` (vs `/github/*` cũ giữ webhook secret manual flow).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

log = logging.getLogger("zeni.github_app")


GITHUB_API_BASE = "https://api.github.com"


# ─── JWT generation cho GitHub App ────────────────────────────────────
def _generate_app_jwt(app_id: str, private_key_pem: str) -> str:
    """
    Generate a JWT signed with the GitHub App private key (RS256).
    Used to authenticate as the App (not as install) for /app endpoints.

    JWT lasts 10 min max per GitHub docs.
    """
    try:
        import jwt as pyjwt  # PyJWT package
    except ImportError:
        raise RuntimeError("PyJWT package required for GitHub App auth")

    now = int(time.time())
    payload = {
        "iat": now - 60,  # backdate 1 min to avoid clock skew issues
        "exp": now + 540,  # 9 min (max 10 min)
        "iss": str(app_id),
    }
    return pyjwt.encode(payload, private_key_pem, algorithm="RS256")


# ─── Installation token ────────────────────────────────────────────
async def get_installation_token(
    app_id: str,
    private_key_pem: str,
    installation_id: str,
) -> dict[str, Any]:
    """
    Exchange App JWT for an installation access token.

    Token lasts 1 hour. Caller should cache + refresh on 401.

    Returns:
      {
        "token": "ghs_xxx",
        "expires_at": "2026-05-11T18:00:00Z",
        "permissions": {...},
        "repository_selection": "selected" | "all",
      }
    """
    app_jwt = _generate_app_jwt(app_id, private_key_pem)
    url = f"{GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


# ─── List repos for installation ───────────────────────────────────
async def list_installation_repos(installation_token: str) -> list[dict[str, Any]]:
    """
    List all repos accessible to this installation.

    Returns: list of repo dicts with name, full_name, default_branch, etc.
    """
    url = f"{GITHUB_API_BASE}/installation/repositories?per_page=100"
    headers = {
        "Authorization": f"Bearer {installation_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    repos = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        while url:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            for r in data.get("repositories", []):
                repos.append({
                    "id": r["id"],
                    "name": r["name"],
                    "full_name": r["full_name"],
                    "private": r.get("private", False),
                    "default_branch": r.get("default_branch", "main"),
                    "language": r.get("language"),
                    "description": r.get("description"),
                    "html_url": r.get("html_url"),
                    "pushed_at": r.get("pushed_at"),
                })
            # Pagination
            link = resp.headers.get("Link", "")
            url = _parse_next_link(link)
    return repos


def _parse_next_link(link_header: str) -> Optional[str]:
    """Extract <url>; rel="next" from GitHub Link header."""
    for part in (link_header or "").split(","):
        if 'rel="next"' in part:
            url = part.split(";")[0].strip()
            return url.strip("<>")
    return None


# ─── Get repo file tree (for framework detection) ──────────────────
async def get_repo_root_files(
    installation_token: str,
    owner: str,
    repo: str,
    branch: str = "main",
) -> list[str]:
    """
    Fetch list of file names at the root of repo's default branch.

    Used by framework_detector to identify framework without cloning.
    """
    # First get the tree SHA of the branch
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/trees/{branch}?recursive=0"
    headers = {
        "Authorization": f"Bearer {installation_token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        tree = resp.json().get("tree", [])
        return [t["path"] for t in tree if t.get("type") == "blob"]


# ─── Get package.json content ──────────────────────────────────────
async def get_package_json(
    installation_token: str,
    owner: str,
    repo: str,
    branch: str = "main",
) -> Optional[dict]:
    """Fetch + parse package.json content from repo root."""
    return await _get_json_file(installation_token, owner, repo, branch, "package.json")


async def get_requirements_txt(
    installation_token: str,
    owner: str,
    repo: str,
    branch: str = "main",
) -> Optional[list[str]]:
    """Fetch + split requirements.txt lines from repo root."""
    content = await _get_file_content(installation_token, owner, repo, branch, "requirements.txt")
    if content is None:
        return None
    return [line.strip() for line in content.splitlines() if line.strip() and not line.startswith("#")]


async def _get_file_content(
    installation_token: str,
    owner: str,
    repo: str,
    branch: str,
    path: str,
) -> Optional[str]:
    """Fetch raw text content of a file from repo."""
    import base64
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{path}?ref={branch}"
    headers = {
        "Authorization": f"Bearer {installation_token}",
        "Accept": "application/vnd.github+json",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            if data.get("encoding") == "base64" and data.get("content"):
                return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            return None
    except Exception as e:
        log.warning("[github_app] _get_file_content %s/%s/%s failed: %s",
                    owner, repo, path, e)
        return None


async def _get_json_file(
    installation_token: str,
    owner: str,
    repo: str,
    branch: str,
    path: str,
) -> Optional[dict]:
    """Fetch + parse JSON file from repo."""
    import json as _json
    content = await _get_file_content(installation_token, owner, repo, branch, path)
    if not content:
        return None
    try:
        return _json.loads(content)
    except _json.JSONDecodeError:
        return None


# ─── OAuth user identity (when customer logs in via GitHub) ────────
async def exchange_code_for_user_token(
    client_id: str,
    client_secret: str,
    code: str,
) -> dict[str, Any]:
    """
    OAuth code exchange → user access token.

    Used when customer clicks "Login with GitHub" or "Connect GitHub" in
    Zeni Dashboard, GitHub redirects with ?code=... → we exchange for user token.

    Returns: {"access_token": "gho_xxx", "token_type": "bearer", "scope": "..."}
    """
    url = "https://github.com/login/oauth/access_token"
    headers = {"Accept": "application/json"}
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, headers=headers, data=data)
        resp.raise_for_status()
        return resp.json()


async def get_user_info(user_token: str) -> dict[str, Any]:
    """Get GitHub user info using a user OAuth token."""
    url = f"{GITHUB_API_BASE}/user"
    headers = {
        "Authorization": f"Bearer {user_token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


# ─── List installations for user ───────────────────────────────────
async def list_user_installations(user_token: str) -> list[dict[str, Any]]:
    """
    List GitHub App installations accessible to this user.
    Used to find their installation_id after they install the Zeni Cloud App.
    """
    url = f"{GITHUB_API_BASE}/user/installations"
    headers = {
        "Authorization": f"Bearer {user_token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json().get("installations", [])
