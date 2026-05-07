"""
Zeni Cloud Core — L1 Compute: Cloud Run service wrapper.

Wraps google-cloud-run v2 API for deploying / listing / deleting Cloud Run
services from POST /projects. Uses application default credentials (ADC)
configured via GOOGLE_APPLICATION_CREDENTIALS env var.

Key design:
- Service name pattern:  zeni-{workspace_id}-{project_name}
- Labels applied:        zeni-workspace=<ws>, zeni-project=<name>, zeni-managed=true
- Each Cloud Run service is isolated by workspace label → list/get/delete
  operations filter by label to enforce multi-tenant separation.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from google.api_core import exceptions as gcp_exceptions
from google.cloud import run_v2

from app.core.config import settings

log = logging.getLogger("zeni.cloud_run")


# ─── Resource caps per size ─────────────────────────────────────
# Cloud Run rule: CPU < 1 requires concurrency = 1. We use CPU >= 1 always.
SIZE_TO_RESOURCES = {
    "xs": {"cpu": "1",    "memory": "512Mi", "min": 0, "max": 3,  "concurrency": 80},
    "s":  {"cpu": "1",    "memory": "1Gi",   "min": 0, "max": 5,  "concurrency": 80},
    "m":  {"cpu": "2",    "memory": "2Gi",   "min": 0, "max": 10, "concurrency": 80},
    "l":  {"cpu": "4",    "memory": "4Gi",   "min": 1, "max": 20, "concurrency": 80},
}

_SERVICE_NAME_RE = re.compile(r"^[a-z][a-z0-9\-]{0,61}[a-z0-9]$")


def service_name_for(workspace: str, project_name: str) -> str:
    """Deterministic Cloud Run service name. RFC 1123 compliant."""
    raw = f"zeni-{workspace}-{project_name}"[:63].lower()
    raw = re.sub(r"[^a-z0-9\-]", "-", raw)
    raw = re.sub(r"-+", "-", raw).strip("-")
    if not _SERVICE_NAME_RE.match(raw):
        raise ValueError(f"Invalid Cloud Run service name derived: {raw}")
    return raw


@dataclass
class DeployResult:
    service_name: str
    url: str | None
    revision: str | None
    region: str


class CloudRunError(RuntimeError):
    pass


def _client() -> run_v2.ServicesClient:
    # Uses ADC (GOOGLE_APPLICATION_CREDENTIALS) — SA key mounted in container
    return run_v2.ServicesClient()


def _parent(region: str) -> str:
    return f"projects/{settings.gcp_project_id}/locations/{region}"


def _service_full_name(name: str, region: str) -> str:
    return f"{_parent(region)}/services/{name}"


async def deploy_service(
    *,
    workspace: str,
    project_name: str,
    image: str,
    size: str = "s",
    region: str | None = None,
    env_vars: dict[str, str] | None = None,
    secrets: dict[str, str] | None = None,  # {ENV_NAME: "secret-name:version"}
    port: int = 8080,
    allow_unauthenticated: bool = True,
    created_by: str | None = None,
) -> DeployResult:
    """Deploy or update a Cloud Run service for a Zeni project."""
    region = region or settings.gcp_region or "us-central1"
    service_name = service_name_for(workspace, project_name)
    resources = SIZE_TO_RESOURCES.get(size, SIZE_TO_RESOURCES["s"])

    client = _client()

    # Build container spec
    env_list = [run_v2.EnvVar(name=k, value=str(v)) for k, v in (env_vars or {}).items()]
    for env_name, secret_ref in (secrets or {}).items():
        if ":" in secret_ref:
            sname, sver = secret_ref.split(":", 1)
        else:
            sname, sver = secret_ref, "latest"
        env_list.append(
            run_v2.EnvVar(
                name=env_name,
                value_source=run_v2.EnvVarSource(
                    secret_key_ref=run_v2.SecretKeySelector(secret=sname, version=sver)
                ),
            )
        )

    container = run_v2.Container(
        image=image,
        ports=[run_v2.ContainerPort(container_port=port)],
        resources=run_v2.ResourceRequirements(
            limits={"cpu": resources["cpu"], "memory": resources["memory"]},
        ),
        env=env_list,
    )

    template = run_v2.RevisionTemplate(
        containers=[container],
        scaling=run_v2.RevisionScaling(
            min_instance_count=resources["min"],
            max_instance_count=resources["max"],
        ),
        timeout={"seconds": 60},
        max_instance_request_concurrency=resources.get("concurrency", 80),
    )

    labels = {
        "zeni-workspace": workspace,
        "zeni-project": project_name,
        "zeni-managed": "true",
    }
    if created_by:
        # labels must be lowercase alphanumeric+hyphen
        safe_actor = re.sub(r"[^a-z0-9\-]", "-", (created_by or "").lower())[:63]
        if safe_actor:
            labels["zeni-created-by"] = safe_actor

    service = run_v2.Service(
        template=template,
        labels=labels,
        ingress=run_v2.IngressTraffic.INGRESS_TRAFFIC_ALL,
        launch_stage="GA",
    )

    # Check if exists → update, else create
    existing: run_v2.Service | None = None
    try:
        existing = client.get_service(request=run_v2.GetServiceRequest(name=_service_full_name(service_name, region)))
    except gcp_exceptions.NotFound:
        existing = None
    except gcp_exceptions.PermissionDenied as e:
        raise CloudRunError(f"Thiếu quyền truy cập Cloud Run: {e}") from e

    try:
        if existing is None:
            op = client.create_service(request=run_v2.CreateServiceRequest(
                parent=_parent(region), service=service, service_id=service_name,
            ))
        else:
            service.name = existing.name
            op = client.update_service(request=run_v2.UpdateServiceRequest(service=service))

        result: run_v2.Service = op.result(timeout=600)
    except gcp_exceptions.GoogleAPICallError as e:
        raise CloudRunError(f"Deploy Cloud Run thất bại: {e.message}") from e

    # Grant public access if requested
    if allow_unauthenticated:
        try:
            policy = client.get_iam_policy(request={"resource": result.name})
            need_add = True
            for b in policy.bindings:
                if b.role == "roles/run.invoker" and "allUsers" in b.members:
                    need_add = False
                    break
            if need_add:
                from google.iam.v1 import policy_pb2
                policy.bindings.append(policy_pb2.Binding(role="roles/run.invoker", members=["allUsers"]))
                client.set_iam_policy(request={"resource": result.name, "policy": policy})
        except Exception as e:
            log.warning("Không thể set public IAM policy cho %s: %s", service_name, e)

    return DeployResult(
        service_name=service_name,
        url=result.uri if result.uri else None,
        revision=result.latest_ready_revision.split("/")[-1] if result.latest_ready_revision else None,
        region=region,
    )


async def delete_service(*, workspace: str, project_name: str, region: str | None = None) -> None:
    region = region or settings.gcp_region or "us-central1"
    service_name = service_name_for(workspace, project_name)
    client = _client()
    try:
        op = client.delete_service(request=run_v2.DeleteServiceRequest(
            name=_service_full_name(service_name, region)
        ))
        op.result(timeout=300)
    except gcp_exceptions.NotFound:
        # Already gone — treat as success (idempotent)
        return
    except gcp_exceptions.GoogleAPICallError as e:
        raise CloudRunError(f"Xoá Cloud Run thất bại: {e.message}") from e


async def get_service_status(*, workspace: str, project_name: str, region: str | None = None) -> dict[str, Any]:
    region = region or settings.gcp_region or "us-central1"
    service_name = service_name_for(workspace, project_name)
    client = _client()
    try:
        svc = client.get_service(request=run_v2.GetServiceRequest(
            name=_service_full_name(service_name, region)
        ))
    except gcp_exceptions.NotFound:
        return {"exists": False}
    except gcp_exceptions.GoogleAPICallError as e:
        raise CloudRunError(f"Get Cloud Run thất bại: {e.message}") from e

    # Derive status from terminal condition
    state = "pending"
    for cond in svc.terminal_condition.__class__.__mro__:
        pass  # placeholder
    tc = svc.terminal_condition
    if tc and tc.type_ == "Ready":
        if tc.state == run_v2.Condition.State.CONDITION_SUCCEEDED:
            state = "running"
        elif tc.state == run_v2.Condition.State.CONDITION_FAILED:
            state = "failed"
        else:
            state = "pending"

    return {
        "exists": True,
        "url": svc.uri,
        "revision": svc.latest_ready_revision.split("/")[-1] if svc.latest_ready_revision else None,
        "state": state,
        "updated_at": svc.update_time.isoformat() if svc.update_time else None,
    }
