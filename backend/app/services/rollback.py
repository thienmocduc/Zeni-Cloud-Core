"""
Project Rollback Service (Phase 2 P2.2 — chairman approved 2026-05-11)

Mục đích: 1-click rollback từ Cloud Run revision hiện tại về revision trước.
Pattern Vercel/Railway: trong UI Deployments tab, click button "Rollback" trên 1
revision cũ → traffic flip ngay (0 downtime nếu cùng image base).

Implementation:
  - Cloud Run native traffic management (instant flip, no rebuild)
  - List revisions of service → user chọn revision_name target
  - update_service_traffic → set 100% traffic to target revision
  - Optional: --tag rollback-{prev_rev} để track

KHÔNG đụng existing deploy code — đây là service mới hoàn toàn.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from google.api_core import exceptions as gcp_exceptions

from app.core.config import settings

log = logging.getLogger("zeni.rollback")


def _run_client():
    """Lazy import — avoid load at module import time."""
    from google.cloud import run_v2
    return run_v2.ServicesClient()


def _service_full_name(service_name: str, region: str) -> str:
    return f"projects/{settings.gcp_project_id}/locations/{region}/services/{service_name}"


def list_revisions(service_name: str, region: str) -> list[dict[str, Any]]:
    """List all revisions of a Cloud Run service (most recent first).

    Returns: list of {name, image, created_at, traffic_percent, tag, status}
    """
    from google.cloud import run_v2
    rev_client = run_v2.RevisionsClient()
    parent = _service_full_name(service_name, region)

    revisions = []
    try:
        for rev in rev_client.list_revisions(request=run_v2.ListRevisionsRequest(parent=parent)):
            revisions.append({
                "name": rev.name.split("/")[-1],
                "full_name": rev.name,
                "image": rev.containers[0].image if rev.containers else None,
                "created_at": rev.create_time.isoformat() if rev.create_time else None,
                "container_concurrency": rev.max_instance_request_concurrency,
                "scaling_min": rev.scaling.min_instance_count if rev.scaling else None,
                "scaling_max": rev.scaling.max_instance_count if rev.scaling else None,
            })
    except gcp_exceptions.NotFound:
        return []
    except Exception as e:
        log.error("[rollback] list_revisions failed: %s", e)
        return []

    # Annotate with current traffic %
    try:
        svc_client = _run_client()
        from google.cloud import run_v2 as run_v2_mod
        svc = svc_client.get_service(
            request=run_v2_mod.GetServiceRequest(name=_service_full_name(service_name, region))
        )
        traffic_map: dict[str, dict[str, Any]] = {}
        for t in svc.traffic_statuses or []:
            traffic_map[t.revision] = {
                "percent": t.percent,
                "tag": t.tag,
                "uri": t.uri,
            }
        for r in revisions:
            t = traffic_map.get(r["name"]) or {}
            r["traffic_percent"] = t.get("percent", 0)
            r["tag"] = t.get("tag")
            r["uri"] = t.get("uri")
    except Exception as e:
        log.warning("[rollback] get traffic info failed: %s", e)

    # Sort: most recent first
    revisions.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return revisions


def rollback_to_revision(
    *,
    service_name: str,
    region: str,
    target_revision: str,
    tag: Optional[str] = None,
) -> dict[str, Any]:
    """
    Flip 100% traffic to target_revision.

    Args:
      service_name: Cloud Run service name
      region: GCP region
      target_revision: full revision name (e.g., "zeni-backend-v167")
      tag: optional traffic tag (e.g., "rollback-2026-05-11")

    Returns:
      {
        "service": service_name,
        "rolled_back_to": target_revision,
        "previous_revision": ...,
        "status": "success" | "failed",
        "error": ... (if failed)
      }
    """
    from google.cloud import run_v2

    client = _run_client()
    svc_name = _service_full_name(service_name, region)

    try:
        existing = client.get_service(request=run_v2.GetServiceRequest(name=svc_name))
    except gcp_exceptions.NotFound:
        return {"status": "failed", "error": f"Service {service_name} not found"}

    # Find current 100% revision (to record as `previous`)
    previous = None
    for t in existing.traffic_statuses or []:
        if t.percent == 100:
            previous = t.revision
            break

    # Verify target revision exists
    target_rev_full = f"{svc_name}/revisions/{target_revision}"
    try:
        from google.cloud import run_v2 as run_v2_mod
        rev_client = run_v2_mod.RevisionsClient()
        rev_client.get_revision(request=run_v2_mod.GetRevisionRequest(name=target_rev_full))
    except gcp_exceptions.NotFound:
        return {
            "status": "failed",
            "error": f"Target revision {target_revision} không tồn tại",
        }

    # Build new traffic config: 100% to target
    new_traffic = [
        run_v2.TrafficTarget(
            type_=run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION,
            revision=target_revision,
            percent=100,
            tag=tag,
        )
    ]
    existing.traffic = new_traffic

    try:
        op = client.update_service(request=run_v2.UpdateServiceRequest(service=existing))
        op.result(timeout=300)
    except gcp_exceptions.GoogleAPICallError as e:
        return {
            "status": "failed",
            "error": f"Rollback API call failed: {e.message}",
            "previous_revision": previous,
        }

    log.info("[rollback] %s: %s → %s (tag=%s)",
             service_name, previous, target_revision, tag)

    return {
        "status": "success",
        "service": service_name,
        "region": region,
        "rolled_back_to": target_revision,
        "previous_revision": previous,
        "tag": tag,
    }
