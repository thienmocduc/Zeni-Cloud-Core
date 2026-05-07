-- ============================================================================
-- Migration 025 — Zeni Books (Vietnamese accounting module — VAS-compliant)
-- Replaces MISA for Vietnamese SME (5-50 employees).
-- Tables:
--   books_chart_of_accounts  — Hệ thống tài khoản theo Thông tư 200/2014/TT-BTC
--   books_customers          — Khách hàng (TK 131 — phải thu)
--   books_suppliers          — Nhà cung cấp (TK 331 — phải trả)
--   books_products           — Hàng hoá / Dịch vụ
--   books_invoices           — Hoá đơn (sales / purchase)
--   books_invoice_items      — Chi tiết hoá đơn
--   books_journal_entries    — Sổ nhật ký chung (kế toán kép)
--   books_journal_lines      — Bút toán Nợ/Có theo từng tài khoản
--   books_expenses           — Phiếu chi phí
-- Plus auto-seed default VAS chart of accounts for every existing workspace.
-- ============================================================================

-- ── Chart of Accounts (Hệ thống tài khoản theo VAS) ─────────────────────────
CREATE TABLE IF NOT EXISTS books_chart_of_accounts (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    code VARCHAR(10) NOT NULL,                   -- '111','112','131','331','511','632','911'
    name VARCHAR(200) NOT NULL,
    name_en VARCHAR(200),
    account_type VARCHAR(20) NOT NULL,           -- 'asset','liability','equity','revenue','expense'
    parent_code VARCHAR(10),
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (workspace_id, code)
);
CREATE INDEX IF NOT EXISTS idx_books_coa_ws ON books_chart_of_accounts(workspace_id, code);

-- ── Customers (Khách hàng — đối ứng TK 131) ─────────────────────────────────
CREATE TABLE IF NOT EXISTS books_customers (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    customer_code VARCHAR(40) NOT NULL,
    name VARCHAR(200) NOT NULL,
    tax_code VARCHAR(20),                         -- MST 10/13 chữ số
    address TEXT,
    phone VARCHAR(20),
    email TEXT,
    contact_person VARCHAR(120),
    receivable_balance NUMERIC(20,2) DEFAULT 0,   -- TK 131
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (workspace_id, customer_code)
);
CREATE INDEX IF NOT EXISTS idx_books_customers_ws ON books_customers(workspace_id, is_active);

-- ── Suppliers (Nhà cung cấp — đối ứng TK 331) ───────────────────────────────
CREATE TABLE IF NOT EXISTS books_suppliers (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    supplier_code VARCHAR(40) NOT NULL,
    name VARCHAR(200) NOT NULL,
    tax_code VARCHAR(20),
    address TEXT,
    phone VARCHAR(20),
    email TEXT,
    payable_balance NUMERIC(20,2) DEFAULT 0,      -- TK 331
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (workspace_id, supplier_code)
);
CREATE INDEX IF NOT EXISTS idx_books_suppliers_ws ON books_suppliers(workspace_id, is_active);

-- ── Products / Services (Hàng hoá / Dịch vụ) ────────────────────────────────
CREATE TABLE IF NOT EXISTS books_products (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    product_code VARCHAR(40) NOT NULL,
    name VARCHAR(200) NOT NULL,
    unit VARCHAR(40) DEFAULT 'cái',               -- cái, kg, m, gói...
    sale_price NUMERIC(20,2),
    cost_price NUMERIC(20,2),
    vat_rate NUMERIC(5,2) DEFAULT 10,             -- 0/5/8/10
    inventory_quantity NUMERIC(15,3) DEFAULT 0,
    product_type VARCHAR(20) DEFAULT 'goods',    -- 'goods','service'
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (workspace_id, product_code)
);
CREATE INDEX IF NOT EXISTS idx_books_products_ws ON books_products(workspace_id, is_active);

-- ── Invoices (Hoá đơn — sales / purchase) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS books_invoices (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    invoice_number VARCHAR(40) NOT NULL,          -- HD-202604-001 hoặc theo template VAT eInvoice
    invoice_type VARCHAR(20) DEFAULT 'sale',      -- 'sale','purchase','adjustment'
    customer_id BIGINT REFERENCES books_customers(id),
    supplier_id BIGINT REFERENCES books_suppliers(id),
    issue_date DATE NOT NULL,
    due_date DATE,
    subtotal NUMERIC(20,2) NOT NULL,
    vat_amount NUMERIC(20,2) NOT NULL,
    total NUMERIC(20,2) NOT NULL,
    paid_amount NUMERIC(20,2) DEFAULT 0,
    status VARCHAR(20) DEFAULT 'draft',           -- 'draft','issued','paid','partial','cancelled'
    notes TEXT,
    einvoice_status VARCHAR(20) DEFAULT 'pending',-- 'pending','signed','sent','rejected'
    einvoice_xml TEXT,                            -- XML signed by VNPT/Viettel eInvoice
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (workspace_id, invoice_number)
);
CREATE INDEX IF NOT EXISTS idx_books_invoices_ws ON books_invoices(workspace_id, issue_date DESC);
CREATE INDEX IF NOT EXISTS idx_books_invoices_status ON books_invoices(workspace_id, status);

-- ── Invoice line items ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS books_invoice_items (
    id BIGSERIAL PRIMARY KEY,
    invoice_id BIGINT NOT NULL REFERENCES books_invoices(id) ON DELETE CASCADE,
    product_id BIGINT REFERENCES books_products(id),
    description TEXT NOT NULL,
    quantity NUMERIC(15,3) NOT NULL,
    unit_price NUMERIC(20,2) NOT NULL,
    vat_rate NUMERIC(5,2) DEFAULT 10,
    line_subtotal NUMERIC(20,2) NOT NULL,
    line_vat NUMERIC(20,2) NOT NULL,
    line_total NUMERIC(20,2) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_books_invoice_items_inv ON books_invoice_items(invoice_id);

-- ── Journal entries (Sổ nhật ký chung — kế toán kép) ────────────────────────
CREATE TABLE IF NOT EXISTS books_journal_entries (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    entry_number VARCHAR(40) NOT NULL,
    entry_date DATE NOT NULL,
    description TEXT,
    source_type VARCHAR(20),                      -- 'invoice','expense','manual','payment'
    source_id BIGINT,
    total_debit NUMERIC(20,2) NOT NULL,
    total_credit NUMERIC(20,2) NOT NULL,
    posted BOOLEAN DEFAULT FALSE,                 -- once posted, không sửa được
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (workspace_id, entry_number),
    CHECK (total_debit = total_credit)
);
CREATE INDEX IF NOT EXISTS idx_books_journal_ws ON books_journal_entries(workspace_id, entry_date DESC);
CREATE INDEX IF NOT EXISTS idx_books_journal_source ON books_journal_entries(source_type, source_id);

-- ── Journal entry lines (Nợ / Có theo từng tài khoản) ───────────────────────
CREATE TABLE IF NOT EXISTS books_journal_lines (
    id BIGSERIAL PRIMARY KEY,
    entry_id BIGINT NOT NULL REFERENCES books_journal_entries(id) ON DELETE CASCADE,
    account_code VARCHAR(10) NOT NULL,
    debit NUMERIC(20,2) DEFAULT 0,
    credit NUMERIC(20,2) DEFAULT 0,
    description TEXT
);
CREATE INDEX IF NOT EXISTS idx_books_journal_lines_entry ON books_journal_lines(entry_id);
CREATE INDEX IF NOT EXISTS idx_books_journal_lines_acct ON books_journal_lines(account_code);

-- ── Expenses (Phiếu chi phí) ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS books_expenses (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    expense_number VARCHAR(40) NOT NULL,
    expense_date DATE NOT NULL,
    category VARCHAR(40),                         -- 'salary','rent','utilities','marketing','tax'
    supplier_id BIGINT REFERENCES books_suppliers(id),
    amount NUMERIC(20,2) NOT NULL,
    vat_amount NUMERIC(20,2) DEFAULT 0,
    payment_method VARCHAR(20),                   -- 'cash','bank_transfer','card'
    description TEXT,
    receipt_image_url TEXT,
    status VARCHAR(20) DEFAULT 'recorded',       -- 'recorded','paid','reimbursed'
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (workspace_id, expense_number)
);
CREATE INDEX IF NOT EXISTS idx_books_expenses_ws ON books_expenses(workspace_id, expense_date DESC);
CREATE INDEX IF NOT EXISTS idx_books_expenses_supplier ON books_expenses(supplier_id);
CREATE INDEX IF NOT EXISTS idx_books_expenses_category ON books_expenses(workspace_id, category);

-- ════════════════════════════════════════════════════════════════════════════
-- Auto-seed default Vietnamese VAS chart of accounts for every workspace.
-- Source: Thông tư 200/2014/TT-BTC — Chế độ kế toán doanh nghiệp.
-- ON CONFLICT DO NOTHING — idempotent (re-run an pass an).
-- Seeding mới được trigger thêm khi workspace mới tạo qua services/books_engine.
-- ════════════════════════════════════════════════════════════════════════════
INSERT INTO books_chart_of_accounts (workspace_id, code, name, name_en, account_type, parent_code)
SELECT w.id, x.code, x.name, x.name_en, x.account_type, x.parent_code
FROM workspaces w
CROSS JOIN (VALUES
    -- Loại 1: Tài sản ngắn hạn
    ('111', 'Tiền mặt', 'Cash on hand', 'asset', NULL),
    ('112', 'Tiền gửi ngân hàng', 'Bank deposits', 'asset', NULL),
    ('113', 'Tiền đang chuyển', 'Cash in transit', 'asset', NULL),
    ('121', 'Chứng khoán kinh doanh', 'Trading securities', 'asset', NULL),
    ('128', 'Đầu tư nắm giữ đến ngày đáo hạn', 'Held-to-maturity investments', 'asset', NULL),
    ('131', 'Phải thu của khách hàng', 'Accounts receivable', 'asset', NULL),
    ('133', 'Thuế GTGT được khấu trừ', 'VAT input deductible', 'asset', NULL),
    ('138', 'Phải thu khác', 'Other receivables', 'asset', NULL),
    ('141', 'Tạm ứng', 'Advances to employees', 'asset', NULL),
    ('152', 'Nguyên liệu, vật liệu', 'Raw materials', 'asset', NULL),
    ('153', 'Công cụ, dụng cụ', 'Tools and supplies', 'asset', NULL),
    ('154', 'Chi phí sản xuất, kinh doanh dở dang', 'Work in progress', 'asset', NULL),
    ('155', 'Thành phẩm', 'Finished goods', 'asset', NULL),
    ('156', 'Hàng hoá', 'Goods for sale', 'asset', NULL),
    ('157', 'Hàng gửi đi bán', 'Goods on consignment', 'asset', NULL),
    -- Loại 2: Tài sản dài hạn
    ('211', 'Tài sản cố định hữu hình', 'Tangible fixed assets', 'asset', NULL),
    ('213', 'Tài sản cố định vô hình', 'Intangible fixed assets', 'asset', NULL),
    ('214', 'Hao mòn tài sản cố định', 'Accumulated depreciation', 'asset', NULL),
    ('228', 'Đầu tư khác', 'Other long-term investments', 'asset', NULL),
    ('242', 'Chi phí trả trước', 'Prepaid expenses', 'asset', NULL),
    -- Loại 3: Nợ phải trả
    ('331', 'Phải trả cho người bán', 'Accounts payable', 'liability', NULL),
    ('333', 'Thuế và các khoản phải nộp Nhà nước', 'Taxes payable to State', 'liability', NULL),
    ('334', 'Phải trả người lao động', 'Salaries payable', 'liability', NULL),
    ('335', 'Chi phí phải trả', 'Accrued expenses', 'liability', NULL),
    ('338', 'Phải trả, phải nộp khác', 'Other payables', 'liability', NULL),
    ('341', 'Vay và nợ thuê tài chính', 'Loans and finance lease', 'liability', NULL),
    ('352', 'Dự phòng phải trả', 'Provisions for liabilities', 'liability', NULL),
    -- Loại 4: Vốn chủ sở hữu
    ('411', 'Vốn đầu tư của chủ sở hữu', 'Owner equity', 'equity', NULL),
    ('414', 'Quỹ đầu tư phát triển', 'Investment fund', 'equity', NULL),
    ('418', 'Các quỹ khác thuộc vốn chủ sở hữu', 'Other owner funds', 'equity', NULL),
    ('421', 'Lợi nhuận sau thuế chưa phân phối', 'Retained earnings', 'equity', NULL),
    -- Loại 5: Doanh thu
    ('511', 'Doanh thu bán hàng và cung cấp dịch vụ', 'Sales revenue', 'revenue', NULL),
    ('515', 'Doanh thu hoạt động tài chính', 'Financial revenue', 'revenue', NULL),
    ('521', 'Các khoản giảm trừ doanh thu', 'Sales deductions', 'revenue', NULL),
    -- Loại 6: Chi phí sản xuất, kinh doanh
    ('621', 'Chi phí nguyên liệu, vật liệu trực tiếp', 'Direct material costs', 'expense', NULL),
    ('622', 'Chi phí nhân công trực tiếp', 'Direct labor costs', 'expense', NULL),
    ('627', 'Chi phí sản xuất chung', 'Manufacturing overhead', 'expense', NULL),
    ('632', 'Giá vốn hàng bán', 'Cost of goods sold', 'expense', NULL),
    ('635', 'Chi phí tài chính', 'Financial expenses', 'expense', NULL),
    ('641', 'Chi phí bán hàng', 'Selling expenses', 'expense', NULL),
    ('642', 'Chi phí quản lý doanh nghiệp', 'Admin expenses', 'expense', NULL),
    -- Loại 7: Thu nhập khác
    ('711', 'Thu nhập khác', 'Other income', 'revenue', NULL),
    -- Loại 8: Chi phí khác
    ('811', 'Chi phí khác', 'Other expenses', 'expense', NULL),
    ('821', 'Chi phí thuế thu nhập doanh nghiệp', 'Corporate income tax expense', 'expense', NULL),
    -- Loại 9: Xác định kết quả kinh doanh
    ('911', 'Xác định kết quả kinh doanh', 'Income summary', 'equity', NULL)
) AS x(code, name, name_en, account_type, parent_code)
ON CONFLICT (workspace_id, code) DO NOTHING;
