"""
Zeni Books Engine — Vietnamese accounting core (VAS).

Cung cấp các logic tính toán & hạch toán kế toán kép theo chuẩn:
  - generate_invoice_number / generate_expense_number / generate_journal_number
  - validate_tax_code               (MST 10/13 chữ số)
  - ensure_chart_of_accounts        (auto-seed COA cho workspace mới)
  - post_invoice_journal            (Nợ 131 / Có 511 + Có 333)
  - post_invoice_purchase_journal   (Nợ 156 + Nợ 133 / Có 331)
  - reverse_invoice_journal         (đảo bút toán khi cancel)
  - post_expense_journal            (Nợ 642/641/.. + Nợ 133 / Có 111/112/331)
  - calculate_balance_sheet         (Bảng cân đối kế toán theo as_of_date)
  - calculate_pnl                   (Báo cáo kết quả kinh doanh theo period)
  - calculate_cash_flow             (Báo cáo lưu chuyển tiền tệ — gián tiếp đơn giản)
  - calculate_vat_report            (Tờ khai thuế GTGT quý — output - input)
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("zeni.books.engine")

# ──────────────────────────────────────────────────────────────────────────────
# Default Vietnamese VAS chart of accounts (Thông tư 200/2014/TT-BTC).
# Dùng cho ensure_chart_of_accounts() khi workspace mới chưa có COA.
# Phải khớp với migration 025_zeni_books.sql để idempotent.
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_COA: list[tuple[str, str, str, str]] = [
    # (code, name_vi, name_en, account_type)
    ("111", "Tiền mặt", "Cash on hand", "asset"),
    ("112", "Tiền gửi ngân hàng", "Bank deposits", "asset"),
    ("113", "Tiền đang chuyển", "Cash in transit", "asset"),
    ("121", "Chứng khoán kinh doanh", "Trading securities", "asset"),
    ("128", "Đầu tư nắm giữ đến ngày đáo hạn", "Held-to-maturity investments", "asset"),
    ("131", "Phải thu của khách hàng", "Accounts receivable", "asset"),
    ("133", "Thuế GTGT được khấu trừ", "VAT input deductible", "asset"),
    ("138", "Phải thu khác", "Other receivables", "asset"),
    ("141", "Tạm ứng", "Advances to employees", "asset"),
    ("152", "Nguyên liệu, vật liệu", "Raw materials", "asset"),
    ("153", "Công cụ, dụng cụ", "Tools and supplies", "asset"),
    ("154", "Chi phí sản xuất, kinh doanh dở dang", "Work in progress", "asset"),
    ("155", "Thành phẩm", "Finished goods", "asset"),
    ("156", "Hàng hoá", "Goods for sale", "asset"),
    ("157", "Hàng gửi đi bán", "Goods on consignment", "asset"),
    ("211", "Tài sản cố định hữu hình", "Tangible fixed assets", "asset"),
    ("213", "Tài sản cố định vô hình", "Intangible fixed assets", "asset"),
    ("214", "Hao mòn tài sản cố định", "Accumulated depreciation", "asset"),
    ("228", "Đầu tư khác", "Other long-term investments", "asset"),
    ("242", "Chi phí trả trước", "Prepaid expenses", "asset"),
    ("331", "Phải trả cho người bán", "Accounts payable", "liability"),
    ("333", "Thuế và các khoản phải nộp Nhà nước", "Taxes payable to State", "liability"),
    ("334", "Phải trả người lao động", "Salaries payable", "liability"),
    ("335", "Chi phí phải trả", "Accrued expenses", "liability"),
    ("338", "Phải trả, phải nộp khác", "Other payables", "liability"),
    ("341", "Vay và nợ thuê tài chính", "Loans and finance lease", "liability"),
    ("352", "Dự phòng phải trả", "Provisions for liabilities", "liability"),
    ("411", "Vốn đầu tư của chủ sở hữu", "Owner equity", "equity"),
    ("414", "Quỹ đầu tư phát triển", "Investment fund", "equity"),
    ("418", "Các quỹ khác thuộc vốn chủ sở hữu", "Other owner funds", "equity"),
    ("421", "Lợi nhuận sau thuế chưa phân phối", "Retained earnings", "equity"),
    ("511", "Doanh thu bán hàng và cung cấp dịch vụ", "Sales revenue", "revenue"),
    ("515", "Doanh thu hoạt động tài chính", "Financial revenue", "revenue"),
    ("521", "Các khoản giảm trừ doanh thu", "Sales deductions", "revenue"),
    ("621", "Chi phí nguyên liệu, vật liệu trực tiếp", "Direct material costs", "expense"),
    ("622", "Chi phí nhân công trực tiếp", "Direct labor costs", "expense"),
    ("627", "Chi phí sản xuất chung", "Manufacturing overhead", "expense"),
    ("632", "Giá vốn hàng bán", "Cost of goods sold", "expense"),
    ("635", "Chi phí tài chính", "Financial expenses", "expense"),
    ("641", "Chi phí bán hàng", "Selling expenses", "expense"),
    ("642", "Chi phí quản lý doanh nghiệp", "Admin expenses", "expense"),
    ("711", "Thu nhập khác", "Other income", "revenue"),
    ("811", "Chi phí khác", "Other expenses", "expense"),
    ("821", "Chi phí thuế thu nhập doanh nghiệp", "Corporate income tax expense", "expense"),
    ("911", "Xác định kết quả kinh doanh", "Income summary", "equity"),
]

# Mapping category → tài khoản chi phí mặc định (cho expense journal)
EXPENSE_CATEGORY_TO_ACCOUNT: dict[str, str] = {
    "salary": "642",        # Lương admin → 642 (nếu sản xuất → 622)
    "rent": "642",          # Tiền thuê văn phòng
    "utilities": "642",     # Điện, nước, internet
    "marketing": "641",     # Chi phí bán hàng
    "tax": "821",           # Thuế TNDN
    "supplies": "642",      # Vật tư văn phòng
    "transport": "641",     # Vận chuyển
    "service": "642",       # Dịch vụ ngoài
    "interest": "635",      # Chi phí lãi vay
    "other": "811",         # Chi phí khác
}


# ──────────────────────────────────────────────────────────────────────────────
# Numerics — luôn quantize 2 chữ số sau dấu phẩy theo VND
# ──────────────────────────────────────────────────────────────────────────────
def _q(amount: Decimal | float | int) -> Decimal:
    """Quantize sang 2 chữ số sau dấu phẩy — chuẩn VND."""
    if not isinstance(amount, Decimal):
        amount = Decimal(str(amount))
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ──────────────────────────────────────────────────────────────────────────────
# Validators
# ──────────────────────────────────────────────────────────────────────────────
_TAX_CODE_RE = re.compile(r"^\d{10}(-\d{3})?$|^\d{10,13}$")


def validate_tax_code(mst: str | None) -> bool:
    """Validate Mã Số Thuế (MST) Việt Nam — 10 chữ số (DN) hoặc 13 chữ số (chi nhánh).

    Cho phép:
      - 10 chữ số:           '0123456789'
      - 13 chữ số liền:      '0123456789001'
      - Format dấu gạch:     '0123456789-001'
    """
    if not mst:
        return True  # MST optional
    return bool(_TAX_CODE_RE.match(mst.strip()))


# ──────────────────────────────────────────────────────────────────────────────
# Number generators (sequential per workspace per month)
# ──────────────────────────────────────────────────────────────────────────────
async def _next_sequence(
    db: AsyncSession,
    *,
    workspace_id: str,
    table: str,
    column: str,
    prefix: str,
) -> str:
    """Generate next sequential number cho workspace + tháng hiện tại.

    Format: PREFIX-YYYYMM-### (ví dụ HD-202604-001).
    Query MAX(...) trong tháng hiện tại để tìm seq tiếp theo.
    """
    now = datetime.now()
    month_tag = now.strftime("%Y%m")
    pattern = f"{prefix}-{month_tag}-%"
    sql = f"""
        SELECT {column} FROM {table}
        WHERE workspace_id = :ws AND {column} LIKE :pat
        ORDER BY {column} DESC LIMIT 1
    """
    row = (await db.execute(text(sql), {"ws": workspace_id, "pat": pattern})).first()
    if row and row[0]:
        last = row[0]
        try:
            seq = int(last.rsplit("-", 1)[1]) + 1
        except (ValueError, IndexError):
            seq = 1
    else:
        seq = 1
    return f"{prefix}-{month_tag}-{seq:03d}"


async def generate_invoice_number(db: AsyncSession, workspace_id: str) -> str:
    return await _next_sequence(
        db, workspace_id=workspace_id,
        table="books_invoices", column="invoice_number", prefix="HD",
    )


async def generate_expense_number(db: AsyncSession, workspace_id: str) -> str:
    return await _next_sequence(
        db, workspace_id=workspace_id,
        table="books_expenses", column="expense_number", prefix="PC",
    )


async def generate_journal_number(db: AsyncSession, workspace_id: str) -> str:
    return await _next_sequence(
        db, workspace_id=workspace_id,
        table="books_journal_entries", column="entry_number", prefix="NK",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Chart of Accounts — auto-seed cho workspace mới
# ──────────────────────────────────────────────────────────────────────────────
async def ensure_chart_of_accounts(db: AsyncSession, workspace_id: str) -> int:
    """Đảm bảo workspace có đủ default VAS chart of accounts.

    Chỉ insert những code chưa có (ON CONFLICT DO NOTHING).
    Trả về số tài khoản mới được seed.
    """
    existing = (await db.execute(text("""
        SELECT COUNT(*) FROM books_chart_of_accounts WHERE workspace_id = :ws
    """), {"ws": workspace_id})).scalar() or 0

    if existing >= len(DEFAULT_COA):
        return 0

    seeded = 0
    for code, name, name_en, account_type in DEFAULT_COA:
        result = await db.execute(text("""
            INSERT INTO books_chart_of_accounts (workspace_id, code, name, name_en, account_type)
            VALUES (:ws, :code, :name, :en, :t)
            ON CONFLICT (workspace_id, code) DO NOTHING
        """), {"ws": workspace_id, "code": code, "name": name, "en": name_en, "t": account_type})
        # SQLAlchemy returns rowcount; in Postgres ON CONFLICT DO NOTHING returns 0/1
        if result.rowcount and result.rowcount > 0:
            seeded += 1
    if seeded:
        log.info("Seeded %d COA accounts for workspace %s", seeded, workspace_id)
    return seeded


# ──────────────────────────────────────────────────────────────────────────────
# Journal posting helpers (kế toán kép — debit = credit luôn)
# ──────────────────────────────────────────────────────────────────────────────
async def _create_journal_entry(
    db: AsyncSession,
    *,
    workspace_id: str,
    entry_date: date,
    description: str,
    source_type: str,
    source_id: int,
    lines: list[dict[str, Any]],
    posted: bool = True,
) -> int:
    """Tạo journal entry + lines. Validate debit = credit.

    `lines` shape: [{"account_code","debit","credit","description"?}, ...]
    """
    total_debit = sum(_q(l.get("debit", 0)) for l in lines)
    total_credit = sum(_q(l.get("credit", 0)) for l in lines)
    if total_debit != total_credit:
        raise ValueError(
            f"Bút toán không cân bằng: Nợ {total_debit} ≠ Có {total_credit}"
        )
    if total_debit == 0:
        raise ValueError("Bút toán phải có ít nhất 1 dòng có giá trị > 0")

    entry_number = await generate_journal_number(db, workspace_id)
    row = (await db.execute(text("""
        INSERT INTO books_journal_entries
            (workspace_id, entry_number, entry_date, description,
             source_type, source_id, total_debit, total_credit, posted)
        VALUES (:ws, :num, :ed, :desc, :st, :sid, :td, :tc, :p)
        RETURNING id
    """), {
        "ws": workspace_id, "num": entry_number, "ed": entry_date,
        "desc": description, "st": source_type, "sid": source_id,
        "td": total_debit, "tc": total_credit, "p": posted,
    })).first()
    entry_id = row[0]

    for line in lines:
        await db.execute(text("""
            INSERT INTO books_journal_lines
                (entry_id, account_code, debit, credit, description)
            VALUES (:eid, :acc, :d, :c, :desc)
        """), {
            "eid": entry_id,
            "acc": line["account_code"],
            "d": _q(line.get("debit", 0)),
            "c": _q(line.get("credit", 0)),
            "desc": line.get("description"),
        })

    return entry_id


async def post_invoice_journal(
    db: AsyncSession,
    *,
    workspace_id: str,
    invoice_id: int,
    invoice_number: str,
    issue_date: date,
    subtotal: Decimal,
    vat_amount: Decimal,
    total: Decimal,
    customer_name: str | None = None,
) -> int:
    """Hạch toán bán hàng (chuẩn VAS):
      Nợ TK 131 (Phải thu khách hàng) = total
        Có TK 511 (Doanh thu)         = subtotal
        Có TK 333 (Thuế GTGT phải nộp)= vat_amount
    """
    desc = f"Hoá đơn bán hàng {invoice_number}"
    if customer_name:
        desc += f" — {customer_name}"

    lines = [
        {"account_code": "131", "debit": total, "credit": 0,
         "description": "Phải thu khách hàng"},
        {"account_code": "511", "debit": 0, "credit": subtotal,
         "description": "Doanh thu bán hàng"},
    ]
    if _q(vat_amount) > 0:
        lines.append({
            "account_code": "333", "debit": 0, "credit": vat_amount,
            "description": "Thuế GTGT đầu ra phải nộp",
        })

    return await _create_journal_entry(
        db,
        workspace_id=workspace_id,
        entry_date=issue_date,
        description=desc,
        source_type="invoice",
        source_id=invoice_id,
        lines=lines,
        posted=True,
    )


async def post_invoice_purchase_journal(
    db: AsyncSession,
    *,
    workspace_id: str,
    invoice_id: int,
    invoice_number: str,
    issue_date: date,
    subtotal: Decimal,
    vat_amount: Decimal,
    total: Decimal,
    supplier_name: str | None = None,
) -> int:
    """Hạch toán mua hàng:
      Nợ TK 156 (Hàng hoá)            = subtotal
      Nợ TK 133 (Thuế GTGT khấu trừ)  = vat_amount
        Có TK 331 (Phải trả người bán) = total
    """
    desc = f"Hoá đơn mua hàng {invoice_number}"
    if supplier_name:
        desc += f" — {supplier_name}"

    lines = [
        {"account_code": "156", "debit": subtotal, "credit": 0,
         "description": "Nhập kho hàng hoá"},
    ]
    if _q(vat_amount) > 0:
        lines.append({
            "account_code": "133", "debit": vat_amount, "credit": 0,
            "description": "Thuế GTGT đầu vào được khấu trừ",
        })
    lines.append({
        "account_code": "331", "debit": 0, "credit": total,
        "description": "Phải trả người bán",
    })

    return await _create_journal_entry(
        db,
        workspace_id=workspace_id,
        entry_date=issue_date,
        description=desc,
        source_type="invoice",
        source_id=invoice_id,
        lines=lines,
        posted=True,
    )


async def reverse_invoice_journal(
    db: AsyncSession,
    *,
    workspace_id: str,
    invoice_id: int,
    invoice_number: str,
    reverse_date: date,
) -> int | None:
    """Khi cancel hoá đơn — tạo bút toán đảo (debit ↔ credit) tham chiếu invoice_id.

    Không xoá bút toán gốc — tuân thủ nguyên tắc "không sửa sổ cái".
    Trả về entry_id mới hoặc None nếu không tìm thấy entry gốc.
    """
    src = (await db.execute(text("""
        SELECT id, total_debit FROM books_journal_entries
        WHERE workspace_id = :ws AND source_type = 'invoice' AND source_id = :sid
        ORDER BY id ASC LIMIT 1
    """), {"ws": workspace_id, "sid": invoice_id})).first()
    if not src:
        return None

    src_id = src[0]
    src_lines = (await db.execute(text("""
        SELECT account_code, debit, credit, description
        FROM books_journal_lines WHERE entry_id = :eid
    """), {"eid": src_id})).all()

    # Đảo nợ ↔ có
    reversed_lines = [
        {
            "account_code": r[0],
            "debit": r[2],     # credit cũ → debit mới
            "credit": r[1],    # debit cũ → credit mới
            "description": (r[3] or "") + " (đảo bút toán)",
        }
        for r in src_lines
    ]
    return await _create_journal_entry(
        db,
        workspace_id=workspace_id,
        entry_date=reverse_date,
        description=f"Đảo bút toán hoá đơn {invoice_number} (huỷ)",
        source_type="invoice_cancel",
        source_id=invoice_id,
        lines=reversed_lines,
        posted=True,
    )


async def post_expense_journal(
    db: AsyncSession,
    *,
    workspace_id: str,
    expense_id: int,
    expense_number: str,
    expense_date: date,
    category: str | None,
    amount: Decimal,
    vat_amount: Decimal,
    payment_method: str | None,
    description: str | None,
) -> int:
    """Hạch toán chi phí. Đối ứng theo payment_method:
      cash          → Có 111
      bank_transfer → Có 112
      card          → Có 112
      khác          → Có 331

    Nợ tài khoản chi phí theo category (mặc định 642 — admin).
    Nợ 133 nếu có VAT.
    """
    expense_account = EXPENSE_CATEGORY_TO_ACCOUNT.get(
        (category or "other").lower(), "642"
    )
    payment_account = {
        "cash": "111",
        "bank_transfer": "112",
        "card": "112",
    }.get((payment_method or "").lower(), "331")

    subtotal = _q(amount)
    vat = _q(vat_amount or 0)
    total = subtotal + vat

    lines = [
        {"account_code": expense_account, "debit": subtotal, "credit": 0,
         "description": description or f"Chi phí {category or 'khác'}"},
    ]
    if vat > 0:
        lines.append({
            "account_code": "133", "debit": vat, "credit": 0,
            "description": "Thuế GTGT đầu vào được khấu trừ",
        })
    lines.append({
        "account_code": payment_account, "debit": 0, "credit": total,
        "description": f"Thanh toán bằng {payment_method or 'công nợ'}",
    })

    return await _create_journal_entry(
        db,
        workspace_id=workspace_id,
        entry_date=expense_date,
        description=f"Phiếu chi {expense_number}",
        source_type="expense",
        source_id=expense_id,
        lines=lines,
        posted=True,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Reports
# ──────────────────────────────────────────────────────────────────────────────
async def calculate_balance_sheet(
    db: AsyncSession,
    *,
    workspace_id: str,
    as_of_date: date,
) -> dict[str, Any]:
    """Bảng cân đối kế toán = sum balances theo account_type tại as_of_date.

    Quy ước:
      Asset balance   = Σ debit − Σ credit
      Liability/Equity= Σ credit − Σ debit
      Revenue/Expense = báo cáo PnL — không vào balance sheet trực tiếp,
                        chỉ dồn vào TK 421 (Lợi nhuận chưa phân phối) qua TK 911.
    """
    rows = (await db.execute(text("""
        SELECT
            coa.code,
            coa.name,
            coa.account_type,
            COALESCE(SUM(jl.debit), 0)  AS sum_debit,
            COALESCE(SUM(jl.credit), 0) AS sum_credit
        FROM books_chart_of_accounts coa
        LEFT JOIN books_journal_lines jl ON jl.account_code = coa.code
        LEFT JOIN books_journal_entries je
            ON je.id = jl.entry_id
            AND je.workspace_id = :ws
            AND je.entry_date <= :as_of
            AND je.posted = TRUE
        WHERE coa.workspace_id = :ws
        GROUP BY coa.code, coa.name, coa.account_type
        ORDER BY coa.code
    """), {"ws": workspace_id, "as_of": as_of_date})).mappings().all()

    assets: list[dict] = []
    liabilities: list[dict] = []
    equity: list[dict] = []

    total_assets = Decimal("0")
    total_liabilities = Decimal("0")
    total_equity = Decimal("0")

    pnl_revenue = Decimal("0")
    pnl_expense = Decimal("0")

    for r in rows:
        d = _q(r["sum_debit"])
        c = _q(r["sum_credit"])
        atype = r["account_type"]

        if atype == "asset":
            bal = d - c
            if bal != 0:
                assets.append({"code": r["code"], "name": r["name"], "balance": float(bal)})
            total_assets += bal
        elif atype == "liability":
            bal = c - d
            if bal != 0:
                liabilities.append({"code": r["code"], "name": r["name"], "balance": float(bal)})
            total_liabilities += bal
        elif atype == "equity":
            bal = c - d
            if bal != 0:
                equity.append({"code": r["code"], "name": r["name"], "balance": float(bal)})
            total_equity += bal
        elif atype == "revenue":
            pnl_revenue += (c - d)
        elif atype == "expense":
            pnl_expense += (d - c)

    # Lợi nhuận chưa phân phối (chưa kết chuyển 911) — đẩy vào equity
    retained_pnl = pnl_revenue - pnl_expense
    if retained_pnl != 0:
        equity.append({
            "code": "421*",
            "name": "Lợi nhuận chưa kết chuyển kỳ này",
            "balance": float(_q(retained_pnl)),
        })
        total_equity += retained_pnl

    return {
        "as_of_date": as_of_date.isoformat(),
        "assets": {
            "items": assets,
            "total": float(_q(total_assets)),
        },
        "liabilities": {
            "items": liabilities,
            "total": float(_q(total_liabilities)),
        },
        "equity": {
            "items": equity,
            "total": float(_q(total_equity)),
        },
        "balanced": _q(total_assets) == _q(total_liabilities + total_equity),
        "difference": float(_q(total_assets - (total_liabilities + total_equity))),
    }


async def calculate_pnl(
    db: AsyncSession,
    *,
    workspace_id: str,
    from_date: date,
    to_date: date,
) -> dict[str, Any]:
    """Báo cáo kết quả kinh doanh = doanh thu − chi phí trong period."""
    rows = (await db.execute(text("""
        SELECT
            coa.code,
            coa.name,
            coa.account_type,
            COALESCE(SUM(jl.debit), 0)  AS sum_debit,
            COALESCE(SUM(jl.credit), 0) AS sum_credit
        FROM books_chart_of_accounts coa
        LEFT JOIN books_journal_lines jl ON jl.account_code = coa.code
        LEFT JOIN books_journal_entries je
            ON je.id = jl.entry_id
            AND je.workspace_id = :ws
            AND je.entry_date BETWEEN :fd AND :td
            AND je.posted = TRUE
        WHERE coa.workspace_id = :ws AND coa.account_type IN ('revenue','expense')
        GROUP BY coa.code, coa.name, coa.account_type
        ORDER BY coa.code
    """), {"ws": workspace_id, "fd": from_date, "td": to_date})).mappings().all()

    revenues: list[dict] = []
    expenses: list[dict] = []
    total_revenue = Decimal("0")
    total_expense = Decimal("0")

    for r in rows:
        d = _q(r["sum_debit"])
        c = _q(r["sum_credit"])
        if r["account_type"] == "revenue":
            bal = c - d
            if bal != 0:
                revenues.append({"code": r["code"], "name": r["name"], "amount": float(bal)})
            total_revenue += bal
        else:
            bal = d - c
            if bal != 0:
                expenses.append({"code": r["code"], "name": r["name"], "amount": float(bal)})
            total_expense += bal

    gross_profit = total_revenue - total_expense
    # Thuế TNDN ước tính 20% (giả định doanh nghiệp SME — VAS chuẩn)
    tax_rate = Decimal("0.20")
    cit = max(Decimal("0"), gross_profit) * tax_rate
    net_profit = gross_profit - cit

    return {
        "period": {"from": from_date.isoformat(), "to": to_date.isoformat()},
        "revenues": {
            "items": revenues,
            "total": float(_q(total_revenue)),
        },
        "expenses": {
            "items": expenses,
            "total": float(_q(total_expense)),
        },
        "profit_before_tax": float(_q(gross_profit)),
        "estimated_corporate_tax_20pct": float(_q(cit)),
        "net_profit_estimate": float(_q(net_profit)),
    }


async def calculate_cash_flow(
    db: AsyncSession,
    *,
    workspace_id: str,
    from_date: date,
    to_date: date,
) -> dict[str, Any]:
    """Báo cáo lưu chuyển tiền tệ — phương pháp trực tiếp đơn giản.

    Inflow  = sum debit của TK 111 + 112 trong kỳ.
    Outflow = sum credit của TK 111 + 112 trong kỳ.
    Net     = inflow - outflow.
    """
    row = (await db.execute(text("""
        SELECT
            COALESCE(SUM(jl.debit), 0)  AS inflow,
            COALESCE(SUM(jl.credit), 0) AS outflow
        FROM books_journal_lines jl
        JOIN books_journal_entries je ON je.id = jl.entry_id
        WHERE je.workspace_id = :ws
          AND je.entry_date BETWEEN :fd AND :td
          AND je.posted = TRUE
          AND jl.account_code IN ('111','112','113')
    """), {"ws": workspace_id, "fd": from_date, "td": to_date})).first()

    inflow = _q(row[0] if row else 0)
    outflow = _q(row[1] if row else 0)

    # Chia nhỏ flows theo loại nguồn
    breakdown_rows = (await db.execute(text("""
        SELECT
            je.source_type,
            COALESCE(SUM(jl.debit), 0)  AS inflow,
            COALESCE(SUM(jl.credit), 0) AS outflow
        FROM books_journal_lines jl
        JOIN books_journal_entries je ON je.id = jl.entry_id
        WHERE je.workspace_id = :ws
          AND je.entry_date BETWEEN :fd AND :td
          AND je.posted = TRUE
          AND jl.account_code IN ('111','112','113')
        GROUP BY je.source_type
        ORDER BY je.source_type
    """), {"ws": workspace_id, "fd": from_date, "td": to_date})).mappings().all()

    breakdown = [
        {
            "source": r["source_type"] or "manual",
            "inflow": float(_q(r["inflow"])),
            "outflow": float(_q(r["outflow"])),
            "net": float(_q(r["inflow"] - r["outflow"])),
        }
        for r in breakdown_rows
    ]

    return {
        "period": {"from": from_date.isoformat(), "to": to_date.isoformat()},
        "cash_inflow": float(inflow),
        "cash_outflow": float(outflow),
        "net_cash_flow": float(_q(inflow - outflow)),
        "breakdown_by_source": breakdown,
    }


async def calculate_vat_report(
    db: AsyncSession,
    *,
    workspace_id: str,
    quarter: int,
    year: int,
) -> dict[str, Any]:
    """Tờ khai thuế GTGT quý — output (TK 333) − input (TK 133).

    Quarter mapping:
      1 → Q1: 01/01 – 31/03
      2 → Q2: 01/04 – 30/06
      3 → Q3: 01/07 – 30/09
      4 → Q4: 01/10 – 31/12
    """
    if quarter < 1 or quarter > 4:
        raise ValueError("Quý phải từ 1 đến 4")

    quarter_starts = {
        1: (1, 1), 2: (4, 1), 3: (7, 1), 4: (10, 1),
    }
    quarter_ends = {
        1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31),
    }
    sm, sd = quarter_starts[quarter]
    em, ed = quarter_ends[quarter]
    from_date = date(year, sm, sd)
    to_date = date(year, em, ed)

    # VAT đầu ra (output) — Có TK 333
    out_row = (await db.execute(text("""
        SELECT COALESCE(SUM(jl.credit - jl.debit), 0) AS net_credit
        FROM books_journal_lines jl
        JOIN books_journal_entries je ON je.id = jl.entry_id
        WHERE je.workspace_id = :ws
          AND je.entry_date BETWEEN :fd AND :td
          AND je.posted = TRUE
          AND jl.account_code = '333'
    """), {"ws": workspace_id, "fd": from_date, "td": to_date})).first()
    output_vat = _q(out_row[0] if out_row else 0)

    # VAT đầu vào (input) — Nợ TK 133
    in_row = (await db.execute(text("""
        SELECT COALESCE(SUM(jl.debit - jl.credit), 0) AS net_debit
        FROM books_journal_lines jl
        JOIN books_journal_entries je ON je.id = jl.entry_id
        WHERE je.workspace_id = :ws
          AND je.entry_date BETWEEN :fd AND :td
          AND je.posted = TRUE
          AND jl.account_code = '133'
    """), {"ws": workspace_id, "fd": from_date, "td": to_date})).first()
    input_vat = _q(in_row[0] if in_row else 0)

    payable = output_vat - input_vat

    # Tổng doanh thu chịu thuế trong quý
    rev_row = (await db.execute(text("""
        SELECT COALESCE(SUM(jl.credit - jl.debit), 0) AS revenue
        FROM books_journal_lines jl
        JOIN books_journal_entries je ON je.id = jl.entry_id
        WHERE je.workspace_id = :ws
          AND je.entry_date BETWEEN :fd AND :td
          AND je.posted = TRUE
          AND jl.account_code = '511'
    """), {"ws": workspace_id, "fd": from_date, "td": to_date})).first()
    taxable_revenue = _q(rev_row[0] if rev_row else 0)

    return {
        "period": {
            "quarter": quarter,
            "year": year,
            "from": from_date.isoformat(),
            "to": to_date.isoformat(),
        },
        "taxable_revenue": float(taxable_revenue),
        "output_vat": float(output_vat),
        "input_vat": float(input_vat),
        "vat_payable": float(_q(max(Decimal("0"), payable))),
        "vat_carryforward": float(_q(max(Decimal("0"), -payable))),
        "note": "Tờ khai thuế GTGT quý theo Mẫu 01/GTGT — TT 80/2021/TT-BTC",
    }
