"""
Zeni Cloud — CTO Input Filter (Watcher Agent · pre-LLM).

Đối ngẫu với output_filter.py — chặn INPUT độc hại TRƯỚC KHI gọi LLM.
Triết lý: ngăn jailbreak/prompt-injection/secret-exfil từ phía customer.

  Layer 1: Regex jailbreak patterns
            (ignore previous, system prompt reveal, role override, DAN, ...)
  Layer 2: Encoding tricks
            (base64 / hex / unicode escape ẩn payload)
  Layer 3: Out-of-scope topics
            (politic, news, finance trading, ngoài chuyên môn deploy)
  Layer 4: Length sanity check (>20KB input = spam)
  Layer 5: Cross-tenant query detect
            (hỏi về workspace khác, tên customer khác)
  Layer 6: Secret extraction patterns
            (give me GCP key, AWS secret, show /etc/passwd, ...)

Severity:
  - block       → REFUSE ngay, không gọi LLM, ghi violation
  - sanitize    → strip phần độc, vẫn gọi LLM với input đã sạch
  - warn        → vẫn gọi LLM nhưng đánh dấu để watcher post-check

Violation auto-feed vào cto_security_violations → auto_lock service.
"""
from __future__ import annotations

import base64
import binascii
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text as _sql_text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("zeni.cto.input_filter")


# ─────────────────────────────────────────────────────────────
# Patterns
# ─────────────────────────────────────────────────────────────
JAILBREAK_PATTERNS: tuple[str, ...] = (
    # Direct override
    r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|rules?|prompts?)",
    r"disregard\s+(all\s+)?(previous|prior|above|your)\s+(instructions?|rules?)",
    r"forget\s+(everything|all|previous|your\s+instructions?)",
    r"reset\s+(your\s+)?(instructions?|context|memory)",
    # System prompt reveal
    r"(reveal|show|display|print|output|repeat|tell\s+me)\s+(your|the)\s+(system\s+prompt|instructions?|rules?|charter|guidelines?)",
    r"what\s+(are\s+)?your\s+(instructions?|system\s+prompt|rules?)",
    r"print\s+(your|the)\s+(prompt|system)",
    r"echo\s+(your|the)\s+(system|prompt|instructions?)",
    # Role override
    r"you\s+are\s+now\s+(a|an|the)\s+",
    r"pretend\s+(you\s+are|to\s+be)\s+",
    r"act\s+as\s+(a|an|if\s+you\s+are)\s+",
    r"role-?play\s+as\s+",
    r"from\s+now\s+on,?\s+you\s+(are|will)\s+",
    # Famous jailbreak names
    r"\bDAN\s+(mode|prompt)\b",
    r"\bdeveloper\s+mode\b",
    r"\bjailbreak\b",
    r"\bsudo\s+mode\b",
    r"\bgod\s+mode\b",
    r"\bunlocked\s+mode\b",
    r"\bgrandma\s+(trick|exploit)\b",
    # Suffix injection
    r"###\s*(system|admin|root)",
    r"<\|im_start\|>\s*system",
    r"\[INST\]\s*system",
    # Direct prompt manipulation
    r"new\s+instructions?:",
    r"updated\s+rules?:",
    r"override:?\s*",
)

# Secret exfiltration intents
SECRET_PATTERNS: tuple[str, ...] = (
    r"\b(give|show|send|reveal|leak|dump|print|export)\s+(me\s+)?(the\s+)?(gcp|aws|azure|google\s+cloud)\s+(key|secret|credential|service\s+account|sa\s+json|access\s+key)",
    r"\b(give|show|send|reveal|leak|dump|print|export)\s+(me\s+)?(the\s+)?(api[_-]?key|secret[_-]?key|private[_-]?key|access[_-]?token|bearer[_-]?token)",
    r"\b(give|show|provide|cấp|đưa|cho)\s+(me|tôi|anh|em)\s+(.*?\s+)?(gcp|aws|azure|vercel|supabase|firebase|auth0)\s+",
    r"\.env\s+(file|content|values?)",
    r"\bSECRET_KEY\b\s*=",
    r"/etc/passwd",
    r"/etc/shadow",
    r"\bservice\s+account\s+json\b",
    r"\bkubectl\s+get\s+secret",
    r"\bgcloud\s+auth\s+(print-access-token|application-default)",
)

# Out-of-scope topics (deploy CTO only) — strict
OUT_OF_SCOPE_PATTERNS: tuple[str, ...] = (
    r"\b(stock|chứng\s+khoán|trading|forex|đầu\s+tư\s+tài\s+chính)\b",
    r"\b(bitcoin|btc|ethereum|eth|crypto)\s+(price|giá|today|hôm\s+nay|now)\b",
    r"\b(price\s+of\s+bitcoin|giá\s+bitcoin|giá\s+btc)\b",
    r"\b(politic|chính\s+trị|election|bầu\s+cử|government|chính\s+phủ)\b",
    r"\b(news|tin\s+tức|breaking\s+news|latest\s+news)\b",
    r"\b(weather|thời\s+tiết|dự\s+báo\s+thời\s+tiết)\b",
    r"\b(love|romance|dating|hẹn\s+hò|tình\s+yêu)\b",
    r"\b(joke|kể\s+chuyện\s+cười|tell\s+me\s+a\s+joke)\b",
    r"\bwrite\s+(me\s+)?(a\s+)?(poem|story|essay|article)\b",
    # VN: viết ... thơ/truyện/bài/tiểu luận (cho phép từ chèn ở giữa)
    r"\bviết\b[^\n]{0,40}\b(thơ|truyện|tiểu\s+luận|bài\s+văn|bài\s+báo|nhạc)\b",
    r"\b(sáng\s+tác|làm\s+thơ|kể\s+chuyện)\b",
)

# Suspicious encoding (base64 > 32 chars liên tiếp = đáng ngờ)
BASE64_LIKELY = re.compile(r"[A-Za-z0-9+/=]{32,}")
HEX_LIKELY = re.compile(r"\b[0-9a-fA-F]{80,}\b")
UNICODE_ESCAPE = re.compile(r"\\u[0-9a-fA-F]{4}(?:\\u[0-9a-fA-F]{4}){10,}")

# Limits
MAX_INPUT_BYTES = 20 * 1024  # 20KB
MAX_LINE_LENGTH = 4000


@dataclass
class FilterDecision:
    """Quyết định của input filter."""
    action: str                  # "allow" | "sanitize" | "block"
    severity: str                # "info" | "warn" | "high" | "critical"
    reasons: list[str]           # human-readable reasons
    matched_patterns: list[str]  # for audit
    sanitized_input: str         # input đã clean (chỉ valid khi action=sanitize/allow)
    refusal_message: Optional[str] = None  # message trả về user khi block


class CtoInputFilter:
    """
    Pre-LLM filter cho mọi user message vào CTO AI.
    Stateless — gọi `analyze()` cho mỗi message.
    """

    def __init__(self) -> None:
        self._jb = [re.compile(p, re.IGNORECASE) for p in JAILBREAK_PATTERNS]
        self._secret = [re.compile(p, re.IGNORECASE) for p in SECRET_PATTERNS]
        self._oos = [re.compile(p, re.IGNORECASE) for p in OUT_OF_SCOPE_PATTERNS]

    def analyze(
        self,
        user_input: str,
        workspace_id: str,
        other_workspace_names: Optional[list[str]] = None,
    ) -> FilterDecision:
        """
        Analyze input. Trả về FilterDecision.

        Args:
            user_input: chuỗi message từ customer
            workspace_id: workspace của customer (để check cross-tenant)
            other_workspace_names: optional list tên ws khác để check Layer 5

        Returns:
            FilterDecision với action/severity/reasons/sanitized.
        """
        reasons: list[str] = []
        matched: list[str] = []
        sanitized = user_input or ""

        # Layer 4: length sanity FIRST (cheap)
        size = len(sanitized.encode("utf-8", errors="ignore"))
        if size > MAX_INPUT_BYTES:
            return FilterDecision(
                action="block",
                severity="warn",
                reasons=[f"input quá lớn ({size} bytes > {MAX_INPUT_BYTES})"],
                matched_patterns=["length_exceeded"],
                sanitized_input="",
                refusal_message="Yêu cầu quá dài. Vui lòng rút gọn dưới 20KB.",
            )

        if not sanitized.strip():
            return FilterDecision(
                action="block", severity="info",
                reasons=["empty input"], matched_patterns=[],
                sanitized_input="",
                refusal_message="Vui lòng nhập câu hỏi cụ thể về deploy.",
            )

        # Layer 1: Jailbreak patterns → BLOCK
        for rx in self._jb:
            m = rx.search(sanitized)
            if m:
                matched.append(f"jailbreak:{rx.pattern[:50]}")
                reasons.append(f"phát hiện mẫu jailbreak: '{m.group(0)[:60]}'")
        if matched:
            return FilterDecision(
                action="block",
                severity="critical",
                reasons=reasons,
                matched_patterns=matched,
                sanitized_input="",
                refusal_message="Yêu cầu vi phạm phạm vi hỗ trợ. Tôi chỉ hỗ trợ kỹ thuật deploy trên Zeni Cloud.",
            )

        # Layer 6: Secret extraction → BLOCK
        for rx in self._secret:
            m = rx.search(sanitized)
            if m:
                matched.append(f"secret:{rx.pattern[:50]}")
                reasons.append(f"yêu cầu cấp credential cấm: '{m.group(0)[:60]}'")
        if matched:
            return FilterDecision(
                action="block",
                severity="critical",
                reasons=reasons,
                matched_patterns=matched,
                sanitized_input="",
                refusal_message=(
                    "Tôi không cung cấp credential của bên thứ ba (GCP/AWS/Vercel/...). "
                    "Để deploy lên Zeni, tôi có thể cấp Zeni PAT hoặc Image Whitelist trong workspace của anh — anh cần loại nào?"
                ),
            )

        # Layer 3: Out-of-scope topics → BLOCK (lịch sự)
        for rx in self._oos:
            m = rx.search(sanitized)
            if m:
                matched.append(f"oos:{rx.pattern[:50]}")
                reasons.append(f"out-of-scope: '{m.group(0)[:60]}'")
        if matched:
            return FilterDecision(
                action="block",
                severity="warn",
                reasons=reasons,
                matched_patterns=matched,
                sanitized_input="",
                refusal_message=(
                    "Tôi là CTO AI Zeni Cloud, chỉ hỗ trợ kỹ thuật deploy/dịch vụ Zeni. "
                    "Chủ đề này ngoài phạm vi — anh có vấn đề gì về project đang deploy không?"
                ),
            )

        # Layer 2: Encoding tricks → SANITIZE (strip suspicious blocks)
        b64_matches = BASE64_LIKELY.findall(sanitized)
        if b64_matches:
            for b in b64_matches:
                try:
                    decoded = base64.b64decode(b, validate=True).decode("utf-8", errors="ignore")
                    if any(rx.search(decoded) for rx in self._jb + self._secret):
                        # Decoded payload là jailbreak → BLOCK
                        return FilterDecision(
                            action="block",
                            severity="critical",
                            reasons=["base64 payload chứa jailbreak sau decode"],
                            matched_patterns=["base64_encoded_jailbreak"],
                            sanitized_input="",
                            refusal_message="Phát hiện payload mã hóa đáng ngờ. Yêu cầu bị từ chối.",
                        )
                except (binascii.Error, ValueError, UnicodeDecodeError):
                    pass
                # Strip block (giữ length nguyên để LLM không bị lệch context)
                sanitized = sanitized.replace(b, "[REDACTED:base64]")
                reasons.append("đã strip base64 block dài")
                matched.append("base64_stripped")

        hex_matches = HEX_LIKELY.findall(sanitized)
        if hex_matches:
            for h in hex_matches:
                sanitized = sanitized.replace(h, "[REDACTED:hex]")
                reasons.append("đã strip hex block dài")
                matched.append("hex_stripped")

        if UNICODE_ESCAPE.search(sanitized):
            sanitized = UNICODE_ESCAPE.sub("[REDACTED:unicode]", sanitized)
            reasons.append("đã strip unicode escape sequence")
            matched.append("unicode_stripped")

        # Layer 5: Cross-tenant query
        if other_workspace_names:
            lowered = sanitized.lower()
            cross_hits: list[str] = []
            for nm in other_workspace_names:
                if not nm or len(nm) < 4:
                    continue
                pat = r"\b" + re.escape(nm.lower()) + r"\b"
                if re.search(pat, lowered):
                    cross_hits.append(nm)
                    if len(cross_hits) >= 3:
                        break
            if cross_hits:
                return FilterDecision(
                    action="block",
                    severity="critical",
                    reasons=[f"hỏi về workspace khác: {cross_hits}"],
                    matched_patterns=["cross_tenant_query"],
                    sanitized_input="",
                    refusal_message=(
                        "Tôi chỉ hỗ trợ workspace anh đang đăng nhập. "
                        "Không thể tham chiếu workspace khác."
                    ),
                )

        # Line length sanity
        for line in sanitized.splitlines():
            if len(line) > MAX_LINE_LENGTH:
                matched.append("oversized_line")
                reasons.append(f"dòng quá dài ({len(line)} chars > {MAX_LINE_LENGTH})")
                sanitized = "\n".join(
                    (ln[:MAX_LINE_LENGTH] + "...[truncated]") if len(ln) > MAX_LINE_LENGTH else ln
                    for ln in sanitized.splitlines()
                )
                break

        # All clear
        action = "sanitize" if reasons else "allow"
        severity = "warn" if reasons else "info"
        return FilterDecision(
            action=action,
            severity=severity,
            reasons=reasons,
            matched_patterns=matched,
            sanitized_input=sanitized,
        )

    # ─────────────────────────────────────────────────────────────
    # Persistence — log violation vào DB cho auto_lock service đọc
    # ─────────────────────────────────────────────────────────────
    @staticmethod
    async def log_violation(
        db: AsyncSession,
        workspace_id: str,
        user_id: Optional[uuid.UUID],
        session_id: Optional[str],
        decision: FilterDecision,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        excerpt: str = "",
    ) -> None:
        """
        Ghi 1 violation vào cto_security_violations. Best-effort.
        Bảng này được auto_lock service đọc → đếm threshold → lock.
        """
        if decision.action == "allow":
            return  # không log allow
        try:
            await db.execute(_sql_text("""
                INSERT INTO cto_security_violations
                    (id, workspace_id, user_id, session_id, action,
                     severity, reasons, matched_patterns, ip_address, user_agent, excerpt)
                VALUES
                    (:id, :ws, :uid, :sid, :act,
                     :sev, :reasons, :patterns, :ip, :ua, :ex)
            """), {
                "id": str(uuid.uuid4()),
                "ws": workspace_id,
                "uid": str(user_id) if user_id else None,
                "sid": session_id,
                "act": decision.action,
                "sev": decision.severity,
                "reasons": "; ".join(decision.reasons)[:1000],
                "patterns": ",".join(decision.matched_patterns)[:500],
                "ip": (ip_address or "")[:64],
                "ua": (user_agent or "")[:256],
                "ex": (excerpt or "")[:500],
            })
            await db.commit()
        except Exception as e:
            log.warning("[input_filter] log_violation failed: %s", e)
            try:
                await db.rollback()
            except Exception:
                pass


__all__ = ["CtoInputFilter", "FilterDecision"]
