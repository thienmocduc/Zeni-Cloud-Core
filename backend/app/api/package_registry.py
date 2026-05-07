"""
Zeni Cloud Core — Package Registry (P1#7 ClawWits).

npm-private + PyPI-private compatible registry, GCS-backed.

Endpoints:
  Native Zeni API (prefix /packages):
    POST   /tokens                          — Generate publish token (CLI auth)
    GET    /tokens                          — List tokens
    DELETE /tokens/{id}                     — Revoke token
    GET    /                                — List packages in workspace
    GET    /{full_name}                     — Package detail (all versions)
    GET    /{full_name}/versions/{version}  — Version detail

  npm registry compat (prefix /npm):
    GET    /-/whoami                        — npm whoami
    GET    /{package}                       — npm package metadata (registry.npmjs.org format)
    GET    /{package}/-/{tarball}           — Tarball download
    PUT    /{package}                       — npm publish (PUT with body)

  PyPI compat (prefix /pypi):
    GET    /simple/                         — Simple index
    GET    /simple/{package}/               — Package simple index (HTML)
    POST   /                                — Twine upload (multipart)
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Header, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db, SessionLocal

log = logging.getLogger("zeni.pkg_registry")

# Two routers: /packages (Zeni-native), /npm (npm-compat)
router = APIRouter(prefix="/packages", tags=["package-registry"])
npm_router = APIRouter(prefix="/npm", tags=["npm-registry"])
pypi_router = APIRouter(prefix="/pypi", tags=["pypi-registry"])

GCS_BUCKET = "zeni-pkg-registry"
GCP_PROJECT = "zeni-cloud-core"


# ===== Schemas =====

class TokenCreate(BaseModel):
    name: str = Field(..., max_length=120)
    scopes: list[str] = Field(default_factory=lambda: ["read", "write"])
    expires_in_days: Optional[int] = Field(None, ge=1, le=730)


class TokenOut(BaseModel):
    id: str
    name: str
    token_prefix: str
    scopes: list[str]
    expires_at: Optional[str] = None
    last_used_at: Optional[str] = None
    use_count: int
    created_at: str


class TokenWithSecret(TokenOut):
    token: str  # full token, only returned at creation


class PackageOut(BaseModel):
    full_name: str
    registry_type: str
    description: Optional[str] = None
    latest_version: Optional[str] = None
    total_versions: int
    total_downloads: int
    visibility: str
    created_at: str


# ===== Helpers =====

def _hash_token(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


async def _verify_publish_token(
    db: AsyncSession,
    authorization: Optional[str],
) -> Optional[dict]:
    """Verify pkg publish token from Authorization header. Returns token row or None."""
    if not authorization:
        return None
    # npm uses "Bearer ...", pip uses "Basic base64(user:pass)"
    token = None
    if authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    elif authorization.lower().startswith("basic "):
        import base64
        try:
            decoded = base64.b64decode(authorization.split(" ", 1)[1]).decode()
            user, pwd = decoded.split(":", 1)
            token = pwd  # pip sends token as password
        except Exception:
            return None
    if not token or not token.startswith("zeni_pkg_"):
        return None
    th = _hash_token(token)
    r = (await db.execute(text(
        "SELECT id, workspace_id, user_id, scopes, expires_at FROM pkg_publish_tokens "
        "WHERE token_hash = :h"
    ), {"h": th})).mappings().first()
    if not r:
        return None
    if r["expires_at"] and r["expires_at"] < datetime.now(timezone.utc):
        return None
    # Update last_used
    await db.execute(text(
        "UPDATE pkg_publish_tokens SET last_used_at = NOW(), use_count = use_count + 1 WHERE id = :id"
    ), {"id": str(r["id"])})
    await db.commit()
    return dict(r)


async def _get_gcs_token() -> str:
    from google.auth import default as google_auth_default
    from google.auth.transport.requests import Request as GAR
    creds, _ = google_auth_default(scopes=["https://www.googleapis.com/auth/devstorage.read_write"])
    creds.refresh(GAR())
    return creds.token


async def _upload_tarball_to_gcs(object_name: str, content: bytes, mime: str = "application/octet-stream") -> None:
    token = await _get_gcs_token()
    # Ensure bucket exists
    async with httpx.AsyncClient(timeout=30.0) as client:
        cb = await client.post(
            f"https://storage.googleapis.com/storage/v1/b?project={GCP_PROJECT}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"name": GCS_BUCKET, "location": "ASIA-SOUTHEAST1"},
        )
        # ignore 409 (exists)
    async with httpx.AsyncClient(timeout=300.0) as client:
        r = await client.post(
            f"https://storage.googleapis.com/upload/storage/v1/b/{GCS_BUCKET}/o",
            params={"uploadType": "media", "name": object_name},
            headers={"Authorization": f"Bearer {token}", "Content-Type": mime},
            content=content,
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"GCS upload failed: {r.status_code}")


# ===== Native Zeni API — Tokens =====

@router.post("/tokens", response_model=TokenWithSecret, status_code=201)
async def create_token(
    data: TokenCreate,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    """Generate publish token. Token shown ONCE at creation."""
    await require_workspace_access(ws, me)
    raw_token = "zeni_pkg_" + secrets.token_urlsafe(40)
    th = _hash_token(raw_token)
    prefix = raw_token[:24]

    expires_at = None
    if data.expires_in_days:
        from datetime import timedelta
        expires_at = datetime.now(timezone.utc) + timedelta(days=data.expires_in_days)

    tok_id = uuid.uuid4()
    await db.execute(text(
        "INSERT INTO pkg_publish_tokens (id, workspace_id, user_id, token_hash, token_prefix, "
        "name, scopes, expires_at) "
        "VALUES (:id, :ws, :uid, :h, :pf, :n, CAST(:s AS jsonb), :ex)"
    ), {
        "id": str(tok_id),
        "ws": ws,
        "uid": str(me.id) if me else None,
        "h": th,
        "pf": prefix,
        "n": data.name,
        "s": json.dumps(data.scopes),
        "ex": expires_at,
    })
    await db.commit()

    return TokenWithSecret(
        id=str(tok_id),
        name=data.name,
        token=raw_token,
        token_prefix=prefix,
        scopes=data.scopes,
        expires_at=expires_at.isoformat() if expires_at else None,
        last_used_at=None,
        use_count=0,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


@router.get("/tokens", response_model=list[TokenOut])
async def list_tokens(
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    rows = (await db.execute(text(
        "SELECT id, name, token_prefix, scopes, expires_at, last_used_at, use_count, created_at "
        "FROM pkg_publish_tokens WHERE workspace_id = :ws ORDER BY created_at DESC"
    ), {"ws": ws})).mappings().all()
    return [
        TokenOut(
            id=str(r["id"]),
            name=r["name"],
            token_prefix=r["token_prefix"],
            scopes=r["scopes"] if isinstance(r["scopes"], list) else json.loads(r["scopes"] or "[]"),
            expires_at=r["expires_at"].isoformat() if r["expires_at"] else None,
            last_used_at=r["last_used_at"].isoformat() if r["last_used_at"] else None,
            use_count=r["use_count"] or 0,
            created_at=r["created_at"].isoformat() if r["created_at"] else "",
        )
        for r in rows
    ]


@router.delete("/tokens/{token_id}", status_code=204)
async def revoke_token(
    token_id: str,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    await db.execute(text(
        "DELETE FROM pkg_publish_tokens WHERE id = :id AND workspace_id = :ws"
    ), {"id": token_id, "ws": ws})
    await db.commit()


@router.get("/", response_model=list[PackageOut])
async def list_packages(
    ws: str = Query(...),
    registry_type: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    sql = (
        "SELECT full_name, registry_type, description, latest_version, total_versions, "
        "total_downloads, visibility, created_at FROM pkg_packages WHERE workspace_id = :ws"
    )
    params: dict[str, Any] = {"ws": ws}
    if registry_type:
        sql += " AND registry_type = :rt"
        params["rt"] = registry_type
    sql += " ORDER BY full_name"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return [
        PackageOut(
            full_name=r["full_name"],
            registry_type=r["registry_type"],
            description=r["description"],
            latest_version=r["latest_version"],
            total_versions=r["total_versions"] or 0,
            total_downloads=r["total_downloads"] or 0,
            visibility=r["visibility"],
            created_at=r["created_at"].isoformat() if r["created_at"] else "",
        )
        for r in rows
    ]


@router.get("/registry-info")
async def registry_info():
    """Public endpoint — registry config for CLI users."""
    return {
        "npm_registry_url": "https://zenicloud.io/api/v1/npm/",
        "pypi_simple_url": "https://zenicloud.io/api/v1/pypi/simple/",
        "publish_npm": "npm publish --registry=https://zenicloud.io/api/v1/npm/",
        "publish_pypi": "twine upload --repository-url=https://zenicloud.io/api/v1/pypi/ dist/*",
        "auth_setup_npm": "npm config set //zenicloud.io/api/v1/npm/:_authToken=<zeni_pkg_token>",
        "auth_setup_pypi": "[~/.pypirc] index-servers = zeni\\n[zeni]\\nrepository = https://zenicloud.io/api/v1/pypi/\\nusername = __token__\\npassword = <zeni_pkg_token>",
    }


# ===== npm-compat =====

@npm_router.get("/-/whoami")
async def npm_whoami(
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    tok = await _verify_publish_token(db, authorization)
    if not tok:
        raise HTTPException(401, "Invalid or missing token")
    return {"username": f"zeni-{tok['workspace_id']}"}


@npm_router.get("/{package_name:path}")
async def npm_metadata(
    package_name: str,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Return package metadata in npm registry format."""
    # Handle scoped @scope/name (URL-encoded as %40scope%2Fname or raw)
    package_name = package_name.replace("%40", "@").replace("%2f", "/")
    pkg = (await db.execute(text(
        "SELECT id, full_name, description, latest_version FROM pkg_packages "
        "WHERE registry_type = 'npm' AND full_name = :n"
    ), {"n": package_name})).mappings().first()
    if not pkg:
        raise HTTPException(404, "Package not found")

    versions = (await db.execute(text(
        "SELECT version, package_json, sha512_b64, size_bytes, gcs_path "
        "FROM pkg_versions WHERE package_id = :pid AND yanked = FALSE ORDER BY published_at"
    ), {"pid": str(pkg["id"])})).mappings().all()

    versions_obj = {}
    for v in versions:
        meta = v["package_json"] if isinstance(v["package_json"], dict) else (json.loads(v["package_json"]) if v["package_json"] else {})
        meta["dist"] = {
            "tarball": f"https://zenicloud.io/api/v1/npm/{package_name}/-/{package_name.split('/')[-1]}-{v['version']}.tgz",
            "shasum": v["sha512_b64"] or "",
            "integrity": f"sha512-{v['sha512_b64']}" if v["sha512_b64"] else "",
        }
        versions_obj[v["version"]] = meta

    dist_tags_rows = (await db.execute(text(
        "SELECT tag_name, version FROM pkg_dist_tags WHERE package_id = :pid"
    ), {"pid": str(pkg["id"])})).mappings().all()
    dist_tags = {r["tag_name"]: r["version"] for r in dist_tags_rows}
    if pkg["latest_version"] and "latest" not in dist_tags:
        dist_tags["latest"] = pkg["latest_version"]

    return {
        "name": package_name,
        "description": pkg["description"] or "",
        "dist-tags": dist_tags,
        "versions": versions_obj,
    }


@npm_router.put("/{package_name:path}")
async def npm_publish(
    package_name: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Receive npm publish (PUT with JSON body containing _attachments tarball)."""
    tok = await _verify_publish_token(db, authorization)
    if not tok:
        raise HTTPException(401, "Invalid or missing token")
    if "write" not in (tok.get("scopes") if isinstance(tok.get("scopes"), list) else json.loads(tok.get("scopes") or "[]")):
        raise HTTPException(403, "Token lacks 'write' scope")

    package_name = package_name.replace("%40", "@").replace("%2f", "/")
    body = await request.json()

    # Extract version + tarball
    versions = body.get("versions", {})
    attachments = body.get("_attachments", {})
    if not versions or not attachments:
        raise HTTPException(422, "Missing versions or _attachments in body")

    version_str = list(versions.keys())[0]
    version_meta = versions[version_str]
    tarball_filename = list(attachments.keys())[0]
    tarball_b64 = attachments[tarball_filename]["data"]

    import base64
    tarball_bytes = base64.b64decode(tarball_b64)
    sha512 = hashlib.sha512(tarball_bytes).digest()
    sha512_b64 = base64.b64encode(sha512).decode()

    ws = tok["workspace_id"]
    scope = None
    name_part = package_name
    if package_name.startswith("@"):
        scope, name_part = package_name.split("/", 1)

    # Upsert package
    pkg_row = (await db.execute(text(
        "INSERT INTO pkg_packages (workspace_id, registry_type, scope, name, full_name, "
        "description, latest_version, created_by) "
        "VALUES (:ws, 'npm', :sc, :np, :fn, :de, :v, :cb) "
        "ON CONFLICT (registry_type, full_name) DO UPDATE SET "
        "description = COALESCE(EXCLUDED.description, pkg_packages.description), "
        "latest_version = EXCLUDED.latest_version, "
        "total_versions = pkg_packages.total_versions + 1, updated_at = NOW() "
        "RETURNING id"
    ), {
        "ws": ws,
        "sc": scope,
        "np": name_part,
        "fn": package_name,
        "de": version_meta.get("description"),
        "v": version_str,
        "cb": str(tok.get("user_id")) if tok.get("user_id") else None,
    })).mappings().first()
    pkg_id = pkg_row["id"]

    # Upload tarball to GCS
    gcs_object = f"npm/{package_name}/-/{tarball_filename}"
    await _upload_tarball_to_gcs(gcs_object, tarball_bytes, "application/octet-stream")

    # Insert version
    await db.execute(text(
        "INSERT INTO pkg_versions (package_id, workspace_id, version, filename, gcs_path, "
        "size_bytes, sha512_b64, package_json, dependencies, dev_dependencies, peer_dependencies, "
        "published_by) VALUES (:pid, :ws, :v, :fn, :gp, :sz, :h, CAST(:pj AS jsonb), "
        "CAST(:de AS jsonb), CAST(:dd AS jsonb), CAST(:pd AS jsonb), :pb) "
        "ON CONFLICT (package_id, version) DO NOTHING"
    ), {
        "pid": str(pkg_id),
        "ws": ws,
        "v": version_str,
        "fn": tarball_filename,
        "gp": f"gs://{GCS_BUCKET}/{gcs_object}",
        "sz": len(tarball_bytes),
        "h": sha512_b64,
        "pj": json.dumps(version_meta),
        "de": json.dumps(version_meta.get("dependencies", {})),
        "dd": json.dumps(version_meta.get("devDependencies", {})),
        "pd": json.dumps(version_meta.get("peerDependencies", {})),
        "pb": str(tok.get("user_id")) if tok.get("user_id") else None,
    })

    # Update dist-tag latest
    await db.execute(text(
        "INSERT INTO pkg_dist_tags (package_id, tag_name, version) VALUES (:pid, 'latest', :v) "
        "ON CONFLICT (package_id, tag_name) DO UPDATE SET version = :v, updated_at = NOW()"
    ), {"pid": str(pkg_id), "v": version_str})
    await db.commit()

    return {"ok": True, "id": package_name, "rev": f"{version_str}-{secrets.token_hex(8)}"}


# ===== pypi-compat =====

@pypi_router.get("/simple/", response_class=HTMLResponse)
async def pypi_simple_index(
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """PyPI Simple Index (PEP 503)."""
    tok = await _verify_publish_token(db, authorization)
    if not tok:
        raise HTTPException(401, "Invalid token")
    rows = (await db.execute(text(
        "SELECT full_name FROM pkg_packages WHERE registry_type = 'pypi' AND workspace_id = :ws"
    ), {"ws": tok["workspace_id"]})).mappings().all()
    links = "\n".join(f'    <a href="{r["full_name"]}/">{r["full_name"]}</a>' for r in rows)
    return HTMLResponse(f"<!DOCTYPE html><html><body>\n{links}\n</body></html>")


@pypi_router.get("/simple/{package_name}/", response_class=HTMLResponse)
async def pypi_package_simple(
    package_name: str,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    tok = await _verify_publish_token(db, authorization)
    if not tok:
        raise HTTPException(401, "Invalid token")
    pkg = (await db.execute(text(
        "SELECT id FROM pkg_packages WHERE registry_type = 'pypi' AND full_name = :n AND workspace_id = :ws"
    ), {"n": package_name, "ws": tok["workspace_id"]})).mappings().first()
    if not pkg:
        raise HTTPException(404, "Package not found")
    versions = (await db.execute(text(
        "SELECT version, filename, sha256_hex FROM pkg_versions WHERE package_id = :pid AND yanked = FALSE"
    ), {"pid": str(pkg["id"])})).mappings().all()
    links = "\n".join(
        f'    <a href="https://zenicloud.io/api/v1/pypi/packages/{package_name}/{v["filename"]}#sha256={v["sha256_hex"]}">{v["filename"]}</a>'
        for v in versions
    )
    return HTMLResponse(f"<!DOCTYPE html><html><body>\n{links}\n</body></html>")


@pypi_router.post("/")
async def pypi_upload(
    request: Request,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Twine upload endpoint (multipart/form-data)."""
    tok = await _verify_publish_token(db, authorization)
    if not tok:
        raise HTTPException(401, "Invalid token")
    form = await request.form()
    name = form.get("name")
    version = form.get("version")
    summary = form.get("summary", "")
    file = form.get("content")
    if not (name and version and file):
        raise HTTPException(422, "Missing name, version, or content")

    content = await file.read()
    sha256 = hashlib.sha256(content).hexdigest()
    md5 = hashlib.md5(content).hexdigest()
    ws = tok["workspace_id"]

    pkg_row = (await db.execute(text(
        "INSERT INTO pkg_packages (workspace_id, registry_type, name, full_name, description, "
        "latest_version, created_by) "
        "VALUES (:ws, 'pypi', :n, :n, :d, :v, :cb) "
        "ON CONFLICT (registry_type, full_name) DO UPDATE SET "
        "latest_version = EXCLUDED.latest_version, total_versions = pkg_packages.total_versions + 1 "
        "RETURNING id"
    ), {"ws": ws, "n": name, "d": summary, "v": version, "cb": str(tok.get("user_id")) if tok.get("user_id") else None})).mappings().first()
    pkg_id = pkg_row["id"]

    gcs_object = f"pypi/{name}/{file.filename}"
    await _upload_tarball_to_gcs(gcs_object, content)

    await db.execute(text(
        "INSERT INTO pkg_versions (package_id, workspace_id, version, filename, gcs_path, "
        "size_bytes, sha256_hex, md5_hex, published_by) "
        "VALUES (:pid, :ws, :v, :fn, :gp, :sz, :sh, :md, :pb) "
        "ON CONFLICT (package_id, version) DO NOTHING"
    ), {
        "pid": str(pkg_id), "ws": ws, "v": version, "fn": file.filename,
        "gp": f"gs://{GCS_BUCKET}/{gcs_object}", "sz": len(content),
        "sh": sha256, "md": md5,
        "pb": str(tok.get("user_id")) if tok.get("user_id") else None,
    })
    await db.commit()
    return Response(status_code=200)
