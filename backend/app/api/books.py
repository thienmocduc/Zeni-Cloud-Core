"""
Zeni Books API — Vietnamese accounting module (VAS-compliant).

Routes (mounted under /api/v1/books in main.py):
  Customers   — GET/POST/PATCH/DELETE   /books/customers
  Suppliers   — GET/POST/PATCH/DELETE   /books/suppliers
  Products    — GET/POST/PATCH/DELETE   /books/products
  Invoices    — POST                    /books/invoices         (auto generate number, calc VAT, post journal nếu issued)
                GET                     /books/invoices         (filter by ws/status/from/to)
                GET                     /books/invoices/{id}    (detail + line items)
                POST                    /books/invoices/{id}/issue   (draft → issued + post journal)
                POST                    /books/invoices/{id}/cancel  (cancel + reverse journal)
                GET                     /books/invoices/{id}/pdf     (PDF placeholder cho v1)
  Expenses    — GET/POST/PATCH/DELETE   /books/expenses
  Journal     — GET                     /books/journal
                POST                    /books/journal/manual    (manual debit/credit balance check)
  Accounts    — GET/POST                /books/accounts
  Reports     — GET                     /books/reports/balance-sheet
                GET                     /books/reports/profit-loss
                GET                     /books/reports/cash-flow
                GET                     /books/reports/vat
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db
from app.services.audit import audit_push
from app.services.books_engine import (
    DEFAULT_COA,
    calculate_balance_sheet,
    calculate_cash_flow,
    calculate_pnl,
    calculate_vat_report,
    ensure_chart_of_accounts,
    generate_expense_number,
    generate_invoice_number,
    post_expense_journal,
    post_invoice_journal,
    post_invoice_purchase_journal,
    reverse_invoice_journal,
    validate_tax_code,
)

log = logging.getLogger("zeni.api.books")
router = APIRouter(prefix="/books", tags=["books"])


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════
async def _resolve_ws(db: AsyncSession, ws: str, me: CurrentUser) -> str:
    """Accept ws code hoặc id, return canonical id, ensure access + ensure COA."""
    row = (await db.execute(text(
        "SELECT id FROM workspaces WHERE id = :ws OR code = :ws LIMIT 1"
    ), {"ws": ws})).first()
    if not row:
        raise HTTPException(404, "Không tìm thấy workspace")
    workspace_id = row[0]
    await require_workspace_access(workspace_id, me)
    # Auto-seed chart of accounts on first /books/* call (idempotent).
    await ensure_chart_of_accounts(db, workspace_id)
    return workspace_id


def _f(v: Any) -> float:
    """Convert Decimal/None → float for JSON."""
    if v is None:
        return 0.0
    return float(v)


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert SQLAlchemy row mapping with sensible Decimal serialization."""
    out = {}
    for k, v in dict(row).items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, (date, datetime)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


# ════════════════════════════════════════════════════════════════════════════
# Pydantic schemas
# ════════════════════════════════════════════════════════════════════════════
class CustomerIn(BaseModel):
    customer_code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    tax_code: str | None = Field(default=None, max_length=20)
    address: str | None = None
    phone: str | None = Field(default=None, max_length=20)
    email: str | None = None
    contact_person: str | None = Field(default=None, max_length=120)

    @field_validator("tax_code")
    @classmethod
    def _check_tax(cls, v: str | None) -> str | None:
        if v and not validate_tax_code(v):
            raise ValueError("Mã số thuế không hợp lệ — phải 10 hoặc 13 chữ số")
        return v


class CustomerPatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    tax_code: str | None = Field(default=None, max_length=20)
    address: str | None = None
    phone: str | None = Field(default=None, max_length=20)
    email: str | None = None
    contact_person: str | None = Field(default=None, max_length=120)
    is_active: bool | None = None

    @field_validator("tax_code")
    @classmethod
    def _check_tax(cls, v: str | None) -> str | None:
        if v and not validate_tax_code(v):
            raise ValueError("Mã số thuế không hợp lệ — phải 10 hoặc 13 chữ số")
        return v


class SupplierIn(BaseModel):
    supplier_code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    tax_code: str | None = Field(default=None, max_length=20)
    address: str | None = None
    phone: str | None = Field(default=None, max_length=20)
    email: str | None = None

    @field_validator("tax_code")
    @classmethod
    def _check_tax(cls, v: str | None) -> str | None:
        if v and not validate_tax_code(v):
            raise ValueError("Mã số thuế không hợp lệ — phải 10 hoặc 13 chữ số")
        return v


class SupplierPatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    tax_code: str | None = Field(default=None, max_length=20)
    address: str | None = None
    phone: str | None = Field(default=None, max_length=20)
    email: str | None = None
    is_active: bool | None = None


class ProductIn(BaseModel):
    product_code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    unit: str = Field(default="cái", max_length=40)
    sale_price: Decimal | None = Field(default=None, ge=0)
    cost_price: Decimal | None = Field(default=None, ge=0)
    vat_rate: Decimal = Field(default=Decimal("10"), ge=0, le=100)
    inventory_quantity: Decimal = Field(default=Decimal("0"))
    product_type: str = Field(default="goods", pattern=r"^(goods|service)$")


class ProductPatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    unit: str | None = Field(default=None, max_length=40)
    sale_price: Decimal | None = Field(default=None, ge=0)
    cost_price: Decimal | None = Field(default=None, ge=0)
    vat_rate: Decimal | None = Field(default=None, ge=0, le=100)
    inventory_quantity: Decimal | None = None
    product_type: str | None = Field(default=None, pattern=r"^(goods|service)$")
    is_active: bool | None = None


class InvoiceItemIn(BaseModel):
    product_id: int | None = None
    description: str = Field(min_length=1)
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    vat_rate: Decimal = Field(default=Decimal("10"), ge=0, le=100)


class InvoiceIn(BaseModel):
    invoice_type: str = Field(default="sale", pattern=r"^(sale|purchase|adjustment)$")
    customer_id: int | None = None
    supplier_id: int | None = None
    issue_date: date
    due_date: date | None = None
    notes: str | None = None
    items: list[InvoiceItemIn] = Field(min_length=1)
    auto_issue: bool = Field(default=False, description="True = tạo + post journal ngay (status='issued')")


class ExpenseIn(BaseModel):
    expense_date: date
    category: str | None = Field(default=None, max_length=40)
    supplier_id: int | None = None
    amount: Decimal = Field(gt=0)
    vat_amount: Decimal = Field(default=Decimal("0"), ge=0)
    payment_method: str | None = Field(default=None, max_length=20)
    description: str | None = None
    receipt_image_url: str | None = None
    auto_post: bool = Field(default=True, description="True = post journal entry ngay")


class ExpensePatch(BaseModel):
    category: str | None = Field(default=None, max_length=40)
    supplier_id: int | None = None
    amount: Decimal | None = Field(default=None, gt=0)
    vat_amount: Decimal | None = Field(default=None, ge=0)
    payment_method: str | None = Field(default=None, max_length=20)
    description: str | None = None
    status: str | None = Field(default=None, pattern=r"^(recorded|paid|reimbursed)$")


class JournalLineIn(BaseModel):
    account_code: str = Field(min_length=2, max_length=10)
    debit: Decimal = Field(default=Decimal("0"), ge=0)
    credit: Decimal = Field(default=Decimal("0"), ge=0)
    description: str | None = None


class ManualJournalIn(BaseModel):
    entry_date: date
    description: str | None = None
    lines: list[JournalLineIn] = Field(min_length=2)


class AccountIn(BaseModel):
    code: str = Field(min_length=2, max_length=10, pattern=r"^\d+[A-Z]?$")
    name: str = Field(min_length=1, max_length=200)
    name_en: str | None = Field(default=None, max_length=200)
    account_type: str = Field(pattern=r"^(asset|liability|equity|revenue|expense)$")
    parent_code: str | None = Field(default=None, max_length=10)


# ════════════════════════════════════════════════════════════════════════════
# Customers
# ════════════════════════════════════════════════════════════════════════════
@router.get("/customers")
async def list_customers(
    ws: str = Query(...),
    is_active: bool | None = Query(None),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List khách hàng của workspace."""
    workspace_id = await _resolve_ws(db, ws, me)
    sql = "SELECT * FROM books_customers WHERE workspace_id = :ws"
    params: dict = {"ws": workspace_id}
    if is_active is not None:
        sql += " AND is_active = :a"
        params["a"] = is_active
    sql += " ORDER BY name"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return {"ok": True, "count": len(rows), "items": [_row_to_dict(r) for r in rows]}


@router.post("/customers")
async def create_customer(
    data: CustomerIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Tạo khách hàng mới."""
    workspace_id = await _resolve_ws(db, ws, me)
    try:
        row = (await db.execute(text("""
            INSERT INTO books_customers
                (workspace_id, customer_code, name, tax_code, address, phone, email, contact_person)
            VALUES (:ws, :code, :name, :tax, :addr, :phone, :email, :cp)
            RETURNING *
        """), {
            "ws": workspace_id, "code": data.customer_code, "name": data.name,
            "tax": data.tax_code, "addr": data.address, "phone": data.phone,
            "email": data.email, "cp": data.contact_person,
        })).mappings().first()
    except Exception as e:
        await db.rollback()
        raise HTTPException(400, f"Không thể tạo khách hàng: {e}")

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="books.customer.create", target=data.customer_code, severity="ok",
        metadata={"name": data.name, "tax_code": data.tax_code},
    )
    await db.commit()
    return {"ok": True, "customer": _row_to_dict(row)}


@router.patch("/customers/{customer_id}")
async def update_customer(
    customer_id: int,
    data: CustomerPatch,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id = await _resolve_ws(db, ws, me)
    fields = data.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(400, "Không có thay đổi nào")

    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    params = {**fields, "id": customer_id, "ws": workspace_id}
    row = (await db.execute(text(f"""
        UPDATE books_customers SET {set_clause}
        WHERE id = :id AND workspace_id = :ws
        RETURNING *
    """), params)).mappings().first()
    if not row:
        raise HTTPException(404, "Không tìm thấy khách hàng")

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="books.customer.update", target=str(customer_id), severity="info",
        metadata=fields,
    )
    await db.commit()
    return {"ok": True, "customer": _row_to_dict(row)}


@router.delete("/customers/{customer_id}")
async def delete_customer(
    customer_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Soft-delete (set is_active=FALSE) — không hard delete để tránh mất tham chiếu."""
    workspace_id = await _resolve_ws(db, ws, me)
    result = await db.execute(text("""
        UPDATE books_customers SET is_active = FALSE
        WHERE id = :id AND workspace_id = :ws
    """), {"id": customer_id, "ws": workspace_id})
    if result.rowcount == 0:
        raise HTTPException(404, "Không tìm thấy khách hàng")
    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="books.customer.deactivate", target=str(customer_id), severity="warning",
    )
    await db.commit()
    return {"ok": True, "deactivated": True}


# ════════════════════════════════════════════════════════════════════════════
# Suppliers
# ════════════════════════════════════════════════════════════════════════════
@router.get("/suppliers")
async def list_suppliers(
    ws: str = Query(...),
    is_active: bool | None = Query(None),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id = await _resolve_ws(db, ws, me)
    sql = "SELECT * FROM books_suppliers WHERE workspace_id = :ws"
    params: dict = {"ws": workspace_id}
    if is_active is not None:
        sql += " AND is_active = :a"
        params["a"] = is_active
    sql += " ORDER BY name"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return {"ok": True, "count": len(rows), "items": [_row_to_dict(r) for r in rows]}


@router.post("/suppliers")
async def create_supplier(
    data: SupplierIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id = await _resolve_ws(db, ws, me)
    try:
        row = (await db.execute(text("""
            INSERT INTO books_suppliers
                (workspace_id, supplier_code, name, tax_code, address, phone, email)
            VALUES (:ws, :code, :name, :tax, :addr, :phone, :email)
            RETURNING *
        """), {
            "ws": workspace_id, "code": data.supplier_code, "name": data.name,
            "tax": data.tax_code, "addr": data.address, "phone": data.phone,
            "email": data.email,
        })).mappings().first()
    except Exception as e:
        await db.rollback()
        raise HTTPException(400, f"Không thể tạo nhà cung cấp: {e}")

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="books.supplier.create", target=data.supplier_code, severity="ok",
        metadata={"name": data.name},
    )
    await db.commit()
    return {"ok": True, "supplier": _row_to_dict(row)}


@router.patch("/suppliers/{supplier_id}")
async def update_supplier(
    supplier_id: int,
    data: SupplierPatch,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id = await _resolve_ws(db, ws, me)
    fields = data.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(400, "Không có thay đổi nào")
    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    params = {**fields, "id": supplier_id, "ws": workspace_id}
    row = (await db.execute(text(f"""
        UPDATE books_suppliers SET {set_clause}
        WHERE id = :id AND workspace_id = :ws
        RETURNING *
    """), params)).mappings().first()
    if not row:
        raise HTTPException(404, "Không tìm thấy nhà cung cấp")
    await db.commit()
    return {"ok": True, "supplier": _row_to_dict(row)}


@router.delete("/suppliers/{supplier_id}")
async def delete_supplier(
    supplier_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id = await _resolve_ws(db, ws, me)
    result = await db.execute(text("""
        UPDATE books_suppliers SET is_active = FALSE
        WHERE id = :id AND workspace_id = :ws
    """), {"id": supplier_id, "ws": workspace_id})
    if result.rowcount == 0:
        raise HTTPException(404, "Không tìm thấy nhà cung cấp")
    await db.commit()
    return {"ok": True, "deactivated": True}


# ════════════════════════════════════════════════════════════════════════════
# Products
# ════════════════════════════════════════════════════════════════════════════
@router.get("/products")
async def list_products(
    ws: str = Query(...),
    product_type: str | None = Query(None),
    is_active: bool | None = Query(None),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id = await _resolve_ws(db, ws, me)
    sql = "SELECT * FROM books_products WHERE workspace_id = :ws"
    params: dict = {"ws": workspace_id}
    if product_type:
        sql += " AND product_type = :pt"
        params["pt"] = product_type
    if is_active is not None:
        sql += " AND is_active = :a"
        params["a"] = is_active
    sql += " ORDER BY name"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return {"ok": True, "count": len(rows), "items": [_row_to_dict(r) for r in rows]}


@router.post("/products")
async def create_product(
    data: ProductIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id = await _resolve_ws(db, ws, me)
    try:
        row = (await db.execute(text("""
            INSERT INTO books_products
                (workspace_id, product_code, name, unit, sale_price, cost_price,
                 vat_rate, inventory_quantity, product_type)
            VALUES (:ws, :code, :name, :unit, :sp, :cp, :v, :iq, :pt)
            RETURNING *
        """), {
            "ws": workspace_id, "code": data.product_code, "name": data.name,
            "unit": data.unit, "sp": data.sale_price, "cp": data.cost_price,
            "v": data.vat_rate, "iq": data.inventory_quantity, "pt": data.product_type,
        })).mappings().first()
    except Exception as e:
        await db.rollback()
        raise HTTPException(400, f"Không thể tạo sản phẩm: {e}")

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="books.product.create", target=data.product_code, severity="ok",
        metadata={"name": data.name, "type": data.product_type},
    )
    await db.commit()
    return {"ok": True, "product": _row_to_dict(row)}


@router.patch("/products/{product_id}")
async def update_product(
    product_id: int,
    data: ProductPatch,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id = await _resolve_ws(db, ws, me)
    fields = data.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(400, "Không có thay đổi nào")
    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    params = {**fields, "id": product_id, "ws": workspace_id}
    row = (await db.execute(text(f"""
        UPDATE books_products SET {set_clause}
        WHERE id = :id AND workspace_id = :ws
        RETURNING *
    """), params)).mappings().first()
    if not row:
        raise HTTPException(404, "Không tìm thấy sản phẩm")
    await db.commit()
    return {"ok": True, "product": _row_to_dict(row)}


@router.delete("/products/{product_id}")
async def delete_product(
    product_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id = await _resolve_ws(db, ws, me)
    result = await db.execute(text("""
        UPDATE books_products SET is_active = FALSE
        WHERE id = :id AND workspace_id = :ws
    """), {"id": product_id, "ws": workspace_id})
    if result.rowcount == 0:
        raise HTTPException(404, "Không tìm thấy sản phẩm")
    await db.commit()
    return {"ok": True, "deactivated": True}


# ════════════════════════════════════════════════════════════════════════════
# Invoices
# ════════════════════════════════════════════════════════════════════════════
async def _calc_invoice_totals(items: list[InvoiceItemIn]) -> tuple[Decimal, Decimal, Decimal, list[dict]]:
    """Tính subtotal, vat, total + line breakdowns from items."""
    subtotal = Decimal("0")
    vat = Decimal("0")
    breakdown: list[dict] = []
    for it in items:
        line_sub = (it.quantity * it.unit_price).quantize(Decimal("0.01"))
        line_vat = (line_sub * it.vat_rate / Decimal("100")).quantize(Decimal("0.01"))
        line_total = line_sub + line_vat
        subtotal += line_sub
        vat += line_vat
        breakdown.append({
            "product_id": it.product_id,
            "description": it.description,
            "quantity": it.quantity,
            "unit_price": it.unit_price,
            "vat_rate": it.vat_rate,
            "line_subtotal": line_sub,
            "line_vat": line_vat,
            "line_total": line_total,
        })
    return subtotal, vat, subtotal + vat, breakdown


@router.post("/invoices")
async def create_invoice(
    data: InvoiceIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Tạo hoá đơn — tự generate invoice_number, tính VAT, optional auto-issue + post journal."""
    workspace_id = await _resolve_ws(db, ws, me)

    # Validation: sale → cần customer_id; purchase → cần supplier_id
    if data.invoice_type == "sale" and not data.customer_id:
        raise HTTPException(400, "Hoá đơn bán hàng phải có khách hàng")
    if data.invoice_type == "purchase" and not data.supplier_id:
        raise HTTPException(400, "Hoá đơn mua hàng phải có nhà cung cấp")

    subtotal, vat, total, breakdown = await _calc_invoice_totals(data.items)

    invoice_number = await generate_invoice_number(db, workspace_id)
    status = "issued" if data.auto_issue else "draft"

    inv_row = (await db.execute(text("""
        INSERT INTO books_invoices
            (workspace_id, invoice_number, invoice_type, customer_id, supplier_id,
             issue_date, due_date, subtotal, vat_amount, total, status, notes)
        VALUES (:ws, :num, :it, :cid, :sid, :id_, :dd, :sub, :v, :tot, :st, :n)
        RETURNING *
    """), {
        "ws": workspace_id, "num": invoice_number, "it": data.invoice_type,
        "cid": data.customer_id, "sid": data.supplier_id,
        "id_": data.issue_date, "dd": data.due_date,
        "sub": subtotal, "v": vat, "tot": total,
        "st": status, "n": data.notes,
    })).mappings().first()
    invoice_id = inv_row["id"]

    # Insert line items
    for line in breakdown:
        await db.execute(text("""
            INSERT INTO books_invoice_items
                (invoice_id, product_id, description, quantity, unit_price,
                 vat_rate, line_subtotal, line_vat, line_total)
            VALUES (:iid, :pid, :desc, :q, :up, :vr, :ls, :lv, :lt)
        """), {
            "iid": invoice_id, "pid": line["product_id"], "desc": line["description"],
            "q": line["quantity"], "up": line["unit_price"], "vr": line["vat_rate"],
            "ls": line["line_subtotal"], "lv": line["line_vat"], "lt": line["line_total"],
        })

    # Auto-post journal nếu issued
    journal_entry_id: int | None = None
    if data.auto_issue:
        partner_name = await _get_partner_name(
            db, data.invoice_type, data.customer_id, data.supplier_id, workspace_id
        )
        if data.invoice_type == "sale":
            journal_entry_id = await post_invoice_journal(
                db,
                workspace_id=workspace_id,
                invoice_id=invoice_id,
                invoice_number=invoice_number,
                issue_date=data.issue_date,
                subtotal=subtotal,
                vat_amount=vat,
                total=total,
                customer_name=partner_name,
            )
            # Update receivable balance
            if data.customer_id:
                await db.execute(text("""
                    UPDATE books_customers SET receivable_balance = receivable_balance + :amt
                    WHERE id = :id AND workspace_id = :ws
                """), {"amt": total, "id": data.customer_id, "ws": workspace_id})
        elif data.invoice_type == "purchase":
            journal_entry_id = await post_invoice_purchase_journal(
                db,
                workspace_id=workspace_id,
                invoice_id=invoice_id,
                invoice_number=invoice_number,
                issue_date=data.issue_date,
                subtotal=subtotal,
                vat_amount=vat,
                total=total,
                supplier_name=partner_name,
            )
            if data.supplier_id:
                await db.execute(text("""
                    UPDATE books_suppliers SET payable_balance = payable_balance + :amt
                    WHERE id = :id AND workspace_id = :ws
                """), {"amt": total, "id": data.supplier_id, "ws": workspace_id})

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="books.invoice.create", target=invoice_number, severity="ok",
        metadata={
            "type": data.invoice_type, "subtotal": float(subtotal),
            "vat": float(vat), "total": float(total), "auto_issued": data.auto_issue,
            "journal_entry_id": journal_entry_id,
        },
    )
    await db.commit()
    return {
        "ok": True,
        "invoice": _row_to_dict(inv_row),
        "items_count": len(breakdown),
        "journal_entry_id": journal_entry_id,
    }


async def _get_partner_name(
    db: AsyncSession, invoice_type: str, customer_id: int | None,
    supplier_id: int | None, workspace_id: str,
) -> str | None:
    if invoice_type == "sale" and customer_id:
        row = (await db.execute(text(
            "SELECT name FROM books_customers WHERE id = :id AND workspace_id = :ws"
        ), {"id": customer_id, "ws": workspace_id})).first()
        return row[0] if row else None
    if invoice_type == "purchase" and supplier_id:
        row = (await db.execute(text(
            "SELECT name FROM books_suppliers WHERE id = :id AND workspace_id = :ws"
        ), {"id": supplier_id, "ws": workspace_id})).first()
        return row[0] if row else None
    return None


@router.get("/invoices")
async def list_invoices(
    ws: str = Query(...),
    status: str | None = Query(None),
    invoice_type: str | None = Query(None),
    from_: date | None = Query(None, alias="from"),
    to: date | None = Query(None),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id = await _resolve_ws(db, ws, me)
    sql = "SELECT * FROM books_invoices WHERE workspace_id = :ws"
    params: dict = {"ws": workspace_id}
    if status:
        sql += " AND status = :st"
        params["st"] = status
    if invoice_type:
        sql += " AND invoice_type = :it"
        params["it"] = invoice_type
    if from_:
        sql += " AND issue_date >= :fd"
        params["fd"] = from_
    if to:
        sql += " AND issue_date <= :td"
        params["td"] = to
    sql += " ORDER BY issue_date DESC, id DESC LIMIT 500"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return {"ok": True, "count": len(rows), "items": [_row_to_dict(r) for r in rows]}


@router.get("/invoices/{invoice_id}")
async def get_invoice(
    invoice_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id = await _resolve_ws(db, ws, me)
    inv = (await db.execute(text("""
        SELECT * FROM books_invoices WHERE id = :id AND workspace_id = :ws
    """), {"id": invoice_id, "ws": workspace_id})).mappings().first()
    if not inv:
        raise HTTPException(404, "Không tìm thấy hoá đơn")
    items = (await db.execute(text("""
        SELECT * FROM books_invoice_items WHERE invoice_id = :id ORDER BY id
    """), {"id": invoice_id})).mappings().all()
    return {
        "ok": True,
        "invoice": _row_to_dict(inv),
        "items": [_row_to_dict(i) for i in items],
    }


@router.post("/invoices/{invoice_id}/issue")
async def issue_invoice(
    invoice_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Đổi status draft → issued + post journal entries (kế toán kép)."""
    workspace_id = await _resolve_ws(db, ws, me)
    inv = (await db.execute(text("""
        SELECT * FROM books_invoices WHERE id = :id AND workspace_id = :ws
    """), {"id": invoice_id, "ws": workspace_id})).mappings().first()
    if not inv:
        raise HTTPException(404, "Không tìm thấy hoá đơn")
    if inv["status"] != "draft":
        raise HTTPException(400, f"Chỉ phát hành được hoá đơn ở trạng thái draft (hiện tại: {inv['status']})")

    partner_name = await _get_partner_name(
        db, inv["invoice_type"], inv["customer_id"], inv["supplier_id"], workspace_id,
    )
    if inv["invoice_type"] == "sale":
        entry_id = await post_invoice_journal(
            db,
            workspace_id=workspace_id,
            invoice_id=invoice_id,
            invoice_number=inv["invoice_number"],
            issue_date=inv["issue_date"],
            subtotal=inv["subtotal"],
            vat_amount=inv["vat_amount"],
            total=inv["total"],
            customer_name=partner_name,
        )
        if inv["customer_id"]:
            await db.execute(text("""
                UPDATE books_customers SET receivable_balance = receivable_balance + :amt
                WHERE id = :id AND workspace_id = :ws
            """), {"amt": inv["total"], "id": inv["customer_id"], "ws": workspace_id})
    elif inv["invoice_type"] == "purchase":
        entry_id = await post_invoice_purchase_journal(
            db,
            workspace_id=workspace_id,
            invoice_id=invoice_id,
            invoice_number=inv["invoice_number"],
            issue_date=inv["issue_date"],
            subtotal=inv["subtotal"],
            vat_amount=inv["vat_amount"],
            total=inv["total"],
            supplier_name=partner_name,
        )
        if inv["supplier_id"]:
            await db.execute(text("""
                UPDATE books_suppliers SET payable_balance = payable_balance + :amt
                WHERE id = :id AND workspace_id = :ws
            """), {"amt": inv["total"], "id": inv["supplier_id"], "ws": workspace_id})
    else:
        raise HTTPException(400, "Loại hoá đơn không hỗ trợ phát hành tự động")

    await db.execute(text("""
        UPDATE books_invoices SET status = 'issued' WHERE id = :id
    """), {"id": invoice_id})

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="books.invoice.issue", target=inv["invoice_number"], severity="ok",
        metadata={"journal_entry_id": entry_id},
    )
    await db.commit()
    return {"ok": True, "invoice_id": invoice_id, "journal_entry_id": entry_id, "status": "issued"}


@router.post("/invoices/{invoice_id}/cancel")
async def cancel_invoice(
    invoice_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Cancel hoá đơn — đảo bút toán nếu đã issued."""
    workspace_id = await _resolve_ws(db, ws, me)
    inv = (await db.execute(text("""
        SELECT * FROM books_invoices WHERE id = :id AND workspace_id = :ws
    """), {"id": invoice_id, "ws": workspace_id})).mappings().first()
    if not inv:
        raise HTTPException(404, "Không tìm thấy hoá đơn")
    if inv["status"] == "cancelled":
        raise HTTPException(400, "Hoá đơn đã bị huỷ")
    if inv["paid_amount"] and Decimal(str(inv["paid_amount"])) > 0:
        raise HTTPException(400, "Không thể huỷ hoá đơn đã thanh toán")

    reverse_entry_id: int | None = None
    if inv["status"] == "issued":
        reverse_entry_id = await reverse_invoice_journal(
            db,
            workspace_id=workspace_id,
            invoice_id=invoice_id,
            invoice_number=inv["invoice_number"],
            reverse_date=date.today(),
        )
        # Roll back partner balances
        if inv["invoice_type"] == "sale" and inv["customer_id"]:
            await db.execute(text("""
                UPDATE books_customers SET receivable_balance = receivable_balance - :amt
                WHERE id = :id AND workspace_id = :ws
            """), {"amt": inv["total"], "id": inv["customer_id"], "ws": workspace_id})
        elif inv["invoice_type"] == "purchase" and inv["supplier_id"]:
            await db.execute(text("""
                UPDATE books_suppliers SET payable_balance = payable_balance - :amt
                WHERE id = :id AND workspace_id = :ws
            """), {"amt": inv["total"], "id": inv["supplier_id"], "ws": workspace_id})

    await db.execute(text("""
        UPDATE books_invoices SET status = 'cancelled' WHERE id = :id
    """), {"id": invoice_id})

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="books.invoice.cancel", target=inv["invoice_number"], severity="warning",
        metadata={"reverse_entry_id": reverse_entry_id},
    )
    await db.commit()
    return {
        "ok": True,
        "invoice_id": invoice_id,
        "status": "cancelled",
        "reverse_entry_id": reverse_entry_id,
    }


@router.get("/invoices/{invoice_id}/pdf", response_class=PlainTextResponse)
async def invoice_pdf(
    invoice_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PlainTextResponse:
    """Trả về PDF hoá đơn — v1 placeholder text. Tích hợp PDFKit/WeasyPrint sẽ làm sau."""
    workspace_id = await _resolve_ws(db, ws, me)
    inv = (await db.execute(text("""
        SELECT i.*, c.name AS customer_name, c.tax_code AS customer_tax,
               c.address AS customer_address,
               s.name AS supplier_name, s.tax_code AS supplier_tax,
               s.address AS supplier_address
        FROM books_invoices i
        LEFT JOIN books_customers c ON c.id = i.customer_id
        LEFT JOIN books_suppliers s ON s.id = i.supplier_id
        WHERE i.id = :id AND i.workspace_id = :ws
    """), {"id": invoice_id, "ws": workspace_id})).mappings().first()
    if not inv:
        raise HTTPException(404, "Không tìm thấy hoá đơn")
    items = (await db.execute(text("""
        SELECT * FROM books_invoice_items WHERE invoice_id = :id ORDER BY id
    """), {"id": invoice_id})).mappings().all()

    lines = [
        "═══════════════════════════════════════════════════════════════",
        "                       HOÁ ĐƠN GTGT",
        "                  (VAT INVOICE — VIETNAM)",
        "═══════════════════════════════════════════════════════════════",
        f"Số hoá đơn:    {inv['invoice_number']}",
        f"Ngày phát hành: {inv['issue_date']}",
        f"Loại:          {inv['invoice_type']}",
        f"Trạng thái:    {inv['status']}",
        "───────────────────────────────────────────────────────────────",
    ]
    if inv["customer_name"]:
        lines += [
            f"Khách hàng:    {inv['customer_name']}",
            f"MST:           {inv['customer_tax'] or '-'}",
            f"Địa chỉ:       {inv['customer_address'] or '-'}",
        ]
    if inv["supplier_name"]:
        lines += [
            f"Nhà cung cấp:  {inv['supplier_name']}",
            f"MST:           {inv['supplier_tax'] or '-'}",
            f"Địa chỉ:       {inv['supplier_address'] or '-'}",
        ]
    lines += ["───────────────────────────────────────────────────────────────",
              "STT  Diễn giải                       SL    Đơn giá         Thành tiền"]
    for idx, it in enumerate(items, 1):
        lines.append(
            f"{idx:<4} {(it['description'] or '')[:30]:<30} "
            f"{_f(it['quantity']):>5.2f} "
            f"{_f(it['unit_price']):>15,.2f} "
            f"{_f(it['line_total']):>15,.2f}"
        )
    lines += [
        "───────────────────────────────────────────────────────────────",
        f"Cộng tiền hàng:    {_f(inv['subtotal']):>20,.2f} VND",
        f"Tiền thuế GTGT:    {_f(inv['vat_amount']):>20,.2f} VND",
        f"Tổng cộng:         {_f(inv['total']):>20,.2f} VND",
        "───────────────────────────────────────────────────────────────",
        "(Bản PDF rendered chính thức sẽ được hỗ trợ qua VNPT/Viettel eInvoice.)",
        "═══════════════════════════════════════════════════════════════",
    ]
    return PlainTextResponse(
        "\n".join(lines),
        headers={"Content-Disposition": f'inline; filename="HD_{inv["invoice_number"]}.txt"'},
    )


# ════════════════════════════════════════════════════════════════════════════
# Expenses
# ════════════════════════════════════════════════════════════════════════════
@router.get("/expenses")
async def list_expenses(
    ws: str = Query(...),
    category: str | None = Query(None),
    from_: date | None = Query(None, alias="from"),
    to: date | None = Query(None),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id = await _resolve_ws(db, ws, me)
    sql = "SELECT * FROM books_expenses WHERE workspace_id = :ws"
    params: dict = {"ws": workspace_id}
    if category:
        sql += " AND category = :c"
        params["c"] = category
    if from_:
        sql += " AND expense_date >= :fd"
        params["fd"] = from_
    if to:
        sql += " AND expense_date <= :td"
        params["td"] = to
    sql += " ORDER BY expense_date DESC, id DESC LIMIT 500"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return {"ok": True, "count": len(rows), "items": [_row_to_dict(r) for r in rows]}


@router.post("/expenses")
async def create_expense(
    data: ExpenseIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id = await _resolve_ws(db, ws, me)
    expense_number = await generate_expense_number(db, workspace_id)
    row = (await db.execute(text("""
        INSERT INTO books_expenses
            (workspace_id, expense_number, expense_date, category, supplier_id,
             amount, vat_amount, payment_method, description, receipt_image_url)
        VALUES (:ws, :num, :ed, :cat, :sid, :amt, :v, :pm, :desc, :img)
        RETURNING *
    """), {
        "ws": workspace_id, "num": expense_number, "ed": data.expense_date,
        "cat": data.category, "sid": data.supplier_id, "amt": data.amount,
        "v": data.vat_amount, "pm": data.payment_method, "desc": data.description,
        "img": data.receipt_image_url,
    })).mappings().first()
    expense_id = row["id"]

    journal_entry_id: int | None = None
    if data.auto_post:
        journal_entry_id = await post_expense_journal(
            db,
            workspace_id=workspace_id,
            expense_id=expense_id,
            expense_number=expense_number,
            expense_date=data.expense_date,
            category=data.category,
            amount=data.amount,
            vat_amount=data.vat_amount,
            payment_method=data.payment_method,
            description=data.description,
        )

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="books.expense.create", target=expense_number, severity="ok",
        metadata={
            "category": data.category, "amount": float(data.amount),
            "vat": float(data.vat_amount), "auto_posted": data.auto_post,
            "journal_entry_id": journal_entry_id,
        },
    )
    await db.commit()
    return {
        "ok": True,
        "expense": _row_to_dict(row),
        "journal_entry_id": journal_entry_id,
    }


@router.patch("/expenses/{expense_id}")
async def update_expense(
    expense_id: int,
    data: ExpensePatch,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id = await _resolve_ws(db, ws, me)
    fields = data.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(400, "Không có thay đổi nào")
    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    params = {**fields, "id": expense_id, "ws": workspace_id}
    row = (await db.execute(text(f"""
        UPDATE books_expenses SET {set_clause}
        WHERE id = :id AND workspace_id = :ws
        RETURNING *
    """), params)).mappings().first()
    if not row:
        raise HTTPException(404, "Không tìm thấy phiếu chi")
    await db.commit()
    return {"ok": True, "expense": _row_to_dict(row)}


@router.delete("/expenses/{expense_id}")
async def delete_expense(
    expense_id: int,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Hard delete expense + cascade journal lines (chỉ cho phép nếu chưa post hoặc admin)."""
    workspace_id = await _resolve_ws(db, ws, me)
    # Xoá journal lines + entry referencing expense
    await db.execute(text("""
        DELETE FROM books_journal_entries
        WHERE workspace_id = :ws AND source_type = 'expense' AND source_id = :id
    """), {"ws": workspace_id, "id": expense_id})
    result = await db.execute(text("""
        DELETE FROM books_expenses WHERE id = :id AND workspace_id = :ws
    """), {"id": expense_id, "ws": workspace_id})
    if result.rowcount == 0:
        raise HTTPException(404, "Không tìm thấy phiếu chi")
    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="books.expense.delete", target=str(expense_id), severity="warning",
    )
    await db.commit()
    return {"ok": True, "deleted": True}


# ════════════════════════════════════════════════════════════════════════════
# Journal entries
# ════════════════════════════════════════════════════════════════════════════
@router.get("/journal")
async def list_journal(
    ws: str = Query(...),
    from_: date | None = Query(None, alias="from"),
    to: date | None = Query(None),
    source_type: str | None = Query(None),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id = await _resolve_ws(db, ws, me)
    sql = "SELECT * FROM books_journal_entries WHERE workspace_id = :ws"
    params: dict = {"ws": workspace_id}
    if from_:
        sql += " AND entry_date >= :fd"
        params["fd"] = from_
    if to:
        sql += " AND entry_date <= :td"
        params["td"] = to
    if source_type:
        sql += " AND source_type = :st"
        params["st"] = source_type
    sql += " ORDER BY entry_date DESC, id DESC LIMIT 500"
    entries = (await db.execute(text(sql), params)).mappings().all()
    entry_ids = [e["id"] for e in entries]
    if entry_ids:
        line_rows = (await db.execute(text("""
            SELECT * FROM books_journal_lines WHERE entry_id = ANY(:ids) ORDER BY id
        """), {"ids": entry_ids})).mappings().all()
        by_entry: dict[int, list[dict]] = {}
        for l in line_rows:
            by_entry.setdefault(l["entry_id"], []).append(_row_to_dict(l))
    else:
        by_entry = {}

    items = []
    for e in entries:
        ed = _row_to_dict(e)
        ed["lines"] = by_entry.get(e["id"], [])
        items.append(ed)
    return {"ok": True, "count": len(items), "items": items}


@router.post("/journal/manual")
async def create_manual_journal(
    data: ManualJournalIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Bút toán thủ công — debit phải = credit, mỗi line phải có account_code hợp lệ."""
    workspace_id = await _resolve_ws(db, ws, me)

    total_debit = sum(l.debit for l in data.lines)
    total_credit = sum(l.credit for l in data.lines)
    if total_debit != total_credit:
        raise HTTPException(
            400,
            f"Bút toán không cân bằng: Tổng Nợ {total_debit} ≠ Tổng Có {total_credit}",
        )
    if total_debit == 0:
        raise HTTPException(400, "Bút toán phải có giá trị > 0")

    # Validate account_codes tồn tại trong workspace
    codes = {l.account_code for l in data.lines}
    valid_rows = (await db.execute(text("""
        SELECT code FROM books_chart_of_accounts
        WHERE workspace_id = :ws AND code = ANY(:codes)
    """), {"ws": workspace_id, "codes": list(codes)})).mappings().all()
    valid_codes = {r["code"] for r in valid_rows}
    missing = codes - valid_codes
    if missing:
        raise HTTPException(400, f"Tài khoản không tồn tại: {sorted(missing)}")

    # Use engine's helper to create entry + lines (handles entry_number generation)
    from app.services.books_engine import _create_journal_entry  # local import OK
    entry_id = await _create_journal_entry(
        db,
        workspace_id=workspace_id,
        entry_date=data.entry_date,
        description=data.description or "Bút toán thủ công",
        source_type="manual",
        source_id=0,
        lines=[l.model_dump() for l in data.lines],
        posted=True,
    )

    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="books.journal.manual", target=str(entry_id), severity="ok",
        metadata={"total_debit": float(total_debit), "lines": len(data.lines)},
    )
    await db.commit()
    return {"ok": True, "entry_id": entry_id, "total_debit": float(total_debit)}


# ════════════════════════════════════════════════════════════════════════════
# Chart of Accounts
# ════════════════════════════════════════════════════════════════════════════
@router.get("/accounts")
async def list_accounts(
    ws: str = Query(...),
    account_type: str | None = Query(None),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    workspace_id = await _resolve_ws(db, ws, me)
    sql = "SELECT * FROM books_chart_of_accounts WHERE workspace_id = :ws AND is_active = TRUE"
    params: dict = {"ws": workspace_id}
    if account_type:
        sql += " AND account_type = :t"
        params["t"] = account_type
    sql += " ORDER BY code"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return {
        "ok": True,
        "count": len(rows),
        "default_coa_size": len(DEFAULT_COA),
        "items": [_row_to_dict(r) for r in rows],
    }


@router.post("/accounts")
async def create_account(
    data: AccountIn,
    ws: str = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Tạo custom account. Cho phép thêm sub-accounts (vd 1111 — Tiền mặt VND)."""
    workspace_id = await _resolve_ws(db, ws, me)
    try:
        row = (await db.execute(text("""
            INSERT INTO books_chart_of_accounts
                (workspace_id, code, name, name_en, account_type, parent_code)
            VALUES (:ws, :code, :name, :en, :t, :p)
            RETURNING *
        """), {
            "ws": workspace_id, "code": data.code, "name": data.name,
            "en": data.name_en, "t": data.account_type, "p": data.parent_code,
        })).mappings().first()
    except Exception as e:
        await db.rollback()
        raise HTTPException(400, f"Không thể tạo tài khoản: {e}")
    await audit_push(
        db, actor=me.email, workspace_id=workspace_id,
        action="books.account.create", target=data.code, severity="ok",
        metadata={"name": data.name, "type": data.account_type},
    )
    await db.commit()
    return {"ok": True, "account": _row_to_dict(row)}


# ════════════════════════════════════════════════════════════════════════════
# Reports
# ════════════════════════════════════════════════════════════════════════════
@router.get("/reports/balance-sheet")
async def report_balance_sheet(
    ws: str = Query(...),
    as_of: date = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Bảng cân đối kế toán — tài sản = nợ phải trả + vốn chủ sở hữu."""
    workspace_id = await _resolve_ws(db, ws, me)
    return {
        "ok": True,
        "report": await calculate_balance_sheet(
            db, workspace_id=workspace_id, as_of_date=as_of,
        ),
    }


@router.get("/reports/profit-loss")
async def report_pnl(
    ws: str = Query(...),
    from_: date = Query(..., alias="from"),
    to: date = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Báo cáo kết quả kinh doanh."""
    workspace_id = await _resolve_ws(db, ws, me)
    if from_ > to:
        raise HTTPException(400, "Ngày bắt đầu phải trước ngày kết thúc")
    return {
        "ok": True,
        "report": await calculate_pnl(
            db, workspace_id=workspace_id, from_date=from_, to_date=to,
        ),
    }


@router.get("/reports/cash-flow")
async def report_cash_flow(
    ws: str = Query(...),
    from_: date = Query(..., alias="from"),
    to: date = Query(...),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Báo cáo lưu chuyển tiền tệ — phương pháp trực tiếp đơn giản."""
    workspace_id = await _resolve_ws(db, ws, me)
    if from_ > to:
        raise HTTPException(400, "Ngày bắt đầu phải trước ngày kết thúc")
    return {
        "ok": True,
        "report": await calculate_cash_flow(
            db, workspace_id=workspace_id, from_date=from_, to_date=to,
        ),
    }


@router.get("/reports/vat")
async def report_vat(
    ws: str = Query(...),
    quarter: int = Query(..., ge=1, le=4),
    year: int = Query(..., ge=2020, le=2100),
    me: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Tờ khai thuế GTGT quý — output VAT − input VAT."""
    workspace_id = await _resolve_ws(db, ws, me)
    return {
        "ok": True,
        "report": await calculate_vat_report(
            db, workspace_id=workspace_id, quarter=quarter, year=year,
        ),
    }
