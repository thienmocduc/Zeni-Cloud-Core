"""
Zeni Cloud Core — Anonymizer Service.

Pipeline 5 bước để chuyển raw record có PII → record an toàn cho training / analytics:

    1. strip_pii            — regex replace email, phone, tax_id, bank, name
    2. apply_k_anonymity    — group theo quasi-identifiers, suppress nếu < k
    3. add_differential_privacy — Laplace / Gaussian noise
    4. tokenize_ids         — replace stable IDs (customer_id, user_id, ...) bằng UUID token
    5. validate_no_pii      — scan lần cuối, return (clean, found_patterns)

Class `Anonymizer` wrap cả 5, có method `process(record)` trả None nếu
validation fail (record không an toàn để publish).

Note: numpy là optional. Nếu không cài, fallback sang random.gauss().
"""
from __future__ import annotations

import logging
import re
import uuid
from collections import defaultdict
from typing import Any, Iterable

log = logging.getLogger("zeni.services.anonymizer")

# ── Regex patterns ───────────────────────────────────────────
# Email RFC-lite
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.\w{2,}\b")
# Phone VN: +84xxxxxxxxx hoặc 0xxxxxxxxx (10-11 chữ số)
_PHONE_VN_RE = re.compile(r"\b(?:\+84|0)\d{9,10}\b")
# Tax ID VN: 10 chữ số, optional -3-digit branch suffix
_TAX_ID_RE = re.compile(r"\b\d{10}(?:-\d{3})?\b")
# Bank account VN: 8-16 chữ số liên tiếp (đặt SAU tax_id để không nuốt tax_id)
_BANK_RE = re.compile(r"\b\d{8,16}\b")
# Credit card: 16 chữ số có/không space-dash
_CREDIT_CARD_RE = re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b")
# Tên VN phổ biến: 5 họ chính + 1-3 từ tên đệm/chính (mỗi từ có chữ cái đầu hoa)
_VN_NAME_RE = re.compile(
    r"\b(?:Nguy[eễ]n|Tr[aầ]n|L[eê]|Ph[aạ]m|Ho[aà]ng|Hu[yỳ]nh|V[oõũ]|Đ[aặ]ng|B[uùủ]i|Đ[oỗ]|Ng[oôồộ]|D[uươ]ng)"
    r"(?:\s+[A-ZĐÀ-Ỹ][a-zđà-ỹ]+){1,3}\b"
)

# Quasi-identifiers thường dùng cho k-anonymity
_DEFAULT_QUASI_IDS = ("age_band", "city", "gender", "industry", "company_size")
# ID fields cần tokenize
_ID_FIELDS = ("customer_id", "user_id", "workspace_id", "account_id", "member_id")


# ─────────────────────────────────────────────────────────────
# Step 1: Strip PII
# ─────────────────────────────────────────────────────────────
def strip_pii(text: str) -> str:
    """
    Replace PII patterns trong text với placeholder.
    Thứ tự quan trọng: credit card → email → phone → tax_id → bank → name
    (Bank pattern khá rộng — chạy sau cùng để không nuốt tax_id / phone.)
    """
    if not text or not isinstance(text, str):
        return text
    out = text
    out = _CREDIT_CARD_RE.sub("[CREDIT_CARD]", out)
    out = _EMAIL_RE.sub("[EMAIL]", out)
    out = _PHONE_VN_RE.sub("[PHONE]", out)
    out = _TAX_ID_RE.sub("[TAX_ID]", out)
    out = _BANK_RE.sub("[BANK]", out)
    out = _VN_NAME_RE.sub("[NAME]", out)
    return out


# ─────────────────────────────────────────────────────────────
# Step 2: k-anonymity
# ─────────────────────────────────────────────────────────────
def apply_k_anonymity(
    records: list[dict],
    k: int = 5,
    quasi_identifiers: Iterable[str] = _DEFAULT_QUASI_IDS,
) -> list[dict]:
    """
    Suppress records mà group theo quasi-identifiers có size < k.
    Trả về danh sách record an toàn (group đã đạt k-anonymity).
    """
    if k < 1:
        raise ValueError("k phải >= 1")
    if not records:
        return []

    qid_keys = tuple(quasi_identifiers)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for rec in records:
        key = tuple(rec.get(q, None) for q in qid_keys)
        groups[key].append(rec)

    safe: list[dict] = []
    suppressed = 0
    for key, group in groups.items():
        if len(group) >= k:
            safe.extend(group)
        else:
            suppressed += len(group)
    if suppressed:
        log.info("[k-anonymity] suppressed %d records (k=%d)", suppressed, k)
    return safe


# ─────────────────────────────────────────────────────────────
# Step 3: Differential Privacy
# ─────────────────────────────────────────────────────────────
def add_differential_privacy(value: float, epsilon: float = 1.0) -> float:
    """
    Thêm Laplace noise scale = 1/epsilon (sensitivity = 1 mặc định).
    epsilon nhỏ → noise lớn → riêng tư hơn.
    Nếu numpy không cài → fallback Gaussian (yếu hơn nhưng vẫn ổn cho aggregate).
    """
    if epsilon <= 0:
        raise ValueError("epsilon phải > 0")
    try:
        import numpy as _np  # type: ignore
        noise = float(_np.random.laplace(0.0, 1.0 / epsilon))
    except ImportError:
        import random
        # Approximate Laplace by transforming uniform: -b * sgn(u) * ln(1 - 2|u|)
        # Hoặc đơn giản dùng gauss với sigma = sqrt(2)/epsilon
        import math
        sigma = math.sqrt(2.0) / epsilon
        noise = random.gauss(0.0, sigma)
    return float(value) + noise


# ─────────────────────────────────────────────────────────────
# Step 4: Tokenize stable IDs
# ─────────────────────────────────────────────────────────────
def tokenize_ids(record: dict, fields: Iterable[str] = _ID_FIELDS) -> dict:
    """
    Replace mọi field ID (customer_id, user_id, ...) bằng UUID token mới.
    Token random — không deterministic, không reverse được.
    Trả về dict mới (không mutate input).
    """
    if not isinstance(record, dict):
        return record
    out = dict(record)
    for f in fields:
        if f in out and out[f] is not None:
            out[f] = f"tok_{uuid.uuid4().hex}"
    return out


# ─────────────────────────────────────────────────────────────
# Step 5: Validate no residual PII
# ─────────────────────────────────────────────────────────────
def validate_no_pii(text: str) -> tuple[bool, list[str]]:
    """
    Scan lần cuối — nếu vẫn match bất kỳ pattern PII nào thì FAIL.
    Trả về (is_clean, list_of_pattern_names_that_matched).
    """
    if not text or not isinstance(text, str):
        return True, []
    found: list[str] = []
    if _CREDIT_CARD_RE.search(text):
        found.append("credit_card")
    if _EMAIL_RE.search(text):
        found.append("email")
    if _PHONE_VN_RE.search(text):
        found.append("phone_vn")
    if _TAX_ID_RE.search(text):
        found.append("tax_id")
    # Bank pattern rộng — chỉ flag nếu xuất hiện rất rõ ràng (chuỗi dài 12+ chữ số)
    if re.search(r"\b\d{12,16}\b", text):
        found.append("bank")
    if _VN_NAME_RE.search(text):
        found.append("vn_name")
    return (len(found) == 0), found


# ─────────────────────────────────────────────────────────────
# Wrapper class
# ─────────────────────────────────────────────────────────────
class Anonymizer:
    """
    Pipeline tiện dụng wrap cả 5 bước.

    Usage:
        anon = Anonymizer(k=5, epsilon=1.0)
        safe = anon.process(record)   # None nếu fail validation
        safe_list = anon.process_batch(records)
    """

    def __init__(
        self,
        k: int = 5,
        epsilon: float = 1.0,
        quasi_identifiers: Iterable[str] = _DEFAULT_QUASI_IDS,
        id_fields: Iterable[str] = _ID_FIELDS,
        numeric_dp_fields: Iterable[str] = (),
    ):
        self.k = k
        self.epsilon = epsilon
        self.quasi_identifiers = tuple(quasi_identifiers)
        self.id_fields = tuple(id_fields)
        self.numeric_dp_fields = tuple(numeric_dp_fields)

    def process(self, record: dict) -> dict | None:
        """
        Chạy steps 1, 3, 4, 5 trên 1 record.
        (Step 2 k-anonymity cần batch, dùng `process_batch`.)
        Trả None nếu validate_no_pii fail.
        """
        if not isinstance(record, dict):
            return None

        # Step 1: strip PII trong mọi text field
        stripped: dict[str, Any] = {}
        for k_, v in record.items():
            if isinstance(v, str):
                stripped[k_] = strip_pii(v)
            else:
                stripped[k_] = v

        # Step 3: differential privacy cho numeric fields đã chỉ định
        for f in self.numeric_dp_fields:
            if f in stripped and isinstance(stripped[f], (int, float)):
                stripped[f] = add_differential_privacy(float(stripped[f]), self.epsilon)

        # Step 4: tokenize IDs
        tokenized = tokenize_ids(stripped, self.id_fields)

        # Step 5: validate
        for v in tokenized.values():
            if isinstance(v, str):
                clean, found = validate_no_pii(v)
                if not clean:
                    log.warning("[anonymizer] validation failed: %s", found)
                    return None
        return tokenized

    def process_batch(self, records: list[dict]) -> list[dict]:
        """
        Full pipeline với k-anonymity:
            process từng record → apply k-anonymity trên kết quả.
        Trả về list các record đã an toàn để publish.
        """
        if not records:
            return []
        # Step 1, 3, 4, 5 per record
        intermediate: list[dict] = []
        for r in records:
            p = self.process(r)
            if p is not None:
                intermediate.append(p)
        # Step 2: k-anonymity trên cả batch
        return apply_k_anonymity(intermediate, k=self.k, quasi_identifiers=self.quasi_identifiers)


__all__ = [
    "Anonymizer",
    "strip_pii",
    "apply_k_anonymity",
    "add_differential_privacy",
    "tokenize_ids",
    "validate_no_pii",
]
