"""
Zeni Cloud Core — L4 Cron Scheduler service.

Wraps Google Cloud Scheduler v1 API. Each cron job is namespaced by workspace
via labels {zeni-workspace=<ws>, zeni-cron=true} for multi-tenant isolation.

Usage:
  create_cron(workspace, name, cron_expr, target_url, method, headers, body)
  list_crons(workspace) → labelled jobs
  delete_cron(workspace, name)
  run_cron_now(workspace, name)  // force-run immediately
"""
from __future__ import annotations

import logging
from typing import Any
from functools import lru_cache

from google.api_core import exceptions as gcp_exceptions
from google.cloud import scheduler_v1

from app.core.config import settings

log = logging.getLogger("zeni.scheduler")


# Cloud Scheduler is region-bound; pick same region as Cloud Run for low latency
LOCATION = "us-central1"


@lru_cache(maxsize=1)
def _client() -> scheduler_v1.CloudSchedulerClient:
    return scheduler_v1.CloudSchedulerClient()


def _parent() -> str:
    return f"projects/{settings.gcp_project_id}/locations/{LOCATION}"


def _job_name(workspace: str, name: str) -> str:
    safe = name.lower().replace("_", "-")
    return f"{_parent()}/jobs/zeni-{workspace}-{safe}"


class CronError(RuntimeError):
    pass


def create_cron(
    *, workspace: str, name: str, schedule: str, target_url: str,
    method: str = "POST", headers: dict[str, str] | None = None,
    body: str | None = None, timezone: str = "Asia/Ho_Chi_Minh",
    description: str | None = None,
) -> dict[str, Any]:
    """Create or replace a Cloud Scheduler job."""
    client = _client()
    full_name = _job_name(workspace, name)

    http_method = {
        "GET": scheduler_v1.HttpMethod.GET,
        "POST": scheduler_v1.HttpMethod.POST,
        "PUT": scheduler_v1.HttpMethod.PUT,
        "DELETE": scheduler_v1.HttpMethod.DELETE,
        "PATCH": scheduler_v1.HttpMethod.PATCH,
    }.get(method.upper(), scheduler_v1.HttpMethod.POST)

    http_target = scheduler_v1.HttpTarget(
        uri=target_url,
        http_method=http_method,
        headers=headers or {},
    )
    if body and method.upper() in ("POST", "PUT", "PATCH"):
        http_target.body = body.encode("utf-8")

    job = scheduler_v1.Job(
        name=full_name,
        description=description or f"Zeni Cloud cron job for workspace {workspace}",
        schedule=schedule,
        time_zone=timezone,
        http_target=http_target,
    )

    try:
        # Try update first (idempotent), if not found → create
        try:
            existing = client.get_job(name=full_name)
            updated = client.update_job(job=job, update_mask={"paths": [
                "schedule", "time_zone", "description", "http_target"
            ]})
            return _job_to_dict(updated)
        except gcp_exceptions.NotFound:
            created = client.create_job(parent=_parent(), job=job)
            return _job_to_dict(created)
    except gcp_exceptions.GoogleAPICallError as e:
        raise CronError(f"Tạo cron thất bại: {e.message}") from e


def list_crons(workspace: str) -> list[dict[str, Any]]:
    """List all cron jobs for a workspace (filtered by name prefix)."""
    client = _client()
    prefix = f"{_parent()}/jobs/zeni-{workspace}-"
    out: list[dict] = []
    try:
        for job in client.list_jobs(parent=_parent()):
            if job.name.startswith(prefix):
                out.append(_job_to_dict(job))
    except gcp_exceptions.GoogleAPICallError as e:
        raise CronError(f"List cron thất bại: {e.message}") from e
    return out


def delete_cron(*, workspace: str, name: str) -> None:
    full_name = _job_name(workspace, name)
    try:
        _client().delete_job(name=full_name)
    except gcp_exceptions.NotFound:
        return
    except gcp_exceptions.GoogleAPICallError as e:
        raise CronError(f"Xoá cron thất bại: {e.message}") from e


def pause_cron(*, workspace: str, name: str) -> dict[str, Any]:
    full_name = _job_name(workspace, name)
    try:
        job = _client().pause_job(name=full_name)
    except gcp_exceptions.GoogleAPICallError as e:
        raise CronError(f"Pause cron thất bại: {e.message}") from e
    return _job_to_dict(job)


def resume_cron(*, workspace: str, name: str) -> dict[str, Any]:
    full_name = _job_name(workspace, name)
    try:
        job = _client().resume_job(name=full_name)
    except gcp_exceptions.GoogleAPICallError as e:
        raise CronError(f"Resume cron thất bại: {e.message}") from e
    return _job_to_dict(job)


def run_cron_now(*, workspace: str, name: str) -> dict[str, Any]:
    """Force-run the cron immediately (out-of-schedule)."""
    full_name = _job_name(workspace, name)
    try:
        job = _client().run_job(name=full_name)
    except gcp_exceptions.GoogleAPICallError as e:
        raise CronError(f"Run cron thất bại: {e.message}") from e
    return _job_to_dict(job)


def _job_to_dict(job: scheduler_v1.Job) -> dict[str, Any]:
    short_name = job.name.split("/")[-1]
    return {
        "name": short_name,
        "full_name": job.name,
        "schedule": job.schedule,
        "time_zone": job.time_zone,
        "description": job.description,
        "state": scheduler_v1.Job.State(job.state).name if job.state else "UNSPECIFIED",
        "target_url": job.http_target.uri if job.http_target else None,
        "method": scheduler_v1.HttpMethod(job.http_target.http_method).name if job.http_target else None,
        "last_attempt_time": job.last_attempt_time.isoformat() if job.last_attempt_time else None,
        "user_update_time": job.user_update_time.isoformat() if job.user_update_time else None,
    }
