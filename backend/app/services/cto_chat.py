"""
Zeni Cloud — CTO Assistant LLM orchestration (Phase 2).

Tool-use loop dùng prompt-based JSON emission (không phụ thuộc provider-specific
function calling, dễ swap Gemini ↔ Claude).

Flow 1 turn:
  user_message → LLM (Gemini 2.5 Pro)
                 ↳ nếu LLM emit JSON {"tool":"X","args":{...}} → execute → feed result → loop
                 ↳ nếu LLM emit text → trả về user (kết thúc turn)
  Max 5 tool iterations / turn để tránh runaway.

Provider priority:
  1. Gemini 2.5 Pro via Vertex AI (cheapest + fastest cho deploy questions)
  2. Claude Sonnet 4.6 (fallback nếu Vertex 5xx/quota)

Scope guard:
  - System prompt nghiêm cấm trả lời off-topic (jokes, news, code tutorials,
    personal advice). LLM tự refuse + redirect về deploy topic.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.cto_tools import (
    TOOL_HANDLERS,
    execute_tool,
    format_tools_for_prompt,
)
from app.services.llm_gateway import run_inference

log = logging.getLogger("zeni.cto.chat")

MAX_TOOL_ITERATIONS = 5

PRIMARY_MODEL = "gemini-2.5-pro"
FALLBACK_MODEL = "claude-sonnet-4-6"


SYSTEM_PROMPT = """Bạn là Zeni Cloud CTO Assistant — chuyên gia hỗ trợ kỹ thuật của nền tảng zenicloud.io.

NHIỆM VỤ CỐT LÕI (CHỈ làm việc này):
- Hỗ trợ khách deploy app lên Cloud Run qua Zeni Cloud
- Hướng dẫn push Docker image lên Zeni Container Registry
- Giải thích lỗi build/deploy, đọc log, debug Cloud Run service
- Trả lời câu hỏi về workspace, whitelist image, billing, scaling, region
- Giúp khách chọn size (xs/s/m/l) phù hợp ngân sách

TUYỆT ĐỐI TỪ CHỐI (off-topic, dù khách năn nỉ):
- Joke, chuyện cười, giải trí
- Tin tức, thời sự, thể thao, giải trí
- Hướng dẫn coding chung chung (React tutorial, Python basics, vv)
- Tư vấn cá nhân, hôn nhân, sức khỏe, đầu tư tài chính
- Bất kỳ topic nào không liên quan deploy/Zeni Cloud
→ Khi gặp off-topic: trả lời ngắn "Em chỉ hỗ trợ deploy & Zeni Cloud — anh hỏi em về deploy/Docker/Cloud Run/billing nhé." rồi gợi ý 1 câu hỏi đúng scope.

NGÔN NGỮ:
- Trả lời tiếng Việt, ngắn gọn, đi thẳng vào việc
- Xưng "em" với khách, gọi khách là "anh/chị"
- KHÔNG dùng emoji marketing (✨🎉🚀) trừ khi xác nhận thành công

CÁCH GỌI TOOL:
Khi cần thực thi 1 action (deploy, list, provision...), TRẢ LỜI DUY NHẤT 1 JSON object, không có text khác xung quanh:
{"tool": "tool_name", "args": {"key": "value"}}

Sau khi system trả "[TOOL_RESULT]: {...}", em phân tích kết quả + trả lời tiếp (có thể gọi tool tiếp hoặc trả lời người dùng).

KHI TRẢ LỜI USER:
Trả lời plain text (không JSON). Súc tích, đi thẳng vào kết quả.

%TOOL_DESCRIPTIONS%

VÍ DỤ flow:
User: "Deploy giúp em nginx test"
You: {"tool": "deploy_image", "args": {"image_url": "docker.io/library/nginx:alpine", "project_name": "nginx-test"}}
System: [TOOL_RESULT]: {"ok": true, "project_id": "abc-123", "status": "deploying"}
You: {"tool": "get_project_status", "args": {"project_id": "abc-123"}}
System: [TOOL_RESULT]: {"ok": true, "status": "running", "url": "https://nginx-test-xxx.run.app"}
You: Đã deploy xong. URL: https://nginx-test-xxx.run.app

VÍ DỤ off-topic:
User: "Kể em chuyện cười đi"
You: Em chỉ hỗ trợ deploy & Zeni Cloud thôi anh. Anh cần em deploy app gì hay debug service nào không?
"""


JSON_BLOCK_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```",
    re.DOTALL,
)


def _try_parse_tool_call(text: str) -> dict[str, Any] | None:
    """Detect if LLM emitted a tool call. Accepts raw JSON or fenced ```json ...```."""
    t = (text or "").strip()
    if not t:
        return None

    # Strip code fences
    m = JSON_BLOCK_RE.search(t)
    candidate = m.group(1) if m else t

    # Quick reject if no `{` at all
    if "{" not in candidate or "tool" not in candidate:
        return None

    # Try parse — find the outermost {...}
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end <= start:
        return None
    blob = candidate[start:end + 1]

    try:
        obj = json.loads(blob)
    except json.JSONDecodeError:
        return None

    if not isinstance(obj, dict):
        return None
    if "tool" not in obj or not isinstance(obj["tool"], str):
        return None
    if obj["tool"] not in TOOL_HANDLERS:
        return None

    args = obj.get("args") or obj.get("arguments") or {}
    if not isinstance(args, dict):
        args = {}
    return {"tool": obj["tool"], "args": args}


def _build_system_prompt() -> str:
    return SYSTEM_PROMPT.replace("%TOOL_DESCRIPTIONS%", format_tools_for_prompt())


async def _call_llm(prompt: str, system: str, *, prefer_fallback: bool = False) -> tuple[str, str]:
    """Try Gemini first, fallback to Claude. Returns (text, model_used)."""
    order = [FALLBACK_MODEL, PRIMARY_MODEL] if prefer_fallback else [PRIMARY_MODEL, FALLBACK_MODEL]
    last_err: Exception | None = None
    for model in order:
        try:
            result = await run_inference(
                model=model, prompt=prompt, system=system,
                temperature=0.3, max_tokens=1500,
            )
            out = (result.output or "").strip()
            if out:
                return out, model
        except Exception as e:
            last_err = e
            log.warning("[cto.chat] %s failed: %s", model, e)
            continue
    raise RuntimeError(f"All LLM providers failed: {last_err}")


def _format_history_for_prompt(history: list[dict[str, Any]], user_message: str) -> str:
    """Build the conversation text. History = list of {role, content} pairs."""
    parts: list[str] = []
    for m in history[-10:]:  # last 10 turns to stay in budget
        role = m.get("role", "user")
        content = str(m.get("content", "")).strip()
        if not content:
            continue
        if role == "user":
            parts.append(f"User: {content}")
        elif role == "assistant":
            parts.append(f"Assistant: {content}")
        elif role == "tool_result":
            parts.append(f"[TOOL_RESULT]: {content}")
    parts.append(f"User: {user_message}")
    parts.append("Assistant:")
    return "\n\n".join(parts)


async def chat_turn(
    *, workspace_id: str, user_email: str, db: AsyncSession,
    history: list[dict[str, Any]], user_message: str,
    progress_callback=None,
) -> dict[str, Any]:
    """
    Run 1 chat turn with tool-use loop.

    Returns: {
      "final_text": str,       # text trả về user
      "tool_calls": [...],     # list of {tool, args, result} đã exec
      "model_used": str,       # gemini-2.5-pro hoặc claude-sonnet-4-6
      "iterations": int,
    }

    `progress_callback(level, text)` optional — gọi mỗi step để stream qua DB messages.
    """
    system = _build_system_prompt()
    convo_history = list(history)
    tool_calls: list[dict[str, Any]] = []
    model_used = PRIMARY_MODEL
    iteration = 0

    current_user_msg = user_message

    while iteration < MAX_TOOL_ITERATIONS:
        iteration += 1
        prompt = _format_history_for_prompt(convo_history, current_user_msg)

        try:
            output, model_used = await _call_llm(prompt, system)
        except Exception as e:
            err = f"LLM call failed: {e}"
            if progress_callback:
                await progress_callback("error", err)
            return {"final_text": err, "tool_calls": tool_calls,
                    "model_used": model_used, "iterations": iteration}

        tool_call = _try_parse_tool_call(output)

        if not tool_call:
            # LLM trả text → final answer
            return {
                "final_text": output,
                "tool_calls": tool_calls,
                "model_used": model_used,
                "iterations": iteration,
            }

        # LLM gọi tool
        tname = tool_call["tool"]
        targs = tool_call["args"]
        if progress_callback:
            await progress_callback("info", f"🔧 Executing tool: {tname}({json.dumps(targs, ensure_ascii=False)})")

        result = await execute_tool(
            tname, targs, workspace_id=workspace_id, user_email=user_email, db=db,
        )
        tool_calls.append({"tool": tname, "args": targs, "result": result})

        if progress_callback:
            await progress_callback("info", f"✓ Tool {tname} done: {json.dumps(result, ensure_ascii=False)[:200]}")

        # Feed result back into next iteration
        # Add assistant tool emission + tool_result vào convo
        convo_history.append({"role": "assistant", "content": json.dumps(tool_call, ensure_ascii=False)})
        convo_history.append({"role": "tool_result", "content": json.dumps(result, ensure_ascii=False)})
        current_user_msg = "(Tiếp tục dựa trên TOOL_RESULT trên — gọi tool tiếp nếu cần hoặc trả lời em.)"

    # Hit max iterations
    return {
        "final_text": "Em đã gọi nhiều tool nhưng chưa giải xong. Anh nói cụ thể hơn yêu cầu được không?",
        "tool_calls": tool_calls,
        "model_used": model_used,
        "iterations": iteration,
    }
