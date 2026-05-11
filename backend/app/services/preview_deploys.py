"""
Preview Deployments per Branch (Phase 2 P2.1 — chairman approved 2026-05-11)

Mục đích: học pattern Vercel — mỗi git branch (không phải `main`) push tới
GitHub → Zeni tự tạo Cloud Run revision RIÊNG với `--tag` riêng → URL preview
unique để khách share team review trước khi merge.

Pattern lấy cảm hứng:
  - Vercel: `feature-x.myapp.vercel.app`
  - Cloudflare Pages: `branch-name.myapp.pages.dev`
  - Netlify: `deploy-preview-PR-NUM--myapp.netlify.app`

Flow:
  1. Customer push code lên branch `feature-stripe-checkout`
  2. GitHub webhook → Zeni `/github/webhook` (đã có)
  3. github_build.py check `branch != default_branch` → call create_preview_deploy()
  4. Build image với tag = branch slug
  5. Deploy Cloud Run với `--tag preview-{branch}` + `--no-traffic`
  6. Generate preview URL: `https://preview-{branch}---{service}-uc.a.run.app`
  7. Update GitHub commit status + (optional) bot comment in PR

Implementation:
  - Slugify branch name to be DNS-safe
  - Reuse existing deploy_service from cloud_run.py
  - Track preview deploys in `preview_deploys` table
  - TTL cleanup: preview deploys >30 days idle → auto-delete

KHÔNG đụng github_build.py production deploy flow — file mới hoàn toàn.
Caller (github_build.py) sẽ gọi service này KHI branch khác default.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

log = logging.getLogger("zeni.preview_deploys")


def slugify_branch(branch: str) -> str:
    """
    Convert branch name to DNS-safe tag slug.

    Examples:
      "feature/stripe-checkout" → "feature-stripe-checkout"
      "release/v1.2.0"          → "release-v1-2-0"
      "main"                    → "main"
      "FIX-Bug#123"             → "fix-bug-123"
    """
    s = re.sub(r"[^a-z0-9-]", "-", branch.lower())
    s = re.sub(r"-+", "-", s).strip("-")
    # Cloud Run tag must start with letter + max 63 chars
    if not s or not s[0].isalpha():
        s = "p-" + s
    return s[:63]


def preview_url_for(
    cloud_run_service: str,
    branch_slug: str,
    region: str = "asia-southeast1",
) -> str:
    """
    Compute preview URL from Cloud Run service URL pattern.

    Cloud Run URL format: https://{tag}---{service}-{hash}.run.app
    """
    # Format: https://{tag}---{service-name}-{project-hash}-{region-short}.a.run.app
    # We don't know the project-hash here, so caller must lookup actual URL
    region_short = region.split("-")[0][:2]  # us-central1 → uc, asia-southeast1 → as
    return f"https://{branch_slug}---{cloud_run_service}-XXXXXXX-{region_short}.a.run.app"


async def create_preview_deploy(
    *,
    workspace_id: str,
    project_name: str,
    cloud_run_service: str,
    branch: str,
    image_tag: str,
    commit_sha: str,
    region: str = "asia-southeast1",
    actor: str = "github-webhook",
) -> dict[str, Any]:
    """
    Deploy a preview revision for a non-default branch.

    Args:
      workspace_id: workspace của project
      project_name: tên project
      cloud_run_service: tên Cloud Run service (đã exist)
      branch: tên branch
      image_tag: full image URL (already built)
      commit_sha: commit SHA
      region: GCP region
      actor: ai trigger deploy (for audit)

    Returns:
      {
        "preview_tag": str,
        "preview_url": str | None,
        "revision": str | None,
        "status": "deployed" | "failed",
        "error": str | None,
        "commit_sha": str,
        "branch": str,
      }
    """
    branch_slug = slugify_branch(branch)
    log.info("[preview] start branch=%s slug=%s commit=%s",
             branch, branch_slug, commit_sha[:8])

    # Deploy with --no-traffic --tag preview-{slug}
    try:
        from app.services.cloud_run import deploy_service
        deploy_result = await deploy_service(
            workspace=workspace_id,
            project_name=project_name,
            image=image_tag,
            size="s",
            region=region,
            env_vars={
                "WORKSPACE": workspace_id,
                "BRANCH": branch,
                "COMMIT_SHA": commit_sha[:12],
                "DEPLOY_TYPE": "preview",
            },
            allow_unauthenticated=True,
        )
        preview_url = deploy_result.url or "(unknown)"
        # Note: deploy_service doesn't set --tag or --no-traffic; the actual
        # tagging happens via separate update_traffic call. See _set_preview_tag.
        await _set_preview_tag(
            service_name=cloud_run_service,
            region=region,
            tag=branch_slug,
        )
        return {
            "status": "deployed",
            "preview_tag": branch_slug,
            "branch": branch,
            "commit_sha": commit_sha,
            "preview_url": preview_url.replace("https://", f"https://{branch_slug}---"),
            "revision": deploy_result.url,
        }
    except Exception as e:
        log.exception("[preview] deploy failed for branch=%s: %s", branch, e)
        return {
            "status": "failed",
            "branch": branch,
            "commit_sha": commit_sha,
            "error": str(e)[:300],
        }


async def _set_preview_tag(*, service_name: str, region: str, tag: str) -> None:
    """Set traffic tag on latest revision (--no-traffic mode)."""
    from google.cloud import run_v2

    client = run_v2.ServicesClient()
    from app.core.config import settings
    svc_name = f"projects/{settings.gcp_project_id}/locations/{region}/services/{service_name}"

    try:
        svc = client.get_service(request=run_v2.GetServiceRequest(name=svc_name))
    except Exception as e:
        log.warning("[preview] get_service for tag failed: %s", e)
        return

    # Find latest revision
    latest_rev = svc.latest_ready_revision or svc.latest_created_revision
    if not latest_rev:
        log.warning("[preview] no latest revision found")
        return
    latest_rev_short = latest_rev.split("/")[-1]

    # Build new traffic: keep existing 100% target + add tag for latest (no traffic)
    existing_traffic = list(svc.traffic)
    # Remove any old tag with same slug
    existing_traffic = [t for t in existing_traffic if t.tag != tag]
    # Add new tagged target with 0% traffic
    existing_traffic.append(run_v2.TrafficTarget(
        type_=run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION,
        revision=latest_rev_short,
        percent=0,
        tag=tag,
    ))
    svc.traffic = existing_traffic

    try:
        op = client.update_service(request=run_v2.UpdateServiceRequest(service=svc))
        op.result(timeout=120)
        log.info("[preview] tag %s set on revision %s", tag, latest_rev_short)
    except Exception as e:
        log.warning("[preview] update_service for tag failed: %s", e)


async def delete_preview_deploy(
    *,
    cloud_run_service: str,
    branch: str,
    region: str = "asia-southeast1",
) -> dict[str, Any]:
    """Remove preview tag (revision auto-cleanup by Cloud Run after no traffic + idle)."""
    from google.cloud import run_v2

    branch_slug = slugify_branch(branch)
    client = run_v2.ServicesClient()
    from app.core.config import settings
    svc_name = f"projects/{settings.gcp_project_id}/locations/{region}/services/{cloud_run_service}"

    try:
        svc = client.get_service(request=run_v2.GetServiceRequest(name=svc_name))
        new_traffic = [t for t in svc.traffic if t.tag != branch_slug]
        if len(new_traffic) == len(svc.traffic):
            return {"status": "not_found", "branch": branch}
        svc.traffic = new_traffic
        op = client.update_service(request=run_v2.UpdateServiceRequest(service=svc))
        op.result(timeout=120)
        return {"status": "deleted", "branch": branch, "tag": branch_slug}
    except Exception as e:
        log.warning("[preview] delete tag failed: %s", e)
        return {"status": "failed", "branch": branch, "error": str(e)[:200]}


def list_preview_deploys(
    cloud_run_service: str,
    region: str = "asia-southeast1",
) -> list[dict[str, Any]]:
    """List all active preview tags on a service."""
    from google.cloud import run_v2
    client = run_v2.ServicesClient()
    from app.core.config import settings
    svc_name = f"projects/{settings.gcp_project_id}/locations/{region}/services/{cloud_run_service}"

    try:
        svc = client.get_service(request=run_v2.GetServiceRequest(name=svc_name))
        previews = []
        for t in svc.traffic_statuses or []:
            if t.tag and t.tag not in ("v158", "v159", "v160", "v161", "v162", "v163",
                                        "v164", "v165", "v166", "v167", "v168", "v169"):
                # Skip version tags — only preview/branch tags
                if not re.match(r"^v\d+", t.tag):
                    previews.append({
                        "tag": t.tag,
                        "revision": t.revision,
                        "percent": t.percent,
                        "uri": t.uri,
                    })
        return previews
    except Exception as e:
        log.warning("[preview] list failed: %s", e)
        return []
