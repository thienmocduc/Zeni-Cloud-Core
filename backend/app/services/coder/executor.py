"""
Step Executor — execute plan với checkpoint, self-correction, reviewer/qa verdict.

Pattern:
  for step in plan:
    1. Resolve depends_on (wait deps complete)
    2. Pre-check (Security re-scan if medium+)
    3. Wait chairman approval if requires_approval
    4. Execute tool
    5. Reviewer + QA verdict
    6. If fail + retry_count < max → retry với corrected args
    7. Else → mark step status

Auto-correction: nếu tool fail, gọi CODER persona với error context để propose
fixed args. Limit 3 retries để tránh infinite loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .council import review_step_output
from .orchestrator import call_persona

log = logging.getLogger("zeni.coder.executor")


async def materialize_steps(
    db: AsyncSession, run_id: str, planner_steps: list[dict]
) -> int:
    """
    Persist planner_steps into cto_run_steps table.
    Returns count of steps created.
    """
    count = 0
    for step in planner_steps:
        idx = step.get("step", count + 1)
        tool_name = step.get("tool", "unknown")
        args = step.get("args", {})
        deps = step.get("depends_on", [])
        risk = step.get("risk", "medium")
        desc = step.get("description", "")
        requires_approval = risk in ("medium", "high", "destructive")

        await db.execute(text(
            "INSERT INTO cto_run_steps (id, run_id, step_idx, tool_name, tool_args, "
            "depends_on, description, status, requires_approval) "
            "VALUES (:id, :rid, :idx, :tn, CAST(:ta AS jsonb), CAST(:dp AS jsonb), "
            ":desc, 'pending', :req)"
        ), {
            "id": str(uuid.uuid4()), "rid": run_id, "idx": idx,
            "tn": tool_name, "ta": json.dumps(args),
            "dp": json.dumps(deps), "desc": desc,
            "req": requires_approval,
        })
        count += 1

    await db.execute(text(
        "UPDATE cto_coder_runs SET total_steps = :n WHERE id = :id"
    ), {"n": count, "id": run_id})
    await db.commit()
    return count


async def execute_step(
    db: AsyncSession,
    run_id: str,
    step_idx: int,
    target_workspace: Optional[str] = None,
    complexity: str = "medium",
) -> dict:
    """
    Execute 1 step in run pipeline.

    Args:
        complexity: propagate từ run → reviewer/qa dùng tier phù hợp
                    (destructive tool tự auto-escalate critical bên trong review_step_output)

    Returns: {status, result|error, reviewer_verdict, qa_verdict, duration_ms, retry_count}
    """
    step_row = (await db.execute(text(
        "SELECT id::text, tool_name, tool_args, status, retry_count, max_retries, "
        "requires_approval, approved_by "
        "FROM cto_run_steps WHERE run_id = :rid AND step_idx = :idx"
    ), {"rid": run_id, "idx": step_idx})).mappings().first()
    if not step_row:
        return {"status": "not_found", "error": f"Step {step_idx} not found"}

    if step_row["requires_approval"] and not step_row["approved_by"]:
        return {"status": "awaiting_approval", "step_id": step_row["id"]}

    tool_name = step_row["tool_name"]
    args = step_row["tool_args"] if isinstance(step_row["tool_args"], dict) else json.loads(step_row["tool_args"] or "{}")
    retry_count = step_row["retry_count"] or 0

    # Mark executing
    await db.execute(text(
        "UPDATE cto_run_steps SET status='executing', executed_at=NOW() WHERE id = :id"
    ), {"id": step_row["id"]})
    await db.commit()

    start = time.perf_counter()
    error: Optional[str] = None
    result: Any = None

    try:
        # Dispatch to tool executor (lazy import to avoid circular)
        from app.api.cto_console import TOOL_EXECUTORS
        handler_name = TOOL_EXECUTORS.get(tool_name)
        if not handler_name:
            # Try internal-only tools
            handler = _INTERNAL_TOOLS.get(tool_name)
            if not handler:
                raise RuntimeError(f"No executor for tool '{tool_name}'")
        else:
            from app.api import cto_console
            handler = getattr(cto_console, handler_name, None)
            if not handler:
                raise RuntimeError(f"Executor '{handler_name}' not found in cto_console module")

        result = await handler(db, args, target_workspace)
        duration = int((time.perf_counter() - start) * 1000)

    except Exception as e:
        error = str(e)
        duration = int((time.perf_counter() - start) * 1000)
        log.warning("[executor] step %s/%d tool=%s failed: %s", run_id, step_idx, tool_name, error)

    # Reviewer + QA verdict (parallel) — propagate complexity
    rv_verdict, qv_verdict = await review_step_output(
        db, run_id, step_idx, tool_name, args, result, error,
        complexity=complexity,
    )

    # Self-correct + retry if failed and retries left
    final_status = "completed" if not error else "failed"
    if error and retry_count < (step_row["max_retries"] or 3):
        # Ask CODER to propose corrected args
        # Use complex tier for self-correction — retry needs deeper reasoning
        correction_prompt = (
            f"Step {step_idx} tool '{tool_name}' failed:\n"
            f"Args: {json.dumps(args, default=str)[:1000]}\n"
            f"Error: {error}\n\n"
            f"Propose corrected args (return JSON only, format: {{\"args\": {{...}}}})."
        )
        try:
            correction = await call_persona("coder", correction_prompt, complexity="complex")
            if correction.output_json and correction.output_json.get("args"):
                new_args = correction.output_json["args"]
                await db.execute(text(
                    "UPDATE cto_run_steps SET tool_args = CAST(:a AS jsonb), "
                    "retry_count = retry_count + 1, status='pending' WHERE id = :id"
                ), {"a": json.dumps(new_args), "id": step_row["id"]})
                await db.commit()
                log.info("[executor] step %s/%d retry %d with new args",
                         run_id, step_idx, retry_count + 1)
                final_status = "retry_pending"
        except Exception as ex:
            log.warning("[executor] correction failed: %s", ex)

    # Persist final state
    await db.execute(text(
        "UPDATE cto_run_steps SET status = :st, duration_ms = :d, "
        "result = CAST(:r AS jsonb), error_detail = :err, "
        "reviewer_verdict = :rv, qa_verdict = :qv "
        "WHERE id = :id"
    ), {
        "st": final_status, "d": duration,
        "r": json.dumps(result, default=str) if result else None,
        "err": error, "rv": rv_verdict, "qv": qv_verdict,
        "id": step_row["id"],
    })
    await db.commit()

    return {
        "status": final_status,
        "result": result,
        "error": error,
        "reviewer_verdict": rv_verdict,
        "qa_verdict": qv_verdict,
        "duration_ms": duration,
        "retry_count": retry_count + (1 if final_status == "retry_pending" else 0),
    }


async def execute_run(db: AsyncSession, run_id: str) -> dict:
    """
    Execute full run: iterate steps in order respecting depends_on.
    Background task; updates cto_coder_runs.status.
    """
    run = (await db.execute(text(
        "SELECT id::text, status, target_workspace, total_steps, requirement, target_project_id "
        "FROM cto_coder_runs WHERE id = :id"
    ), {"id": run_id})).mappings().first()
    if not run:
        return {"status": "not_found"}

    if run["status"] not in ("approved", "executing"):
        return {"status": "not_ready", "current_status": run["status"]}

    await db.execute(text(
        "UPDATE cto_coder_runs SET status='executing', started_at=COALESCE(started_at, NOW()) "
        "WHERE id = :id"
    ), {"id": run_id})
    await db.commit()

    target_ws = run["target_workspace"]
    total = run["total_steps"] or 0

    # Re-detect complexity from run requirement (so reviewer/qa use right tier)
    from .orchestrator import detect_complexity
    complexity = detect_complexity(
        run["requirement"], run.get("target_workspace"), run.get("target_project_id")
    )

    # Get pending steps in order
    steps = (await db.execute(text(
        "SELECT step_idx FROM cto_run_steps WHERE run_id = :rid AND status = 'pending' "
        "ORDER BY step_idx"
    ), {"rid": run_id})).mappings().all()

    completed_count = 0
    failed_count = 0
    awaiting_count = 0

    for s in steps:
        idx = s["step_idx"]
        # Update current_step_idx
        await db.execute(text(
            "UPDATE cto_coder_runs SET current_step_idx = :idx WHERE id = :id"
        ), {"idx": idx, "id": run_id})
        await db.commit()

        result = await execute_step(db, run_id, idx, target_workspace=target_ws, complexity=complexity)
        if result["status"] == "completed":
            completed_count += 1
        elif result["status"] == "awaiting_approval":
            awaiting_count += 1
            # Stop the run loop — chairman must approve
            await db.execute(text(
                "UPDATE cto_coder_runs SET status = 'awaiting_approval' WHERE id = :id"
            ), {"id": run_id})
            await db.commit()
            return {"status": "awaiting_approval", "step_idx": idx,
                    "completed": completed_count, "total": total}
        elif result["status"] == "retry_pending":
            # Retry the same step on next loop iteration
            return await execute_run(db, run_id)
        else:
            failed_count += 1
            # Critical fail → stop
            await db.execute(text(
                "UPDATE cto_coder_runs SET status='failed', completed_at=NOW(), "
                "error_summary = :err WHERE id = :id"
            ), {"err": result.get("error", "step failed")[:1000], "id": run_id})
            await db.commit()
            return {"status": "failed", "step_idx": idx,
                    "completed": completed_count, "failed": failed_count,
                    "error": result.get("error")}

    # Done
    await db.execute(text(
        "UPDATE cto_coder_runs SET status='completed', completed_at=NOW(), "
        "duration_ms = EXTRACT(EPOCH FROM (NOW() - COALESCE(started_at, created_at))) * 1000, "
        "final_result = CAST(:r AS jsonb) WHERE id = :id"
    ), {"r": json.dumps({
        "completed": completed_count, "total": total, "failed": failed_count
    }), "id": run_id})
    await db.commit()
    return {"status": "completed", "completed": completed_count, "total": total}


# ─── Internal tools (not in cto_tool_policy public list) ──────
async def _exec_propose_edit(db, args, target_ws):
    """Agent propose code patch — return diff as message."""
    file_id = args.get("file_id")
    instruction = args.get("instruction", "")
    if not file_id:
        raise ValueError("propose_edit cần file_id")

    snap = (await db.execute(text(
        "SELECT file_path, content, language FROM cto_project_file_snapshots "
        "WHERE id = :id AND expires_at > NOW()"
    ), {"id": file_id})).mappings().first()
    if not snap:
        raise ValueError(f"File snapshot {file_id} expired")

    coder_resp = await call_persona(
        "coder",
        f"File: {snap['file_path']} ({snap.get('language') or 'unknown'})\n\n"
        f"Current content:\n```\n{snap['content'][:8000]}\n```\n\n"
        f"User instruction: {instruction}\n\n"
        f"Generate UNIFIED DIFF (--- a/file +++ b/file format) hoặc full new content.",
    )
    return {
        "file_path": snap["file_path"],
        "diff_or_content": coder_resp.output_text,
        "model": coder_resp.model_id,
        "cost_usd": coder_resp.cost_usd,
    }


async def _exec_apply_edit(db, args, target_ws):
    """Apply approved diff — create new snapshot version."""
    file_id = args.get("file_id")
    new_content = args.get("new_content")
    if not file_id or new_content is None:
        raise ValueError("apply_edit cần file_id + new_content")

    orig = (await db.execute(text(
        "SELECT session_id::text, workspace_id, project_id, file_path, language "
        "FROM cto_project_file_snapshots WHERE id = :id"
    ), {"id": file_id})).mappings().first()
    if not orig:
        raise ValueError(f"Original snapshot {file_id} not found")

    new_id = uuid.uuid4()
    await db.execute(text(
        "INSERT INTO cto_project_file_snapshots (id, session_id, workspace_id, project_id, "
        "file_path, content, content_size, language, fetched_from) "
        "VALUES (:id, :sid, :ws, :pid, :p, :c, :sz, :l, 'agent_edit')"
    ), {
        "id": str(new_id), "sid": orig["session_id"], "ws": orig["workspace_id"],
        "pid": orig.get("project_id"), "p": orig["file_path"],
        "c": new_content, "sz": len(new_content), "l": orig.get("language"),
    })
    return {
        "new_snapshot_id": str(new_id),
        "file_path": orig["file_path"],
        "size": len(new_content),
    }


async def _exec_search_codebase(db, args, target_ws):
    """Pgvector semantic search across snapshots (returns top-k)."""
    query = args.get("query", "")
    session_id = args.get("session_id")
    if not query:
        raise ValueError("search_codebase cần query")
    # Simple LIKE fallback nếu không có embedding
    rows = (await db.execute(text(
        "SELECT id::text, file_path, language, "
        "substring(content for 200) AS preview "
        "FROM cto_project_file_snapshots "
        "WHERE expires_at > NOW() AND content ILIKE :q "
        + ("AND session_id = :sid " if session_id else "")
        + "LIMIT 10"
    ), {"q": f"%{query}%", **({"sid": session_id} if session_id else {})})).mappings().all()
    return {"results": [dict(r) for r in rows]}


async def _exec_recall_memory(db, args, target_ws):
    """Retrieve agent memory by keyword."""
    query = args.get("query", "")
    scope = args.get("scope", target_ws or "global")
    rows = (await db.execute(text(
        "SELECT id::text, memory_type, title, content, use_count, created_at::text "
        "FROM cto_agent_memory WHERE scope = :s "
        + ("AND content ILIKE :q " if query else "")
        + "ORDER BY use_count DESC, created_at DESC LIMIT 10"
    ), {"s": scope, **({"q": f"%{query}%"} if query else {})})).mappings().all()
    # Increment use_count
    if rows:
        ids = [r["id"] for r in rows]
        await db.execute(text(
            "UPDATE cto_agent_memory SET use_count = use_count + 1, last_used_at = NOW() "
            "WHERE id::text = ANY(:ids)"
        ), {"ids": ids})
        await db.commit()
    return {"memories": [dict(r) for r in rows]}


async def _exec_save_memory(db, args, target_ws):
    """Save observation/learning into memory."""
    mtype = args.get("memory_type", "knowledge")
    title = args.get("title", "")
    content = args.get("content", "")
    scope = args.get("scope", target_ws or "global")
    if not content:
        raise ValueError("save_memory cần content")
    mid = uuid.uuid4()
    await db.execute(text(
        "INSERT INTO cto_agent_memory (id, scope, memory_type, title, content, metadata) "
        "VALUES (:id, :s, :mt, :t, :c, CAST(:meta AS jsonb))"
    ), {
        "id": str(mid), "s": scope, "mt": mtype, "t": title, "c": content,
        "meta": json.dumps(args.get("metadata", {})),
    })
    await db.commit()
    return {"id": str(mid), "memory_type": mtype}


_INTERNAL_TOOLS: dict[str, Any] = {
    "propose_edit": _exec_propose_edit,
    "apply_edit": _exec_apply_edit,
    "search_codebase": _exec_search_codebase,
    "recall_memory": _exec_recall_memory,
    "save_memory": _exec_save_memory,
}
