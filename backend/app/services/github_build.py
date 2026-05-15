"""
Zeni Cloud Core — GitHub Build Worker (Phase 2).

Khi GitHub webhook fire → background task này pickup queued deploy:
1. Clone repo (public, hoặc private với access_token)
2. Generate Dockerfile if missing (dùng framework templates)
3. Submit Cloud Build với GitHub Source
4. Wait → push image to Artifact Registry
5. Auto-deploy to Cloud Run via cloud_run service
6. Update github_deploys.status='success' + deploy_url
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from google.auth import default as google_auth_default
from google.auth.transport.requests import Request as GoogleAuthRequest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("zeni.github_build")

GCP_PROJECT = "zeni-cloud-core"
ARTIFACT_REGISTRY = "us-central1-docker.pkg.dev/zeni-cloud-core/zeni-images"


def _get_auth_token() -> str:
    creds, _ = google_auth_default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(GoogleAuthRequest())
    return creds.token


async def submit_github_build(repo_owner: str, repo_name: str, branch: str,
                               image_tag: str, framework: str,
                               access_token: str | None = None) -> dict[str, Any]:
    """
    Submit Cloud Build với GitHub source.
    Cloud Build natively supports gitSource (no need to clone manually).
    """
    token = _get_auth_token()
    repo_url = f"https://github.com/{repo_owner}/{repo_name}"

    # If private repo, use access_token in URL
    if access_token:
        repo_url_with_auth = f"https://x-access-token:{access_token}@github.com/{repo_owner}/{repo_name}"
    else:
        repo_url_with_auth = repo_url

    # Build config: clone via git, generate Dockerfile if needed, build, push
    dockerfile_steps = []
    if framework != "custom":
        # Generate Dockerfile from template if repo doesn't have one
        from app.services.source_build import _generate_dockerfile
        dockerfile_content = _generate_dockerfile(framework, port=8080)
        dockerfile_steps = [{
            "name": "ubuntu",
            "entrypoint": "bash",
            "args": ["-c", f"if [ ! -f Dockerfile ]; then echo '{dockerfile_content}' > Dockerfile; fi"]
        }]

    build_config = {
        "steps": [
            # Step 1: Clone repo
            {
                "name": "gcr.io/cloud-builders/git",
                "args": ["clone", "--depth=1", "--branch", branch, repo_url_with_auth, "."],
            },
            # Step 2: Auto-generate Dockerfile (if missing)
            *dockerfile_steps,
            # Step 3: Docker build
            {
                "name": "gcr.io/cloud-builders/docker",
                "args": ["build", "-t", image_tag, "."],
            },
            # Step 4: Push image
            {
                "name": "gcr.io/cloud-builders/docker",
                "args": ["push", image_tag],
            },
        ],
        "images": [image_tag],
        "timeout": "900s",  # 15 minutes
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"https://cloudbuild.googleapis.com/v1/projects/{GCP_PROJECT}/builds",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=build_config,
        )
        r.raise_for_status()
        return r.json()


async def poll_build(build_id: str, max_wait_sec: int = 900) -> dict[str, Any]:
    token = _get_auth_token()
    elapsed = 0
    async with httpx.AsyncClient(timeout=15.0) as client:
        while elapsed < max_wait_sec:
            r = await client.get(
                f"https://cloudbuild.googleapis.com/v1/projects/{GCP_PROJECT}/builds/{build_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            data = r.json()
            status = data.get("status", "PENDING")
            if status in ("SUCCESS", "FAILURE", "CANCELLED", "TIMEOUT"):
                return data
            await asyncio.sleep(15)
            elapsed += 15
    return {"status": "TIMEOUT"}


async def run_github_build_and_deploy(
    db: AsyncSession,
    deploy_id: int,
    connection_id: int,
    workspace_id: str,
    repo_owner: str,
    repo_name: str,
    branch: str,
    framework: str,
    port: int,
    commit_sha: str = "HEAD",
    access_token: str | None = None,
) -> None:
    """Background task: clone GitHub → build → deploy."""
    project_name = f"gh-{repo_name}"[:48].lower().replace("_", "-")
    image_tag = f"{ARTIFACT_REGISTRY}/zeni-{workspace_id}-{project_name}:{commit_sha[:8]}"

    try:
        await db.execute(
            text("UPDATE github_deploys SET status='building' WHERE id=:id"),
            {"id": deploy_id}
        )
        await db.commit()

        # Submit Cloud Build
        op = await submit_github_build(repo_owner, repo_name, branch, image_tag, framework, access_token)
        meta = op.get("metadata", {}) or {}
        build_meta = meta.get("build", {}) if isinstance(meta, dict) else {}
        build_id = build_meta.get("id") or op.get("name", "").split("/")[-1]

        await db.execute(
            text("UPDATE github_deploys SET build_id=:bid WHERE id=:id"),
            {"bid": build_id, "id": deploy_id}
        )
        await db.commit()

        # Poll
        result = await poll_build(build_id, max_wait_sec=900)
        if result.get("status") != "SUCCESS":
            err = result.get("statusDetail") or f"Build {result.get('status')}"
            await db.execute(
                text("UPDATE github_deploys SET status='failed', error_message=:e, completed_at=NOW() WHERE id=:id"),
                {"e": err[:500], "id": deploy_id}
            )
            await db.commit()
            return

        # Deploy to Cloud Run
        # v168 FIX: function thật là `deploy_service` (đã rename từ `deploy_cloud_run`
        # ở source_build.py v138). github_build.py còn dùng tên cũ → ImportError silent
        # → worker pickup task nhưng exception → task stuck pending (vietcontech bug #9+#10).
        # deploy_service signature: workspace, project_name, image, size, region, env_vars,
        # secrets, port, allow_unauthenticated, created_by. Build params phù hợp.
        #
        # Phase 2 P2.1 (v170): nếu push lên BRANCH KHÁC default_branch → preview deploy
        # với tag = branch slug, 0% traffic. Default branch = production deploy bình thường.
        try:
            from app.services.cloud_run import deploy_service
            sn = f"zeni-{workspace_id}-{project_name}".replace("_", "-")

            # Lookup default_branch của connection
            row = (await db.execute(text(
                "SELECT default_branch FROM github_connections WHERE id=:cid"
            ), {"cid": connection_id})).first()
            default_branch = (row[0] if row else None) or "main"
            is_preview = (branch != default_branch)

            deploy_result = await deploy_service(
                workspace=workspace_id,
                project_name=project_name,
                image=image_tag,
                size="s",
                region="asia-southeast1",
                env_vars={
                    "WORKSPACE": workspace_id,
                    "REPO": f"{repo_owner}/{repo_name}",
                    "BRANCH": branch,
                    "COMMIT_SHA": commit_sha[:12],
                    "DEPLOY_TYPE": "preview" if is_preview else "production",
                },
                secrets={},
                port=port,
                allow_unauthenticated=True,
            )

            # P2.1: nếu preview branch → set traffic tag, no-traffic
            if is_preview:
                try:
                    from app.services.preview_deploys import slugify_branch, _set_preview_tag
                    branch_slug = slugify_branch(branch)
                    await _set_preview_tag(
                        service_name=sn,
                        region="asia-southeast1",
                        tag=branch_slug,
                    )
                    # Compute preview URL
                    base_url = deploy_result.url or f"https://{sn}-asia-southeast1.run.app"
                    deploy_url = base_url.replace("https://", f"https://{branch_slug}---")
                    log.info("[github_build] preview deploy: branch=%s tag=%s url=%s",
                             branch, branch_slug, deploy_url)
                except Exception as e:
                    log.warning("[github_build] preview tag failed (non-fatal): %s", e)
                    deploy_url = deploy_result.url or f"https://{sn}-asia-southeast1.run.app"
            else:
                deploy_url = deploy_result.url or f"https://{sn}-asia-southeast1.run.app"
            await db.execute(
                text("""UPDATE github_deploys SET status='success', completed_at=NOW(),
                        image_url=:img, deploy_url=:url WHERE id=:id"""),
                {"img": image_tag, "url": deploy_url, "id": deploy_id}
            )
            # Update connection's last_deploy + project_id
            await db.execute(
                text("""UPDATE github_connections
                        SET last_deploy_at=NOW(), last_deploy_sha=:sha, last_deploy_status='success'
                        WHERE id=:cid"""),
                {"sha": commit_sha, "cid": connection_id}
            )
            await db.commit()
            log.info("[github_build] deploy %s SUCCESS at %s", deploy_id, deploy_url)
        except Exception as e:
            log.exception("[github_build] %s deploy fail", deploy_id)
            await db.execute(
                text("""UPDATE github_deploys SET status='success', completed_at=NOW(),
                        image_url=:img,
                        error_message='Image built but Cloud Run deploy failed: ' || :err
                        WHERE id=:id"""),
                {"img": image_tag, "err": str(e)[:200], "id": deploy_id}
            )
            await db.commit()

    except Exception as e:
        log.exception("[github_build] %s FAILED", deploy_id)
        try:
            await db.execute(
                text("UPDATE github_deploys SET status='failed', error_message=:e, completed_at=NOW() WHERE id=:id"),
                {"e": f"{type(e).__name__}: {str(e)[:300]}", "id": deploy_id}
            )
            await db.commit()
        except Exception:
            pass
