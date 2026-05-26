"""
Zeni Cloud — CTO Assistant tools (Phase 2 LLM agent).

Set of in-process tools mà CTO chat agent có thể gọi để hỗ trợ khách deploy.
Tránh HTTP round-trip — gọi thẳng service/DB.

Tools:
  - provision_registry(ws)              → /registry/provision tương đương
  - deploy_image(ws, image, name, size) → /cto/deploy image flow
  - list_projects(ws)                   → SELECT từ projects table
  - get_project_status(ws, project_id)  → status + URL
  - add_whitelist(ws, prefix)           → INSERT workspace_image_whitelist
  - read_logs(ws, project_id, n)        → Cloud Run logging API (last N lines)

Mỗi tool trả `dict` JSON-safe (string + int + nested dict/list).
"""
from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("zeni.cto.tools")


# ─── Tool definitions cho prompt (JSON Schema-lite) ────────────
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "provision_registry",
        "description": "Tự động tạo Zeni Container Registry cho workspace hiện tại (Artifact Registry repo + Service Account + IAM binding + auto-whitelist). Idempotent — gọi nhiều lần OK.",
        "parameters": {},
    },
    {
        "name": "deploy_image",
        "description": "Deploy Docker image lên Cloud Run. Tự động whitelist prefix nếu cần. Trả về project_id + URL khi live.",
        "parameters": {
            "image_url": {"type": "string", "required": True, "description": "Full image URL, vd: docker.io/library/nginx:alpine hoặc us-central1-docker.pkg.dev/zeni-cloud-core/ws/app:v1"},
            "project_name": {"type": "string", "required": False, "description": "Tên project (kebab-case, ≤40 chars). Mặc định auto-derive từ image."},
            "size": {"type": "string", "required": False, "description": "xs (1vCPU/512MB), s (1vCPU/1GB), m (2vCPU/2GB), l (4vCPU/4GB). Mặc định 's'."},
        },
    },
    {
        "name": "list_projects",
        "description": "List 20 projects gần nhất của workspace hiện tại với status + URL.",
        "parameters": {},
    },
    {
        "name": "get_project_status",
        "description": "Lấy status chi tiết của 1 project (đang deploy, running, failed) + URL + revision.",
        "parameters": {
            "project_id": {"type": "string", "required": True, "description": "UUID của project (lấy từ list_projects)."},
        },
    },
    {
        "name": "add_whitelist",
        "description": "Thêm prefix Docker image vào workspace whitelist. Vd: 'ghcr.io/myorg/' để allow pull từ GitHub Container Registry.",
        "parameters": {
            "prefix": {"type": "string", "required": True, "description": "Prefix với trailing slash, vd: 'ghcr.io/myorg/' hoặc 'registry.gitlab.com/team/'"},
        },
    },
    {
        "name": "read_logs",
        "description": "Đọc 50 dòng log gần nhất của Cloud Run service. Dùng để debug khi project lỗi.",
        "parameters": {
            "project_id": {"type": "string", "required": True},
            "lines": {"type": "integer", "required": False, "description": "Số dòng (max 200, default 50)"},
        },
    },
    {
        "name": "delegate_to_specialist",
        "description": (
            "Khi khách hỏi chuyên môn NGOÀI scope deploy (vd: review hợp đồng pháp lý, "
            "code review chi tiết, viết marketing copy, phân tích tài chính, thiết kế nội thất, "
            "compliance NĐ13, OCR hóa đơn VAT, tính lương, v.v.) — KHÔNG tự trả lời mà "
            "delegate sang specialist agent phù hợp từ thư viện 108 agents qua ZeniRouter. "
            "CTO vẫn giữ vai trò orchestrator: nhận response từ specialist, format gọn gàng, "
            "ship cho khách với context Zeni. Nếu không có specialist phù hợp, trả 'ok=false'."
        ),
        "parameters": {
            "agent_id": {
                "type": "string", "required": True,
                "description": (
                    "Slug agent từ catalog (bảng agent_catalog). Ví dụ phổ biến: "
                    "'legal-document-reviewer', 'contract-generator', 'compliance-checker' (legal); "
                    "'code-reviewer', 'bug-triage-bot', 'api-doc-generator' (dev); "
                    "'marketing-copywriter', 'email-campaign-writer' (marketing); "
                    "'csv-analyzer', 'data-cleaner', 'etl-pipeline-builder' (data); "
                    "'tax-filing-helper-vn', 'cash-flow-forecaster' (finance VN); "
                    "'shopee-seller-bot', 'tiktok-shop-bot', 'vietqr-receipt-parser', 'misa-sync' (VN-vertical); "
                    "'mood-board-curator', 'color-palette-generator', 'feng-shui-advisor' (design); "
                    "'smart-contract-auditor', 'nft-metadata-generator' (web3); "
                    "'menu-engineering', 'recipe-standardizer' (f&b); "
                    "'symptom-triage', 'lab-result-interpreter', 'diet-plan-vn-food' (healthcare). "
                    "Đầy đủ 108 slug có trong bảng agent_catalog."
                ),
            },
            "input": {
                "type": "string", "required": True,
                "description": (
                    "Câu hỏi/nội dung cụ thể cho specialist. PHẢI preserve full context của khách "
                    "(không paraphrase). Specialist nhận input → trả output, CTO format + ship cho user."
                ),
            },
        },
    },
]


def _workspace_slug(ws: str) -> str:
    s = re.sub(r"[^a-z0-9-]", "-", ws.lower()).strip("-")
    return s[:30] or "ws"


# ─── Tool implementations ─────────────────────────────────────
async def tool_provision_registry(*, workspace_id: str, user_email: str, db: AsyncSession) -> dict[str, Any]:
    """Reuse logic from app.api.registry.registry_provision but in-process."""
    from app.api.registry import _get_gcp_token, _registry_url, _pusher_sa, GCP_PROJECT, AR_LOCATION

    slug = _workspace_slug(workspace_id)
    url = _registry_url(workspace_id)
    sa_email = _pusher_sa(workspace_id)
    sa_short = f"{slug}-pusher"

    token = await _get_gcp_token()
    repo_created = sa_created = whitelist_added = False
    already_existed = False

    async with httpx.AsyncClient(timeout=30.0) as client:
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # 1) AR repo
        r = await client.post(
            f"https://artifactregistry.googleapis.com/v1/projects/{GCP_PROJECT}/locations/{AR_LOCATION}/repositories?repositoryId={slug}",
            headers=headers,
            json={"format": "DOCKER", "description": f"Zeni Container Registry for workspace {workspace_id}",
                  "labels": {"workspace": slug, "managed_by": "zeni-cloud"}},
        )
        if r.status_code in (200, 202):
            repo_created = True
        elif r.status_code == 409:
            already_existed = True
        else:
            return {"ok": False, "error": f"AR create repo HTTP {r.status_code}: {r.text[:200]}"}

        # 2) SA
        r = await client.post(
            f"https://iam.googleapis.com/v1/projects/{GCP_PROJECT}/serviceAccounts",
            headers=headers,
            json={"accountId": sa_short, "serviceAccount": {
                "displayName": f"{workspace_id} pusher",
                "description": f"Push images to AR repo {slug} (Zeni Cloud auto-provisioned)",
            }},
        )
        if r.status_code in (200, 201):
            sa_created = True

        # 3) IAM bind
        try:
            await client.post(
                f"https://artifactregistry.googleapis.com/v1/projects/{GCP_PROJECT}/locations/{AR_LOCATION}/repositories/{slug}:setIamPolicy",
                headers=headers,
                json={"policy": {"bindings": [
                    {"role": "roles/artifactregistry.writer", "members": [f"serviceAccount:{sa_email}"]}
                ]}},
            )
        except Exception as e:
            log.warning("[cto.tools] IAM bind warn: %s", e)

    # 4) Whitelist
    try:
        await db.execute(text("""
            INSERT INTO workspace_image_whitelist (workspace_id, prefix, description, enabled)
            VALUES (:ws, :p, 'Zeni Container Registry (auto-provisioned via CTO chat)', TRUE)
            ON CONFLICT (workspace_id, prefix) DO NOTHING
        """), {"ws": workspace_id, "p": url + "/"})
        await db.commit()
        whitelist_added = True
    except Exception as e:
        log.warning("[cto.tools] whitelist insert warn: %s", e)

    return {
        "ok": True,
        "registry_url": url,
        "pusher_sa": sa_email,
        "repo_created": repo_created,
        "sa_created": sa_created,
        "whitelist_added": whitelist_added,
        "already_existed": already_existed,
        "next_step": "Khách cần tạo SA key qua /app/registry → nút 'Generate key.json' để có credentials docker login. Em không tạo key tự động (security).",
    }


async def tool_deploy_image(
    *, workspace_id: str, user_email: str, db: AsyncSession,
    image_url: str, project_name: str | None = None, size: str = "s",
) -> dict[str, Any]:
    """Reuse api.projects.deploy_project for the actual deploy."""
    from app.api.projects import deploy_project as projects_deploy
    from app.schemas.resources import ProjectCreateIn
    from fastapi import BackgroundTasks, HTTPException

    if not project_name:
        base = image_url.rsplit("/", 1)[-1].split(":")[0]
        project_name = re.sub(r"[^a-z0-9-]", "-", base.lower())[:40].strip("-") or "cto-app"

    if size not in ("xs", "s", "m", "l"):
        size = "s"

    # Auto-add whitelist prefix
    if "/" in image_url:
        prefix = image_url.rsplit("/", 1)[0] + "/"
        try:
            await db.execute(text("""
                INSERT INTO workspace_image_whitelist (workspace_id, prefix, description, enabled)
                VALUES (:ws, :p, 'auto-added by CTO chat agent', TRUE)
                ON CONFLICT (workspace_id, prefix) DO NOTHING
            """), {"ws": workspace_id, "p": prefix})
            await db.commit()
        except Exception as e:
            log.warning("[cto.tools] whitelist auto-add warn: %s", e)

    class _MockUser:
        def __init__(self, email: str):
            self.email = email
            self.id = None
            self.role = "Developer"
            self.auth_scope = "full"

    payload = ProjectCreateIn(
        name=project_name, type="web", runtime="container", size=size,
        region="asia-southeast1", image=image_url, port=8080,
        allow_unauthenticated=True,
    )
    bg = BackgroundTasks()

    try:
        result = await projects_deploy(ws=workspace_id, data=payload, bg=bg, me=_MockUser(user_email), db=db)
        # Schedule background tasks (Cloud Run deploy runs async)
        import asyncio
        for task in bg.tasks:
            asyncio.create_task(task())
        return {
            "ok": True,
            "project_id": str(result.id),
            "project_name": result.name,
            "status": result.status,
            "note": "Deploy đã start. Cloud Run cần 30-60s để build. Em sẽ poll get_project_status để báo URL khi live.",
        }
    except HTTPException as e:
        return {"ok": False, "error": f"Deploy rejected: {e.detail}", "status_code": e.status_code}
    except Exception as e:
        return {"ok": False, "error": f"Deploy failed: {e}"}


async def tool_list_projects(*, workspace_id: str, db: AsyncSession) -> dict[str, Any]:
    rows = (await db.execute(text("""
        SELECT id, name, status, image, domain, created_at, region
        FROM projects
        WHERE workspace_id = :ws
        ORDER BY created_at DESC
        LIMIT 20
    """), {"ws": workspace_id})).mappings().all()
    return {
        "ok": True,
        "count": len(rows),
        "projects": [
            {
                "project_id": str(r["id"]),
                "name": r["name"],
                "status": r["status"],
                "image": r["image"],
                "url": r["domain"] or "",
                "region": r["region"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


async def tool_get_project_status(*, workspace_id: str, db: AsyncSession, project_id: str) -> dict[str, Any]:
    try:
        UUID(project_id)
    except Exception:
        return {"ok": False, "error": f"project_id không hợp lệ (cần UUID): {project_id}"}

    row = (await db.execute(text("""
        SELECT id, name, status, image, domain, region, created_at
        FROM projects
        WHERE id = :id AND workspace_id = :ws
    """), {"id": project_id, "ws": workspace_id})).mappings().first()
    if not row:
        return {"ok": False, "error": "Project không tồn tại trong workspace này"}
    return {
        "ok": True,
        "project_id": str(row["id"]),
        "name": row["name"],
        "status": row["status"],
        "image": row["image"],
        "url": row["domain"] or "",
        "region": row["region"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


async def tool_add_whitelist(*, workspace_id: str, db: AsyncSession, prefix: str) -> dict[str, Any]:
    if not prefix or len(prefix) > 200:
        return {"ok": False, "error": "Prefix không hợp lệ (1-200 chars)"}
    if not prefix.endswith("/"):
        prefix = prefix + "/"
    try:
        await db.execute(text("""
            INSERT INTO workspace_image_whitelist (workspace_id, prefix, description, enabled)
            VALUES (:ws, :p, 'Added by CTO chat agent', TRUE)
            ON CONFLICT (workspace_id, prefix) DO UPDATE SET enabled = TRUE
        """), {"ws": workspace_id, "p": prefix})
        await db.commit()
        return {"ok": True, "prefix": prefix, "note": "Whitelist đã active — workspace giờ có thể pull image từ prefix này."}
    except Exception as e:
        return {"ok": False, "error": f"Insert failed: {e}"}


async def tool_read_logs(*, workspace_id: str, db: AsyncSession, project_id: str, lines: int = 50) -> dict[str, Any]:
    try:
        UUID(project_id)
    except Exception:
        return {"ok": False, "error": f"project_id không hợp lệ: {project_id}"}

    row = (await db.execute(text("""
        SELECT name, region FROM projects WHERE id = :id AND workspace_id = :ws
    """), {"id": project_id, "ws": workspace_id})).mappings().first()
    if not row:
        return {"ok": False, "error": "Project không tồn tại"}

    from app.services.cloud_run import service_name_for
    service_name = service_name_for(workspace_id, row["name"])
    region = row["region"] or "asia-southeast1"
    n = max(10, min(int(lines or 50), 200))

    # Cloud Logging API filter
    from app.api.registry import _get_gcp_token, GCP_PROJECT
    token = await _get_gcp_token()
    filter_str = (
        f'resource.type="cloud_run_revision" '
        f'AND resource.labels.service_name="{service_name}" '
        f'AND resource.labels.location="{region}"'
    )
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            "https://logging.googleapis.com/v2/entries:list",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "resourceNames": [f"projects/{GCP_PROJECT}"],
                "filter": filter_str,
                "orderBy": "timestamp desc",
                "pageSize": n,
            },
        )
        if r.status_code != 200:
            return {"ok": False, "error": f"Cloud Logging HTTP {r.status_code}: {r.text[:200]}"}
        entries = r.json().get("entries", [])

    log_lines = []
    for e in entries:
        ts = e.get("timestamp", "")
        sev = e.get("severity", "DEFAULT")
        msg = e.get("textPayload") or e.get("jsonPayload", {}).get("message") or str(e.get("jsonPayload", {}))[:200]
        log_lines.append(f"[{ts}] {sev}: {msg}")

    return {"ok": True, "service_name": service_name, "region": region,
            "line_count": len(log_lines), "logs": log_lines}


# ─── Tool: delegate_to_specialist (NEW 2026-05-26) ────────────
async def tool_delegate_to_specialist(
    *, workspace_id: str, user_email: str, db: AsyncSession,
    agent_id: str, input: str,
) -> dict[str, Any]:
    """Delegate task chuyên môn cho specialist agent từ thư viện 108 qua ZeniRouter.

    Flow:
      1. Lookup agent_id trong agent_catalog → get system_prompt + default_model + cost
      2. Call llm_gateway.run_inference với system_prompt + user input
      3. Trả response cho CTO orchestrator (CTO sẽ format + ship cho user)

    Bảo mật:
      - Agent phải is_active = TRUE
      - Workspace context preserved (no cross-tenant data leak)
      - Strip injection (CTO charter đã filter sẵn ở Watcher layer)
    """
    from sqlalchemy import text as _sql
    from app.services.llm_gateway import run_inference

    if not agent_id or not isinstance(agent_id, str):
        return {"ok": False, "error": "agent_id required (slug from agent_catalog)"}
    if not input or not isinstance(input, str):
        return {"ok": False, "error": "input required (preserve user full request)"}
    if len(input) > 8000:
        return {"ok": False, "error": "input quá dài (max 8000 chars). Tóm tắt + retry."}

    row = (await db.execute(_sql("""
        SELECT id, name, name_vi, system_prompt, default_model, cost_per_run_usd, pricing_tier
        FROM agent_catalog
        WHERE id = :aid AND is_active = TRUE
    """), {"aid": agent_id})).mappings().one_or_none()
    if not row:
        hints = (await db.execute(_sql("""
            SELECT id, name_vi FROM agent_catalog
            WHERE is_active = TRUE AND (id ILIKE :pat OR name ILIKE :pat OR name_vi ILIKE :pat)
            ORDER BY install_count DESC NULLS LAST LIMIT 5
        """), {"pat": f"%{agent_id[:20]}%"})).mappings().all()
        hint_text = "; ".join(f"{h['id']} ({h['name_vi']})" for h in hints) if hints else "không có"
        return {
            "ok": False,
            "error": f"Specialist '{agent_id}' không tồn tại hoặc đã disable. Có thể anh muốn: {hint_text}",
        }

    messages = [
        {"role": "system", "content": row["system_prompt"]},
        {"role": "user", "content": input},
    ]
    model = row["default_model"] or "deepseek-chat"

    try:
        result = await run_inference(
            messages=messages,
            model=model,
            max_tokens=2048,
            temperature=0.4,
        )
    except Exception as e:
        log.exception("[cto.delegate] agent=%s call failed", agent_id)
        return {"ok": False, "error": f"Specialist {agent_id} call failed: {type(e).__name__}"}

    response_text = (result or {}).get("text") or (result or {}).get("content") or ""
    usage = (result or {}).get("usage") or {}

    try:
        from app.services.audit import audit_push
        await audit_push(
            db, actor=user_email, workspace_id=workspace_id,
            action="cto.delegate.specialist", target=agent_id, severity="info",
            metadata={
                "agent_id": agent_id,
                "agent_name": row["name_vi"] or row["name"],
                "input_length": len(input),
                "output_length": len(response_text),
                "model_used": model,
                "cost_usd": float(row["cost_per_run_usd"] or 0.005),
            },
        )
        await db.commit()
    except Exception:
        log.warning("[cto.delegate] audit push failed (non-fatal)")

    return {
        "ok": True,
        "specialist": {
            "agent_id": agent_id,
            "name": row["name_vi"] or row["name"],
            "pricing_tier": row["pricing_tier"],
        },
        "response": response_text,
        "model_used": model,
        "tokens": {
            "input": usage.get("input_tokens", 0),
            "output": usage.get("output_tokens", 0),
        },
        "cost_usd": float(row["cost_per_run_usd"] or 0.005),
        "_cto_hint": (
            "Specialist đã trả response. Anh (CTO) format gọn gàng + ship cho user. "
            "Nếu specialist response chưa đủ, có thể delegate tiếp cho specialist khác. "
            "Nhắc user pricing_tier nếu agent ở tier cao hơn gói hiện tại của họ."
        ),
    }


# ─── Tool dispatch ─────────────────────────────────────────────
TOOL_HANDLERS = {
    "provision_registry": tool_provision_registry,
    "deploy_image": tool_deploy_image,
    "list_projects": tool_list_projects,
    "get_project_status": tool_get_project_status,
    "add_whitelist": tool_add_whitelist,
    "read_logs": tool_read_logs,
    "delegate_to_specialist": tool_delegate_to_specialist,
}


async def execute_tool(
    tool_name: str, args: dict[str, Any], *,
    workspace_id: str, user_email: str, db: AsyncSession,
) -> dict[str, Any]:
    """Dispatch + sanitize tool call. Returns JSON-safe dict."""
    handler = TOOL_HANDLERS.get(tool_name)
    if not handler:
        return {"ok": False, "error": f"Unknown tool: {tool_name}. Tools có sẵn: {list(TOOL_HANDLERS.keys())}"}

    safe_args = {k: v for k, v in (args or {}).items() if isinstance(k, str)}
    try:
        return await handler(workspace_id=workspace_id, user_email=user_email, db=db, **safe_args)
    except TypeError as e:
        return {"ok": False, "error": f"Sai arguments cho tool {tool_name}: {e}"}
    except Exception as e:
        log.exception("[cto.tools] tool=%s exec failed", tool_name)
        return {"ok": False, "error": f"Tool {tool_name} crashed: {e}"}


def format_tools_for_prompt() -> str:
    """Render TOOL_DEFINITIONS as plain text for the system prompt."""
    lines = ["Bạn có thể gọi 7 tools sau (mỗi tool emit 1 JSON object riêng):", ""]
    for t in TOOL_DEFINITIONS:
        params = t["parameters"]
        if params:
            arg_list = ", ".join(f"{k}={v.get('type','string')}" for k, v in params.items())
        else:
            arg_list = "(no args)"
        lines.append(f"- {t['name']}({arg_list}): {t['description']}")
    return "\n".join(lines)
