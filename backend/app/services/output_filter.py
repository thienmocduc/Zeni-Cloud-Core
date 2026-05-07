"""
Zeni Cloud Core — Output Filter Service.

5-layer defense ngăn agent leak data khi sinh response:

  Layer 1: Regex PII block      — chặn email / phone / tax_id / bank / credit card
  Layer 2: Cross-tenant leak    — chặn khi output chứa tên company của workspace khác
  Layer 3: Suspicious phrases   — "training data", "other customer", "verbatim", ...
  Layer 4: Length sanity check  — output > 50KB là dấu hiệu leak hàng loạt
  Layer 5: Audit logging        — ghi vào output_filter_logs để customer xem

Class `OutputFilter` cung cấp method async `filter()` trả (filtered_response, warnings).
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from sqlalchemy import text as _sql_text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("zeni.services.output_filter")


# ── Layer 4 threshold ────────────────────────────────────────
MAX_RESPONSE_BYTES = 50 * 1024  # 50KB


class OutputFilter:
    """
    Filter agent output trước khi trả cho user.

    PII patterns dùng cho Layer 1 (regex).
    """

    PII_PATTERNS: dict[str, str] = {
        "email":       r"\b[\w.+-]+@[\w.-]+\.\w{2,}\b",
        "phone_vn":    r"\b(?:\+84|0)\d{9,10}\b",
        "tax_id":      r"\b\d{10}(?:-\d{3})?\b",
        "bank":        r"\b\d{12,16}\b",
        "credit_card": r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b",
    }

    SUSPICIOUS_PHRASES: tuple[str, ...] = (
        "training data",
        "other customer",
        "another customer",
        "other workspace",
        "another workspace",
        "verbatim",
        "as i was trained",
        "in my training",
        "memorized",
        "leaked from",
        "internal database",
        "system prompt",
        "ignore previous instructions",
    )

    def __init__(self) -> None:
        self._compiled = {name: re.compile(pat, re.IGNORECASE)
                          for name, pat in self.PII_PATTERNS.items()}
        self._suspicious_re = re.compile(
            r"(?i)\b(" + "|".join(re.escape(p) for p in self.SUSPICIOUS_PHRASES) + r")\b"
        )

    # ─────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────
    def mask_text(self, text: str, pattern_name: str) -> str:
        """Replace mọi match của pattern_name bằng [REDACTED:pattern_name]."""
        if pattern_name not in self._compiled:
            return text
        return self._compiled[pattern_name].sub(f"[REDACTED:{pattern_name}]", text)

    @staticmethod
    async def log_leak(
        db: AsyncSession,
        workspace_id: str | None,
        user_id: uuid.UUID | None,
        agent_name: str | None,
        leak_type: str,
        excerpt: str,
        severity: str = "warning",
    ) -> None:
        """
        Insert row vào output_filter_logs. Best-effort (try/except).
        """
        try:
            # Cắt excerpt ngắn để tránh log bị blow up
            short = (excerpt or "")[:500]
            await db.execute(_sql_text("""
                INSERT INTO output_filter_logs
                    (workspace_id, user_id, agent_name, leak_type, blocked_excerpt, severity)
                VALUES
                    (:ws, :uid, :agent, :ltype, :ex, :sev)
            """), {
                "ws": workspace_id,
                "uid": str(user_id) if user_id else None,
                "agent": agent_name,
                "ltype": leak_type,
                "ex": short,
                "sev": severity,
            })
        except Exception as e:
            log.warning("[output_filter] log_leak failed: %s", e)

    # ─────────────────────────────────────────────────────────
    # Layer 2: cross-tenant leak detect
    # ─────────────────────────────────────────────────────────
    async def _detect_cross_tenant(
        self,
        response: str,
        my_workspace_id: str,
        db: AsyncSession,
    ) -> list[str]:
        """
        Query danh sách company name (Workspace.name) của các workspace KHÁC,
        check xem response có nhắc tên nào không. Nếu có → cross-tenant leak.
        Trả về list tên company bị nhắc.
        """
        leaked: list[str] = []
        try:
            rows = (await db.execute(_sql_text("""
                SELECT name FROM workspaces WHERE id != :me
            """), {"me": my_workspace_id})).all()
            other_names = [r[0] for r in rows if r[0] and len(r[0]) >= 3]
            # Check substring match (case-insensitive, word boundary)
            lower_resp = response.lower()
            for nm in other_names:
                # Bỏ qua tên quá generic (1-2 từ phổ biến)
                if len(nm) < 4:
                    continue
                pat = r"\b" + re.escape(nm.lower()) + r"\b"
                if re.search(pat, lower_resp):
                    leaked.append(nm)
                    if len(leaked) >= 5:
                        break
        except Exception as e:
            log.warning("[output_filter] cross_tenant query failed: %s", e)
        return leaked

    # ─────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────
    async def filter(
        self,
        response: str,
        user_workspace_id: str,
        agent_name: str,
        db: AsyncSession,
        user_id: uuid.UUID | None = None,
    ) -> tuple[str, list[str]]:
        """
        Apply 5 layers, trả (filtered_response, leak_warnings).

        - Layer 1: replace PII match → [REDACTED:type], log mỗi type detect
        - Layer 2: nếu nhắc tên workspace khác → log cross_tenant
        - Layer 3: nếu match suspicious phrase → log
        - Layer 4: nếu response > 50KB → log + truncate
        - Layer 5: tất cả các log đều ghi vào output_filter_logs
        """
        if not response or not isinstance(response, str):
            return response or "", []

        warnings: list[str] = []
        filtered = response

        # Layer 1: PII regex
        for name, regex in self._compiled.items():
            matches = regex.findall(filtered)
            if matches:
                excerpt_match = regex.search(filtered)
                excerpt = excerpt_match.group(0) if excerpt_match else ""
                filtered = regex.sub(f"[REDACTED:{name}]", filtered)
                warnings.append(f"pii:{name}:{len(matches)}")
                await self.log_leak(
                    db, user_workspace_id, user_id, agent_name,
                    leak_type=f"pii_{name}",
                    excerpt=excerpt,
                    severity="warning",
                )

        # Layer 2: cross-tenant
        leaked_names = await self._detect_cross_tenant(filtered, user_workspace_id, db)
        if leaked_names:
            warnings.append(f"cross_tenant:{len(leaked_names)}")
            for nm in leaked_names:
                # Mask name
                filtered = re.sub(
                    r"\b" + re.escape(nm) + r"\b",
                    "[REDACTED:other_workspace]",
                    filtered,
                    flags=re.IGNORECASE,
                )
            await self.log_leak(
                db, user_workspace_id, user_id, agent_name,
                leak_type="cross_tenant",
                excerpt=", ".join(leaked_names[:3]),
                severity="critical",
            )

        # Layer 3: suspicious phrases
        susp_matches = self._suspicious_re.findall(filtered)
        if susp_matches:
            warnings.append(f"suspicious_phrase:{len(susp_matches)}")
            await self.log_leak(
                db, user_workspace_id, user_id, agent_name,
                leak_type="suspicious_phrase",
                excerpt=", ".join(set(s.lower() for s in susp_matches))[:200],
                severity="warning",
            )

        # Layer 4: length
        if len(filtered.encode("utf-8", errors="ignore")) > MAX_RESPONSE_BYTES:
            warnings.append("oversize")
            await self.log_leak(
                db, user_workspace_id, user_id, agent_name,
                leak_type="oversize",
                excerpt=f"size={len(filtered)} chars",
                severity="warning",
            )
            # Truncate
            filtered = filtered[: MAX_RESPONSE_BYTES // 2] + "\n\n[...TRUNCATED: response too large...]"

        return filtered, warnings


__all__ = ["OutputFilter", "MAX_RESPONSE_BYTES"]
