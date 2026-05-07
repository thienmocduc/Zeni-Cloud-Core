/*
 * ZENI CLOUD CORE · Extended Modules UI (Stream A6)
 *
 * Loaded AFTER zeni-realdata.js + zeni-app-views.js. Hooks into setView and
 * window.ZeniAPI to add 5 new tabs:
 *   - vector         : Vector DB (pgvector) — list, create, search playground
 *   - cache-queue    : Cache + Queue (Postgres) — set/get keys, push/pull jobs
 *   - ocr-translate  : OCR + Translate (Cloud Vision + Cloud Translation)
 *   - sms-slack      : SMS (Stringee/Twilio) + Slack (webhook + bot API)
 *   - entities       : Multi-Entity Billing — legal entities + revenue + intercompany
 *
 * KHÔNG sửa zeni-api.js. Toàn bộ render đi vào #content (replace nội dung
 * "Unknown view" mà router gốc tạo cho tab lạ).
 *
 * Pattern (DNA section 6 — frontend patching):
 *   - extend window.ZeniAPI.<module> mà không sửa file gốc
 *   - wrap window.setView để inject renderer cho 5 view mới
 *   - không phá behavior cũ (gọi _origSetView trước, sau đó override #content)
 */
(function () {
  'use strict';

  // ─── Boot guard: chờ ZeniAPI + state + setView sẵn sàng ────────
  if (!window.ZeniAPI || !window.state || !window.setView) {
    setTimeout(arguments.callee, 100);
    return;
  }

  const log = (...a) => console.log('[zeni-ext]', ...a);
  const ACCESS_KEY = 'zeni.jwt.access';

  /* ═══════════════════════════════════════════════════════════════
     1. LOCAL HTTP CLIENT (mirror của _fetch trong zeni-api.js)
     ═══════════════════════════════════════════════════════════════ */
  const API_BASE = (typeof window.ZENI_API_BASE === 'string' && window.ZENI_API_BASE) || '/api/v1';

  async function _fetch(path, { method = 'GET', body, headers = {}, noAuth = false } = {}) {
    const url = API_BASE + path;
    const opts = {
      method,
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json', ...headers },
    };
    if (!noAuth) {
      const tok = localStorage.getItem(ACCESS_KEY);
      if (tok) opts.headers.Authorization = 'Bearer ' + tok;
    }
    if (body !== undefined) opts.body = typeof body === 'string' ? body : JSON.stringify(body);

    let res;
    try { res = await fetch(url, opts); }
    catch (netErr) {
      const err = new Error('Không kết nối được backend (' + netErr.message + ')');
      err.network = true;
      throw err;
    }
    if (!res.ok) {
      const detail = await res.json().catch(() => ({ detail: res.statusText }));
      const msg = typeof detail.detail === 'string' ? detail.detail : JSON.stringify(detail.detail || detail);
      const err = new Error(msg);
      err.status = res.status;
      err.body = detail;
      throw err;
    }
    if (res.status === 204) return null;
    const ct = res.headers.get('content-type') || '';
    return ct.includes('application/json') ? res.json() : res.text();
  }
  // expose helper cho debug + cho code khác có thể tái dùng
  window.ZeniAPI._fetch = window.ZeniAPI._fetch || _fetch;

  /* ═══════════════════════════════════════════════════════════════
     2. EXTEND window.ZeniAPI VỚI 8 MODULE MỚI
     ═══════════════════════════════════════════════════════════════ */
  const Z = window.ZeniAPI;
  const wsq = () => encodeURIComponent(window.state.currentWs || 'anima');

  Z.vector = {
    list: () => _fetch('/vector/collections?ws=' + wsq()),
    create: (body) => _fetch('/vector/collections?ws=' + wsq(), { method: 'POST', body }),
    upsert: (name, points) => _fetch('/vector/' + encodeURIComponent(name) + '/upsert?ws=' + wsq(), { method: 'POST', body: { points } }),
    search: (name, vector, k, filter) => _fetch('/vector/' + encodeURIComponent(name) + '/search?ws=' + wsq(), { method: 'POST', body: { vector, k, filter } }),
    drop: (name) => _fetch('/vector/' + encodeURIComponent(name) + '?ws=' + wsq(), { method: 'DELETE' }),
    embed: (text) => _fetch('/ai/complete?ws=' + wsq(), { method: 'POST', body: { kind: 'embed', input: text } }),
  };

  Z.cache = {
    set: (key, value, ttl) => _fetch('/cache/' + encodeURIComponent(key) + '?ws=' + wsq(), { method: 'POST', body: { value, ttl_seconds: ttl } }),
    get: (key) => _fetch('/cache/' + encodeURIComponent(key) + '?ws=' + wsq()),
    del: (key) => _fetch('/cache/' + encodeURIComponent(key) + '?ws=' + wsq(), { method: 'DELETE' }),
    list: (prefix) => _fetch('/cache?ws=' + wsq() + (prefix ? '&prefix=' + encodeURIComponent(prefix) : '')),
  };

  Z.queue = {
    push: (name, payload, delay) => _fetch('/queue/' + encodeURIComponent(name) + '/push?ws=' + wsq(), { method: 'POST', body: { payload, delay_seconds: delay } }),
    pull: (name, lease) => _fetch('/queue/' + encodeURIComponent(name) + '/pull?ws=' + wsq(), { method: 'POST', body: { lease_seconds: lease || 60 } }),
    ack: (name, jobId, success, error) => _fetch('/queue/' + encodeURIComponent(name) + '/ack?ws=' + wsq(), { method: 'POST', body: { job_id: jobId, success, error } }),
    stats: (name) => _fetch('/queue/' + encodeURIComponent(name) + '/stats?ws=' + wsq()),
  };

  Z.ocr = {
    image: (body) => _fetch('/ocr/image?ws=' + wsq(), { method: 'POST', body }),
    pdf: (body) => _fetch('/ocr/pdf?ws=' + wsq(), { method: 'POST', body }),
  };

  Z.translate = {
    text: (body) => _fetch('/translate?ws=' + wsq(), { method: 'POST', body }),
  };

  Z.sms = {
    send: (body) => _fetch('/sms/send?ws=' + wsq(), { method: 'POST', body }),
  };

  Z.slack = {
    webhook: (body) => _fetch('/slack/webhook?ws=' + wsq(), { method: 'POST', body }),
    post: (body) => _fetch('/slack/post?ws=' + wsq(), { method: 'POST', body }),
  };

  Z.legalEntities = {
    list: () => _fetch('/legal-entities'),
    create: (body) => _fetch('/legal-entities', { method: 'POST', body }),
    update: (id, body) => _fetch('/legal-entities/' + encodeURIComponent(id), { method: 'PATCH', body }),
    revenueByEntity: (period) => _fetch('/billing/revenue-by-entity' + (period ? '?period=' + encodeURIComponent(period) : '')),
    runIntercompany: (body) => _fetch('/billing/intercompany/run', { method: 'POST', body }),
  };

  /* ═══════════════════════════════════════════════════════════════
     3. UI HELPERS (kế thừa style index.html)
     ═══════════════════════════════════════════════════════════════ */
  function escHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function fmtVnd(n) { return Math.round(n || 0).toLocaleString('vi-VN') + 'đ'; }
  function fmtNum(n) { return (n || 0).toLocaleString('vi-VN'); }
  function timeAgo(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    const s = Math.floor((Date.now() - d.getTime()) / 1000);
    if (s < 60) return s + 's trước';
    if (s < 3600) return Math.floor(s / 60) + 'ph trước';
    if (s < 86400) return Math.floor(s / 3600) + 'h trước';
    return Math.floor(s / 86400) + 'ng trước';
  }
  function emit(kind, msg) {
    if (typeof window.toast === 'function') window.toast(msg, kind);
    else if (kind === 'err') console.error('[zeni-ext]', msg);
    else console.log('[zeni-ext]', msg);
  }

  // Loading spinner inline (dark-friendly)
  const spinnerHtml = `
    <span class="zeni-spinner" style="display:inline-block;width:16px;height:16px;border:2px solid rgba(168,139,250,0.2);border-top-color:#A855F7;border-radius:50%;animation:zeni-spin 0.7s linear infinite;vertical-align:middle;"></span>`;

  function loadingBlock(text) {
    return `<div style="padding:30px;text-align:center;color:var(--ink-300);font-size:13px;">${spinnerHtml}<span style="margin-left:10px;">${escHtml(text || 'Đang tải dữ liệu…')}</span></div>`;
  }
  function errorBlock(msg) {
    return `<div style="padding:24px;background:rgba(244,114,182,0.08);border:1px solid rgba(244,114,182,0.25);border-radius:10px;color:#F472B6;font-size:13px;">⚠ ${escHtml(msg)}</div>`;
  }
  function emptyBlock(msg) {
    return `<div style="padding:32px;text-align:center;color:var(--ink-400);font-size:13px;background:rgba(168,139,250,0.04);border:1px dashed var(--border-soft);border-radius:10px;">${escHtml(msg)}</div>`;
  }

  // Tiny modal opener (dùng .modal-wrap + .modal có sẵn trong index.html)
  function openModal(title, bodyHtml, footerHtml) {
    const wrap = document.getElementById('modal-wrap');
    const modal = document.getElementById('modal');
    if (!wrap || !modal) return null;
    modal.innerHTML = `
      <div class="modal-head">
        <div>
          <div class="modal-title">${escHtml(title)}</div>
        </div>
        <button class="modal-close" onclick="document.getElementById('modal-wrap').classList.remove('active')">×</button>
      </div>
      <div class="modal-body">${bodyHtml || ''}</div>
      ${footerHtml ? '<div class="modal-foot">' + footerHtml + '</div>' : ''}
    `;
    wrap.classList.add('active');
    wrap.onclick = (e) => { if (e.target === wrap) wrap.classList.remove('active'); };
    return modal;
  }
  function closeModal() {
    const wrap = document.getElementById('modal-wrap');
    if (wrap) wrap.classList.remove('active');
  }

  function confirmDialog(title, msg) {
    return new Promise((resolve) => {
      const m = openModal(
        title,
        `<div style="font-size:14px;color:var(--ink-100);line-height:1.6;">${escHtml(msg)}</div>`,
        `<button class="btn btn-md btn-ghost" id="zeni-confirm-cancel">Huỷ</button>
         <button class="btn btn-md btn-danger" id="zeni-confirm-ok">Xác nhận</button>`
      );
      if (!m) { resolve(false); return; }
      document.getElementById('zeni-confirm-cancel').onclick = () => { closeModal(); resolve(false); };
      document.getElementById('zeni-confirm-ok').onclick = () => { closeModal(); resolve(true); };
    });
  }

  // Common page header (style giống .page-head sẵn có)
  function pageHeader(title, sub, layerColor) {
    return `
      <div class="layer-hero" style="--layer-color:${layerColor || '#A855F7'};">
        <div style="font-family:var(--font-mono);font-size:10px;letter-spacing:0.18em;text-transform:uppercase;color:${layerColor || '#A855F7'};margin-bottom:8px;">Module mở rộng</div>
        <h1 class="page-title" style="font-size:30px;">${escHtml(title)}</h1>
        <p style="font-size:14px;color:var(--ink-300);margin-top:6px;max-width:720px;line-height:1.55;">${escHtml(sub || '')}</p>
      </div>`;
  }

  /* ═══════════════════════════════════════════════════════════════
     4. INJECT CSS BỔ SUNG (chỉ cho component mới)
     ═══════════════════════════════════════════════════════════════ */
  (function injectCss() {
    if (document.getElementById('zeni-ext-style')) return;
    const css = `
      @keyframes zeni-spin { to { transform: rotate(360deg); } }
      .zeni-tabs { display:flex; gap:4px; padding:4px; background:rgba(10,5,32,0.6); border:1px solid var(--border-soft); border-radius:10px; margin-bottom:16px; width:fit-content; }
      .zeni-tab { padding:8px 16px; font-family:var(--font-mono); font-size:11px; letter-spacing:0.1em; text-transform:uppercase; color:var(--ink-400); border-radius:6px; cursor:pointer; transition:all 0.2s; font-weight:600; }
      .zeni-tab:hover { color:var(--ink-200); background:rgba(168,139,250,0.06); }
      .zeni-tab.active { background:linear-gradient(135deg, rgba(99,102,241,0.2), rgba(168,85,247,0.15)); color:var(--crown-gold); box-shadow:0 0 12px rgba(168,85,247,0.2); }
      .zeni-table { width:100%; font-size:13px; color:var(--ink-100); border-collapse:collapse; }
      .zeni-table thead th { padding:10px 8px; font-family:var(--font-mono); font-size:10px; letter-spacing:0.1em; text-transform:uppercase; color:var(--ink-400); text-align:left; border-bottom:1px solid var(--border); }
      .zeni-table tbody tr { border-bottom:1px solid var(--border-soft); transition:background 0.15s; }
      .zeni-table tbody tr:hover { background:rgba(168,139,250,0.04); }
      .zeni-table td { padding:10px 8px; vertical-align:middle; }
      .zeni-table td.mono { font-family:var(--font-mono); font-size:12px; color:var(--ink-200); }
      .zeni-row-action { color:var(--err); cursor:pointer; font-size:12px; padding:4px 8px; border-radius:4px; transition:background 0.15s; }
      .zeni-row-action:hover { background:rgba(244,114,182,0.1); }
      .cost-preview { display:inline-block; padding:6px 12px; background:rgba(253,230,138,0.08); border:1px solid rgba(253,230,138,0.25); border-radius:6px; color:var(--crown-gold); font-family:var(--font-mono); font-size:11px; letter-spacing:0.05em; }
      .ext-card { background:linear-gradient(135deg, rgba(168,85,247,0.04), rgba(99,102,241,0.02)); border:1px solid var(--border); border-radius:14px; padding:18px; margin-bottom:14px; }
      .ext-grid-2 { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
      @media (max-width: 768px) { .ext-grid-2 { grid-template-columns:1fr; } }
      .ext-grid-3 { display:grid; grid-template-columns:repeat(3, 1fr); gap:14px; }
      @media (max-width: 768px) { .ext-grid-3 { grid-template-columns:1fr; } }
      .ext-stat-card { background:rgba(168,139,250,0.05); border:1px solid var(--border-soft); border-radius:10px; padding:14px; }
      .ext-stat-label { font-family:var(--font-mono); font-size:10px; text-transform:uppercase; color:var(--ink-400); letter-spacing:0.1em; margin-bottom:6px; }
      .ext-stat-value { font-size:22px; font-weight:800; color:var(--ink-50); }
      .ext-drop {
        border: 2px dashed var(--border-strong);
        border-radius: 12px;
        padding: 32px 18px;
        text-align: center;
        color: var(--ink-300);
        background: rgba(168,139,250,0.04);
        transition: all 0.2s;
        cursor: pointer;
      }
      .ext-drop:hover, .ext-drop.dragover { border-color: var(--crown); background: rgba(168,85,247,0.08); color: var(--ink-100); }
      .ext-drop input[type=file] { display:none; }
      .ext-bar-row { display:flex; align-items:center; gap:10px; padding:8px 0; }
      .ext-bar-label { width:160px; font-size:12px; color:var(--ink-200); }
      .ext-bar-track { flex:1; height:10px; background:rgba(168,139,250,0.1); border-radius:5px; overflow:hidden; }
      .ext-bar-fill { height:100%; background:linear-gradient(90deg, var(--ajna), var(--crown)); border-radius:5px; }
      .ext-bar-val { width:120px; text-align:right; font-family:var(--font-mono); font-size:11px; color:var(--crown-gold); }
      .ext-history-row { padding:8px 10px; border-radius:6px; background:rgba(34,211,238,0.04); margin-bottom:4px; font-size:12px; display:flex; justify-content:space-between; align-items:center; }
      .ext-history-row.err { background:rgba(244,114,182,0.06); }
      .ext-form-row { margin-bottom: 12px; }
      .ext-form-label { font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--crown-light); margin-bottom: 7px; font-weight: 600; display: block; }
      .ext-form-input, .ext-form-textarea, .ext-form-select {
        width:100%; padding:11px 13px; background:rgba(10,5,32,0.6); border:1px solid var(--border);
        border-radius:10px; color:var(--ink-50); font-family:inherit; font-size:13px; transition:all 0.2s;
      }
      .ext-form-textarea { font-family:var(--font-mono); font-size:12px; min-height:84px; resize:vertical; }
      .ext-form-input:focus, .ext-form-textarea:focus, .ext-form-select:focus {
        border-color:var(--ajna-light); background:rgba(99,102,241,0.08); outline:none;
        box-shadow:0 0 0 3px rgba(99,102,241,0.12);
      }
      .ext-hint { font-size:11px; color:var(--ink-400); margin-top:5px; }
      .ext-counter { font-family:var(--font-mono); font-size:10px; color:var(--ink-500); margin-top:4px; text-align:right; }
      @media (max-width: 768px) {
        .ext-form-row > div[style*="grid-template-columns"] { grid-template-columns: 1fr !important; }
      }
    `;
    const style = document.createElement('style');
    style.id = 'zeni-ext-style';
    style.textContent = css;
    document.head.appendChild(style);
  })();

  /* ═══════════════════════════════════════════════════════════════
     5. RENDERER UTIL — replace #content fully
     ═══════════════════════════════════════════════════════════════ */
  function getContentRoot() { return document.getElementById('content'); }

  function setCrumb(label) {
    const c = document.getElementById('crumb-cur');
    if (c) c.textContent = label;
  }

  function renderInto(html) {
    const root = getContentRoot();
    if (!root) return;
    root.innerHTML = html;
    const view = document.createElement('div'); // wrap to keep .view active style if needed
    // do nothing extra — innerHTML already replaced
  }

  /* ═══════════════════════════════════════════════════════════════
     6. RENDER VIEW: VECTOR DB
     ═══════════════════════════════════════════════════════════════ */
  async function renderVector() {
    setCrumb('Vector DB');
    const root = getContentRoot();
    if (!root) return;
    root.innerHTML = pageHeader(
      'Vector DB · pgvector',
      'Lưu trữ và tìm kiếm vector ngữ nghĩa trên Postgres. Phù hợp cho RAG, similarity search, recommendation. Giá $0.10/1K vector lưu/tháng + $0.05/1K truy vấn.',
      '#22D3EE'
    ) + `
      <div class="card">
        <div class="card-head">
          <div class="card-title"><span>Collections</span></div>
          <div class="card-actions">
            <button class="btn btn-md btn-accent" id="vec-btn-new">+ Collection mới</button>
            <button class="btn btn-md btn-ghost" id="vec-btn-refresh">Tải lại</button>
          </div>
        </div>
        <div id="vec-list">${loadingBlock('Đang lấy danh sách collections…')}</div>
      </div>
      <div class="card" id="vec-playground-card" style="display:none;">
        <div class="card-head">
          <div class="card-title"><span>Search Playground</span><span class="tag" id="vec-pg-name">—</span></div>
          <div class="card-actions">
            <button class="btn btn-sm btn-ghost" id="vec-pg-close">Đóng</button>
          </div>
        </div>
        <div id="vec-playground-body"></div>
      </div>
    `;

    // load collections
    async function loadList() {
      const target = document.getElementById('vec-list');
      target.innerHTML = loadingBlock('Đang lấy danh sách collections…');
      try {
        const list = await Z.vector.list();
        if (!list || list.length === 0) {
          target.innerHTML = emptyBlock('Chưa có collection nào. Bấm "+ Collection mới" để tạo.');
          return;
        }
        target.innerHTML = `
          <table class="zeni-table">
            <thead>
              <tr><th>Tên</th><th>Dim</th><th>Metric</th><th>Số dòng</th><th>Tạo lúc</th><th></th></tr>
            </thead>
            <tbody>
              ${list.map(c => `
                <tr data-name="${escHtml(c.name)}">
                  <td class="mono" style="color:var(--aurora-cyan);font-weight:700;cursor:pointer;" data-action="open">${escHtml(c.name)}</td>
                  <td class="mono">${c.dim}</td>
                  <td><span class="pill pill-info">${escHtml(c.metric || 'cosine')}</span></td>
                  <td class="mono">${fmtNum(c.row_count || 0)}</td>
                  <td style="color:var(--ink-400);font-size:11px;">${timeAgo(c.created_at)}</td>
                  <td><span class="zeni-row-action" data-action="delete">Xoá</span></td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        `;
        target.querySelectorAll('tr[data-name]').forEach(tr => {
          const name = tr.getAttribute('data-name');
          tr.querySelector('[data-action=open]').onclick = () => openPlayground(name);
          tr.querySelector('[data-action=delete]').onclick = async () => {
            const ok = await confirmDialog('Xoá collection?', 'Sẽ xoá toàn bộ vector trong collection "' + name + '". Hành động không thể hoàn tác.');
            if (!ok) return;
            try { await Z.vector.drop(name); emit('ok', '✓ Đã xoá collection ' + name); loadList(); }
            catch (e) { emit('err', 'Lỗi xoá: ' + e.message); }
          };
        });
      } catch (e) {
        target.innerHTML = errorBlock('Lỗi tải collections: ' + e.message);
      }
    }

    function openCreateModal() {
      openModal(
        'Tạo Vector Collection',
        `
          <div class="ext-form-row">
            <label class="ext-form-label">Tên collection</label>
            <input class="ext-form-input" id="vc-name" placeholder="vd: legal_docs_2026" />
            <div class="ext-hint">Chỉ chứa chữ thường, số, dấu gạch dưới (3-32 ký tự)</div>
          </div>
          <div class="ext-form-row">
            <label class="ext-form-label">Số chiều (dim) — <span id="vc-dim-val">768</span></label>
            <input type="range" min="64" max="1536" step="32" value="768" id="vc-dim" style="width:100%;" />
            <div class="ext-hint">Khớp với model embedding (Gemini = 768, OpenAI ada = 1536)</div>
          </div>
          <div class="ext-form-row">
            <label class="ext-form-label">Metric khoảng cách</label>
            <div style="display:flex;gap:14px;">
              <label style="font-size:13px;color:var(--ink-100);"><input type="radio" name="vc-metric" value="cosine" checked /> cosine (mặc định)</label>
              <label style="font-size:13px;color:var(--ink-100);"><input type="radio" name="vc-metric" value="l2" /> l2 (Euclid)</label>
              <label style="font-size:13px;color:var(--ink-100);"><input type="radio" name="vc-metric" value="ip" /> ip (Inner product)</label>
            </div>
          </div>
        `,
        `<button class="btn btn-md btn-ghost" onclick="document.getElementById('modal-wrap').classList.remove('active')">Huỷ</button>
         <button class="btn btn-md btn-accent" id="vc-submit">Tạo collection</button>`
      );
      const dimSlider = document.getElementById('vc-dim');
      const dimVal = document.getElementById('vc-dim-val');
      dimSlider.oninput = () => { dimVal.textContent = dimSlider.value; };
      document.getElementById('vc-submit').onclick = async () => {
        const name = document.getElementById('vc-name').value.trim();
        const dim = parseInt(dimSlider.value, 10);
        const metric = document.querySelector('input[name=vc-metric]:checked').value;
        if (!/^[a-z][a-z0-9_]{2,31}$/.test(name)) {
          emit('warn', 'Tên không hợp lệ: chữ thường, số, gạch dưới (3-32 ký tự)');
          return;
        }
        if (dim < 1 || dim > 4096) { emit('warn', 'Dim phải trong khoảng 1-4096'); return; }
        try {
          await Z.vector.create({ name, dim, metric });
          emit('ok', '✓ Đã tạo collection ' + name);
          closeModal();
          loadList();
        } catch (e) {
          emit('err', 'Lỗi tạo: ' + e.message);
        }
      };
    }

    function openPlayground(name) {
      const card = document.getElementById('vec-playground-card');
      const body = document.getElementById('vec-playground-body');
      document.getElementById('vec-pg-name').textContent = name;
      card.style.display = '';
      body.innerHTML = `
        <div class="ext-grid-2">
          <div>
            <div class="ext-form-row">
              <label class="ext-form-label">Tạo vector từ text (Gemini Embed)</label>
              <textarea class="ext-form-textarea" id="vp-text" placeholder="Nhập đoạn văn bản để embedding…"></textarea>
              <button class="btn btn-sm btn-ghost" id="vp-embed" style="margin-top:8px;">⚡ Embed text → vector</button>
            </div>
            <div class="ext-form-row">
              <label class="ext-form-label">Hoặc dán JSON array trực tiếp</label>
              <textarea class="ext-form-textarea" id="vp-vector" placeholder='[0.12, -0.45, 0.78, ...]'></textarea>
            </div>
          </div>
          <div>
            <div class="ext-form-row">
              <label class="ext-form-label">k (top results) — <span id="vp-k-val">10</span></label>
              <input type="range" min="1" max="50" value="10" id="vp-k" style="width:100%;" />
            </div>
            <div class="ext-form-row">
              <label class="ext-form-label">Filter (optional, JSON)</label>
              <textarea class="ext-form-textarea" id="vp-filter" placeholder='{"category": "legal"}'></textarea>
            </div>
            <button class="btn btn-md btn-accent" id="vp-search">🔍 Tìm kiếm</button>
            <span class="cost-preview" style="margin-left:10px;">~ ${fmtVnd(1.2)} / lần search</span>
          </div>
        </div>
        <div style="margin-top:18px;">
          <div style="font-family:var(--font-mono);font-size:10px;letter-spacing:0.12em;text-transform:uppercase;color:var(--crown-light);margin-bottom:8px;">Kết quả</div>
          <div id="vp-results">${emptyBlock('Chưa có kết quả. Nhập vector và bấm "Tìm kiếm".')}</div>
        </div>
      `;
      const kSlider = document.getElementById('vp-k');
      const kVal = document.getElementById('vp-k-val');
      kSlider.oninput = () => { kVal.textContent = kSlider.value; };

      document.getElementById('vec-pg-close').onclick = () => { card.style.display = 'none'; };

      document.getElementById('vp-embed').onclick = async () => {
        const text = document.getElementById('vp-text').value.trim();
        if (!text) { emit('warn', 'Nhập text để embed'); return; }
        document.getElementById('vp-embed').innerHTML = spinnerHtml + ' Đang embed…';
        try {
          const r = await Z.vector.embed(text);
          // Backend response may be {embedding:[...]} or array directly
          const vec = r.embedding || r.vector || r.data || r;
          document.getElementById('vp-vector').value = JSON.stringify(vec);
          emit('ok', '✓ Embed xong (' + (Array.isArray(vec) ? vec.length : '?') + ' chiều)');
        } catch (e) {
          emit('err', 'Embed lỗi: ' + e.message);
        } finally {
          document.getElementById('vp-embed').innerHTML = '⚡ Embed text → vector';
        }
      };

      document.getElementById('vp-search').onclick = async () => {
        const raw = document.getElementById('vp-vector').value.trim();
        if (!raw) { emit('warn', 'Cần vector để search'); return; }
        let vec;
        try { vec = JSON.parse(raw); }
        catch { emit('err', 'Vector không phải JSON array hợp lệ'); return; }
        if (!Array.isArray(vec) || vec.length === 0) { emit('err', 'Vector phải là mảng số'); return; }
        const k = parseInt(kSlider.value, 10);
        if (k < 1 || k > 50) { emit('warn', 'k phải trong 1-50'); return; }
        let filter = null;
        const filterRaw = document.getElementById('vp-filter').value.trim();
        if (filterRaw) {
          try { filter = JSON.parse(filterRaw); } catch { emit('warn', 'Filter JSON sai cú pháp — bỏ qua'); }
        }
        const resBox = document.getElementById('vp-results');
        resBox.innerHTML = loadingBlock('Đang search top-' + k + '…');
        try {
          const r = await Z.vector.search(name, vec, k, filter);
          const rows = r.results || r.points || r;
          if (!rows || rows.length === 0) {
            resBox.innerHTML = emptyBlock('Không tìm thấy kết quả.');
            return;
          }
          resBox.innerHTML = `
            <table class="zeni-table">
              <thead><tr><th>#</th><th>ID</th><th>Distance</th><th>Metadata</th></tr></thead>
              <tbody>
                ${rows.map((row, i) => `
                  <tr>
                    <td class="mono">${i + 1}</td>
                    <td class="mono">${escHtml(row.id || '—')}</td>
                    <td class="mono" style="color:var(--aurora-cyan);">${(row.distance || row.score || 0).toFixed(4)}</td>
                    <td style="font-size:11px;color:var(--ink-300);font-family:var(--font-mono);">${escHtml(JSON.stringify(row.metadata || {}))}</td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          `;
        } catch (e) {
          resBox.innerHTML = errorBlock('Lỗi search: ' + e.message);
        }
      };
    }

    document.getElementById('vec-btn-new').onclick = openCreateModal;
    document.getElementById('vec-btn-refresh').onclick = loadList;
    loadList();
  }

  /* ═══════════════════════════════════════════════════════════════
     7. RENDER VIEW: CACHE & QUEUE
     ═══════════════════════════════════════════════════════════════ */
  async function renderCacheQueue() {
    setCrumb('Cache & Queue');
    const root = getContentRoot();
    if (!root) return;
    root.innerHTML = pageHeader(
      'Cache & Queue · Postgres-based',
      'Cache key-value (TTL) + Job queue (push/pull/ack) chạy trên Cloud SQL — không cần Redis riêng. Free 10K ops/tháng.',
      '#FDE68A'
    ) + `
      <div class="zeni-tabs">
        <div class="zeni-tab active" data-subtab="cache">Cache</div>
        <div class="zeni-tab" data-subtab="queue">Queue</div>
      </div>
      <div id="cq-body"></div>
    `;

    function bindTabs() {
      root.querySelectorAll('.zeni-tab').forEach(t => {
        t.onclick = () => {
          root.querySelectorAll('.zeni-tab').forEach(x => x.classList.remove('active'));
          t.classList.add('active');
          if (t.dataset.subtab === 'cache') renderCacheTab();
          else renderQueueTab();
        };
      });
    }

    async function renderCacheTab() {
      const body = document.getElementById('cq-body');
      body.innerHTML = `
        <div class="ext-grid-2">
          <div class="card">
            <div class="card-head"><div class="card-title"><span>Set / Get / Delete</span></div></div>
            <div class="ext-form-row">
              <label class="ext-form-label">Key</label>
              <input class="ext-form-input" id="cc-key" placeholder="vd: user_session_42" />
            </div>
            <div class="ext-form-row">
              <label class="ext-form-label">Value (JSON)</label>
              <textarea class="ext-form-textarea" id="cc-value" placeholder='{"name":"Tuấn","ts":1234567890}'></textarea>
            </div>
            <div class="ext-form-row">
              <label class="ext-form-label">TTL</label>
              <select class="ext-form-select" id="cc-ttl">
                <option value="60">1 phút</option>
                <option value="300">5 phút</option>
                <option value="3600" selected>1 giờ</option>
                <option value="86400">1 ngày</option>
                <option value="2592000">30 ngày</option>
                <option value="">Không hạn (persist)</option>
              </select>
            </div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;">
              <button class="btn btn-sm btn-accent" id="cc-set">💾 Set</button>
              <button class="btn btn-sm btn-ghost" id="cc-get">↩ Get</button>
              <button class="btn btn-sm btn-danger" id="cc-del">✕ Delete</button>
            </div>
            <div id="cc-result" style="margin-top:14px;"></div>
          </div>
          <div class="card">
            <div class="card-head">
              <div class="card-title"><span>Keys gần nhất</span></div>
              <div class="card-actions"><button class="btn btn-sm btn-ghost" id="cc-list-refresh">Tải lại</button></div>
            </div>
            <div id="cc-list">${loadingBlock('Đang tải keys…')}</div>
          </div>
        </div>
      `;

      async function loadKeys() {
        try {
          const list = await Z.cache.list();
          const items = list.keys || list;
          if (!items || items.length === 0) {
            document.getElementById('cc-list').innerHTML = emptyBlock('Chưa có key nào trong cache.');
            return;
          }
          document.getElementById('cc-list').innerHTML = `
            <table class="zeni-table">
              <thead><tr><th>Key</th><th>Size</th><th>Hết hạn</th></tr></thead>
              <tbody>
                ${items.slice(0, 50).map(k => `
                  <tr>
                    <td class="mono" style="color:var(--aurora-cyan);">${escHtml(k.key || k)}</td>
                    <td class="mono">${k.size_bytes ? (k.size_bytes + 'B') : '—'}</td>
                    <td style="color:var(--ink-400);font-size:11px;">${k.expires_at ? timeAgo(k.expires_at) : '∞'}</td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          `;
        } catch (e) {
          document.getElementById('cc-list').innerHTML = errorBlock('Lỗi: ' + e.message);
        }
      }

      document.getElementById('cc-set').onclick = async () => {
        const key = document.getElementById('cc-key').value.trim();
        const valRaw = document.getElementById('cc-value').value.trim();
        const ttl = document.getElementById('cc-ttl').value;
        if (!key) { emit('warn', 'Nhập key'); return; }
        let val;
        try { val = valRaw ? JSON.parse(valRaw) : null; }
        catch { emit('err', 'Value JSON sai cú pháp'); return; }
        try {
          await Z.cache.set(key, val, ttl ? parseInt(ttl, 10) : null);
          emit('ok', '✓ Đã set ' + key);
          loadKeys();
        } catch (e) { emit('err', 'Lỗi set: ' + e.message); }
      };

      document.getElementById('cc-get').onclick = async () => {
        const key = document.getElementById('cc-key').value.trim();
        if (!key) { emit('warn', 'Nhập key'); return; }
        try {
          const v = await Z.cache.get(key);
          document.getElementById('cc-result').innerHTML = `
            <div style="font-family:var(--font-mono);font-size:11px;color:var(--ink-400);margin-bottom:6px;">VALUE</div>
            <pre style="background:rgba(10,5,32,0.6);border:1px solid var(--border);border-radius:8px;padding:12px;color:var(--aurora-cyan);font-size:12px;white-space:pre-wrap;word-break:break-all;">${escHtml(JSON.stringify(v, null, 2))}</pre>
          `;
        } catch (e) {
          document.getElementById('cc-result').innerHTML = errorBlock(e.status === 404 ? 'Key không tồn tại hoặc đã hết hạn' : ('Lỗi get: ' + e.message));
        }
      };

      document.getElementById('cc-del').onclick = async () => {
        const key = document.getElementById('cc-key').value.trim();
        if (!key) { emit('warn', 'Nhập key'); return; }
        const ok = await confirmDialog('Xoá key?', 'Sẽ xoá key "' + key + '" khỏi cache.');
        if (!ok) return;
        try { await Z.cache.del(key); emit('ok', '✓ Đã xoá'); loadKeys(); }
        catch (e) { emit('err', 'Lỗi xoá: ' + e.message); }
      };

      document.getElementById('cc-list-refresh').onclick = loadKeys;
      loadKeys();
    }

    async function renderQueueTab() {
      const body = document.getElementById('cq-body');
      body.innerHTML = `
        <div class="ext-form-row" style="display:flex;gap:10px;align-items:end;">
          <div style="flex:1;">
            <label class="ext-form-label">Queue name</label>
            <input class="ext-form-input" id="qq-name" placeholder="vd: send_email" value="default" />
          </div>
          <button class="btn btn-md btn-ghost" id="qq-load">Tải stats</button>
        </div>
        <div id="qq-stats" class="ext-grid-3" style="margin-bottom:18px;"></div>
        <div class="ext-grid-2">
          <div class="card">
            <div class="card-head"><div class="card-title"><span>Push job</span></div></div>
            <div class="ext-form-row">
              <label class="ext-form-label">Payload (JSON)</label>
              <textarea class="ext-form-textarea" id="qq-payload" placeholder='{"to":"a@b.com","subject":"hi"}'></textarea>
            </div>
            <div class="ext-form-row">
              <label class="ext-form-label">Delay (giây, optional)</label>
              <input class="ext-form-input" id="qq-delay" placeholder="0" />
            </div>
            <button class="btn btn-md btn-accent" id="qq-push">⬆ Push</button>
          </div>
          <div class="card">
            <div class="card-head"><div class="card-title"><span>Pull / Ack</span></div></div>
            <button class="btn btn-md btn-ghost" id="qq-pull">⬇ Pull (lease 60s)</button>
            <div id="qq-pulled" style="margin-top:14px;"></div>
          </div>
        </div>
      `;

      async function loadStats() {
        const name = document.getElementById('qq-name').value.trim() || 'default';
        const target = document.getElementById('qq-stats');
        target.innerHTML = '<div style="grid-column:1/-1;">' + loadingBlock('Đang tải stats…') + '</div>';
        try {
          const s = await Z.queue.stats(name);
          target.innerHTML = `
            <div class="ext-stat-card"><div class="ext-stat-label">Pending</div><div class="ext-stat-value" style="color:var(--crown-gold);">${fmtNum(s.pending || 0)}</div></div>
            <div class="ext-stat-card"><div class="ext-stat-label">In flight (leased)</div><div class="ext-stat-value" style="color:var(--ajna-light);">${fmtNum(s.leased || s.in_flight || 0)}</div></div>
            <div class="ext-stat-card"><div class="ext-stat-label">Completed</div><div class="ext-stat-value" style="color:var(--aurora-cyan);">${fmtNum(s.completed || 0)}</div></div>
          `;
        } catch (e) {
          target.innerHTML = '<div style="grid-column:1/-1;">' + errorBlock('Lỗi: ' + e.message) + '</div>';
        }
      }

      document.getElementById('qq-load').onclick = loadStats;
      document.getElementById('qq-push').onclick = async () => {
        const name = document.getElementById('qq-name').value.trim() || 'default';
        const payloadRaw = document.getElementById('qq-payload').value.trim();
        const delayRaw = document.getElementById('qq-delay').value.trim();
        if (!payloadRaw) { emit('warn', 'Nhập payload'); return; }
        let payload;
        try { payload = JSON.parse(payloadRaw); } catch { emit('err', 'Payload JSON sai cú pháp'); return; }
        const delay = delayRaw ? parseInt(delayRaw, 10) : 0;
        try {
          const r = await Z.queue.push(name, payload, delay);
          emit('ok', '✓ Đã push job #' + (r.id || r.job_id || '?'));
          loadStats();
        } catch (e) { emit('err', 'Lỗi push: ' + e.message); }
      };
      document.getElementById('qq-pull').onclick = async () => {
        const name = document.getElementById('qq-name').value.trim() || 'default';
        const target = document.getElementById('qq-pulled');
        target.innerHTML = loadingBlock('Đang pull…');
        try {
          const job = await Z.queue.pull(name, 60);
          if (!job || !job.id) {
            target.innerHTML = emptyBlock('Queue trống — chưa có job pending.');
            return;
          }
          target.innerHTML = `
            <div class="ext-card">
              <div style="font-family:var(--font-mono);font-size:11px;color:var(--crown-gold);">Job #${job.id}</div>
              <pre style="margin-top:8px;background:rgba(10,5,32,0.6);border:1px solid var(--border);border-radius:8px;padding:10px;color:var(--aurora-cyan);font-size:11px;white-space:pre-wrap;">${escHtml(JSON.stringify(job.payload || {}, null, 2))}</pre>
              <div style="display:flex;gap:8px;margin-top:10px;">
                <button class="btn btn-sm btn-success" id="qq-ack-ok">✓ Ack thành công</button>
                <button class="btn btn-sm btn-danger" id="qq-ack-fail">✗ Ack thất bại</button>
              </div>
            </div>
          `;
          document.getElementById('qq-ack-ok').onclick = async () => {
            try { await Z.queue.ack(name, job.id, true); emit('ok', '✓ Đã ack'); target.innerHTML = ''; loadStats(); }
            catch (e) { emit('err', 'Lỗi ack: ' + e.message); }
          };
          document.getElementById('qq-ack-fail').onclick = async () => {
            try { await Z.queue.ack(name, job.id, false, 'manual fail'); emit('warn', 'Đã đánh dấu fail'); target.innerHTML = ''; loadStats(); }
            catch (e) { emit('err', 'Lỗi ack: ' + e.message); }
          };
        } catch (e) {
          target.innerHTML = errorBlock('Lỗi pull: ' + e.message);
        }
      };
      loadStats();
    }

    bindTabs();
    renderCacheTab();
  }

  /* ═══════════════════════════════════════════════════════════════
     8. RENDER VIEW: OCR & TRANSLATE
     ═══════════════════════════════════════════════════════════════ */
  async function renderOcrTranslate() {
    setCrumb('OCR & Dịch thuật');
    const root = getContentRoot();
    if (!root) return;
    root.innerHTML = pageHeader(
      'OCR & Translate · Cloud Vision + Translation',
      'Trích xuất văn bản từ ảnh/PDF (OCR) và dịch tự động giữa các ngôn ngữ. Tích hợp Google Cloud Vision API + Translation API.',
      '#A5B4FC'
    ) + `
      <div class="zeni-tabs">
        <div class="zeni-tab active" data-subtab="ocr">OCR</div>
        <div class="zeni-tab" data-subtab="translate">Dịch thuật</div>
      </div>
      <div id="ot-body"></div>
    `;

    function bindTabs() {
      root.querySelectorAll('.zeni-tab').forEach(t => {
        t.onclick = () => {
          root.querySelectorAll('.zeni-tab').forEach(x => x.classList.remove('active'));
          t.classList.add('active');
          if (t.dataset.subtab === 'ocr') renderOcrTab();
          else renderTranslateTab();
        };
      });
    }

    function renderOcrTab() {
      const body = document.getElementById('ot-body');
      body.innerHTML = `
        <div class="card">
          <div class="card-head"><div class="card-title"><span>Trích xuất văn bản</span></div></div>
          <div class="ext-grid-2">
            <div>
              <label class="ext-form-label">Tải ảnh / PDF</label>
              <label class="ext-drop" id="ocr-drop">
                <div style="font-size:24px;">📄</div>
                <div style="margin-top:8px;font-weight:600;color:var(--ink-200);">Kéo thả ảnh PNG/JPG hoặc PDF (≤5 trang)</div>
                <div style="font-size:11px;margin-top:6px;color:var(--ink-400);">Hoặc bấm để chọn file</div>
                <input type="file" id="ocr-file" accept="image/*,application/pdf" />
              </label>
              <div class="ext-form-row" style="margin-top:14px;">
                <label class="ext-form-label">Hoặc nhập GCS URI / URL công khai</label>
                <input class="ext-form-input" id="ocr-uri" placeholder="gs://bucket/file.png  hoặc  https://example.com/img.jpg" />
              </div>
              <button class="btn btn-md btn-accent" id="ocr-submit" style="margin-top:8px;">⚡ Trích xuất văn bản</button>
              <span class="cost-preview" style="margin-left:10px;" id="ocr-cost">~ ${fmtVnd(38)}/trang</span>
            </div>
            <div>
              <label class="ext-form-label">Văn bản trích xuất</label>
              <textarea class="ext-form-textarea" id="ocr-result" readonly placeholder="Kết quả OCR sẽ hiện ở đây…" style="min-height:240px;"></textarea>
              <button class="btn btn-sm btn-ghost" id="ocr-copy" style="margin-top:8px;">📋 Sao chép</button>
            </div>
          </div>
        </div>
      `;

      const drop = document.getElementById('ocr-drop');
      const fileInput = document.getElementById('ocr-file');
      let pickedFile = null;

      ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('dragover'); }));
      ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('dragover'); }));
      drop.addEventListener('drop', (e) => { e.preventDefault(); if (e.dataTransfer.files[0]) { pickedFile = e.dataTransfer.files[0]; drop.querySelector('div:nth-child(2)').textContent = '✓ ' + pickedFile.name; } });
      fileInput.addEventListener('change', () => { pickedFile = fileInput.files[0]; if (pickedFile) drop.querySelector('div:nth-child(2)').textContent = '✓ ' + pickedFile.name; });

      document.getElementById('ocr-submit').onclick = async () => {
        const uri = document.getElementById('ocr-uri').value.trim();
        const result = document.getElementById('ocr-result');
        const submitBtn = document.getElementById('ocr-submit');
        submitBtn.disabled = true;
        submitBtn.innerHTML = spinnerHtml + ' Đang OCR…';
        result.value = '';
        try {
          let body;
          if (uri) {
            if (uri.startsWith('gs://')) body = { gcs_uri: uri };
            else body = { image_url: uri };
          } else if (pickedFile) {
            const b64 = await new Promise((res, rej) => {
              const r = new FileReader();
              r.onload = () => res(r.result.split(',')[1]);
              r.onerror = rej;
              r.readAsDataURL(pickedFile);
            });
            body = { image_base64: b64 };
          } else {
            emit('warn', 'Cần upload file hoặc nhập URI');
            return;
          }
          const isPdf = (pickedFile && pickedFile.type === 'application/pdf') || (uri && uri.toLowerCase().endsWith('.pdf'));
          const r = isPdf ? await Z.ocr.pdf(body) : await Z.ocr.image(body);
          const text = r.text || (r.pages ? r.pages.map(p => p.text || '').join('\n\n--- TRANG ---\n\n') : JSON.stringify(r));
          result.value = text;
          emit('ok', '✓ OCR xong (' + text.length + ' ký tự)');
        } catch (e) {
          emit('err', 'Lỗi OCR: ' + e.message);
        } finally {
          submitBtn.disabled = false;
          submitBtn.innerHTML = '⚡ Trích xuất văn bản';
        }
      };

      document.getElementById('ocr-copy').onclick = () => {
        const t = document.getElementById('ocr-result').value;
        if (!t) { emit('warn', 'Chưa có nội dung để copy'); return; }
        navigator.clipboard.writeText(t).then(() => emit('ok', '✓ Đã copy'));
      };
    }

    function renderTranslateTab() {
      const body = document.getElementById('ot-body');
      body.innerHTML = `
        <div class="card">
          <div class="card-head"><div class="card-title"><span>Dịch văn bản</span></div></div>
          <div class="ext-grid-2">
            <div>
              <label class="ext-form-label">Văn bản nguồn (max 5000 ký tự)</label>
              <textarea class="ext-form-textarea" id="tr-text" maxlength="5000" placeholder="Nhập văn bản cần dịch…" style="min-height:180px;"></textarea>
              <div class="ext-counter" id="tr-counter">0 / 5000</div>
              <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px;">
                <div>
                  <label class="ext-form-label">Ngôn ngữ nguồn</label>
                  <select class="ext-form-select" id="tr-source">
                    <option value="">Tự nhận diện</option>
                    <option value="en" selected>English (en)</option>
                    <option value="vi">Tiếng Việt (vi)</option>
                    <option value="zh">中文 (zh)</option>
                    <option value="ja">日本語 (ja)</option>
                    <option value="ko">한국어 (ko)</option>
                    <option value="fr">Français (fr)</option>
                    <option value="de">Deutsch (de)</option>
                  </select>
                </div>
                <div>
                  <label class="ext-form-label">Ngôn ngữ đích</label>
                  <select class="ext-form-select" id="tr-target">
                    <option value="vi" selected>Tiếng Việt (vi)</option>
                    <option value="en">English (en)</option>
                    <option value="zh">中文 (zh)</option>
                    <option value="ja">日本語 (ja)</option>
                    <option value="ko">한국어 (ko)</option>
                    <option value="fr">Français (fr)</option>
                    <option value="de">Deutsch (de)</option>
                  </select>
                </div>
              </div>
              <button class="btn btn-md btn-accent" id="tr-submit" style="margin-top:14px;">🔄 Dịch ngay</button>
              <span class="cost-preview" style="margin-left:10px;" id="tr-cost">~ ${fmtVnd(0)}</span>
            </div>
            <div>
              <label class="ext-form-label">Bản dịch</label>
              <textarea class="ext-form-textarea" id="tr-result" readonly placeholder="Kết quả dịch sẽ hiện ở đây…" style="min-height:240px;"></textarea>
              <div id="tr-meta" style="margin-top:8px;font-size:11px;color:var(--ink-400);"></div>
            </div>
          </div>
        </div>
      `;
      const txt = document.getElementById('tr-text');
      const counter = document.getElementById('tr-counter');
      const cost = document.getElementById('tr-cost');
      const updateCost = () => {
        const len = txt.value.length;
        counter.textContent = len + ' / 5000';
        // $20/1M chars → 1 char = 0.00002 USD ~ 0.5đ
        const vnd = Math.ceil(len * 0.5);
        cost.textContent = '~ ' + fmtVnd(vnd);
      };
      txt.addEventListener('input', updateCost);
      updateCost();

      document.getElementById('tr-submit').onclick = async () => {
        const text = txt.value.trim();
        const target = document.getElementById('tr-target').value;
        const source = document.getElementById('tr-source').value;
        if (!text) { emit('warn', 'Nhập văn bản để dịch'); return; }
        const submitBtn = document.getElementById('tr-submit');
        submitBtn.disabled = true;
        submitBtn.innerHTML = spinnerHtml + ' Đang dịch…';
        try {
          const body = { text, target_lang: target };
          if (source) body.source_lang = source;
          const r = await Z.translate.text(body);
          document.getElementById('tr-result').value = r.translated_text || r.translation || JSON.stringify(r);
          document.getElementById('tr-meta').textContent =
            'Phát hiện: ' + (r.source_lang_detected || source || 'auto') +
            ' · ' + (r.char_count || text.length) + ' ký tự';
          emit('ok', '✓ Dịch xong');
        } catch (e) {
          emit('err', 'Lỗi dịch: ' + e.message);
        } finally {
          submitBtn.disabled = false;
          submitBtn.innerHTML = '🔄 Dịch ngay';
        }
      };
    }

    bindTabs();
    renderOcrTab();
  }

  /* ═══════════════════════════════════════════════════════════════
     9. RENDER VIEW: SMS & SLACK
     ═══════════════════════════════════════════════════════════════ */
  async function renderSmsSlack() {
    setCrumb('SMS & Slack');
    const root = getContentRoot();
    if (!root) return;
    root.innerHTML = pageHeader(
      'SMS & Slack · Notifications',
      'Gửi SMS qua Stringee (số VN) hoặc Twilio (quốc tế). Gửi tin nhắn Slack qua webhook hoặc Bot API. Lịch sử 10 tin gần nhất hiện ở dưới.',
      '#22D3EE'
    ) + `
      <div class="ext-grid-2">
        <div class="card">
          <div class="card-head"><div class="card-title"><span>📱 Gửi SMS</span></div></div>
          <div class="ext-form-row">
            <label class="ext-form-label">Số điện thoại</label>
            <input class="ext-form-input" id="sms-to" placeholder="0901234567 hoặc +84901234567 hoặc +1..." />
            <div class="ext-hint">Số bắt đầu 0 hoặc +84 → Stringee · Khác → Twilio</div>
          </div>
          <div class="ext-form-row">
            <label class="ext-form-label">Nội dung (max 160 ký tự / segment)</label>
            <textarea class="ext-form-textarea" id="sms-text" maxlength="640" placeholder="Tin nhắn ngắn gọn dưới 160 ký tự để tối ưu chi phí…"></textarea>
            <div class="ext-counter" id="sms-counter">0 / 160 (1 segment)</div>
          </div>
          <button class="btn btn-md btn-accent" id="sms-send">✉ Gửi SMS</button>
          <span class="cost-preview" style="margin-left:10px;" id="sms-cost">~ ${fmtVnd(0)}</span>
        </div>
        <div class="card">
          <div class="card-head"><div class="card-title"><span>💬 Gửi Slack</span></div></div>
          <div class="ext-form-row">
            <label class="ext-form-label">Chế độ</label>
            <div style="display:flex;gap:14px;">
              <label style="font-size:13px;color:var(--ink-100);"><input type="radio" name="slack-mode" value="webhook" checked /> Webhook URL</label>
              <label style="font-size:13px;color:var(--ink-100);"><input type="radio" name="slack-mode" value="bot" /> Bot Token + Channel</label>
            </div>
          </div>
          <div class="ext-form-row" id="slack-webhook-row">
            <label class="ext-form-label">Webhook URL</label>
            <input class="ext-form-input" id="slack-webhook" placeholder="https://hooks.slack.com/services/T0.../B0.../..." />
          </div>
          <div class="ext-form-row" id="slack-bot-row" style="display:none;">
            <label class="ext-form-label">Bot Token</label>
            <input class="ext-form-input" id="slack-token" placeholder="xoxb-..." />
            <label class="ext-form-label" style="margin-top:10px;">Channel</label>
            <input class="ext-form-input" id="slack-channel" placeholder="#general hoặc C12345" />
          </div>
          <div class="ext-form-row">
            <label class="ext-form-label">Nội dung</label>
            <textarea class="ext-form-textarea" id="slack-text" placeholder="Hello from Zeni Cloud :wave:"></textarea>
          </div>
          <button class="btn btn-md btn-accent" id="slack-send">✉ Gửi Slack</button>
        </div>
      </div>
      <div class="card" style="margin-top:14px;">
        <div class="card-head"><div class="card-title"><span>📜 Lịch sử 10 tin gần nhất</span></div></div>
        <div id="ss-history">${emptyBlock('Chưa có tin nhắn nào trong session này.')}</div>
      </div>
    `;

    // SMS counter + cost
    const smsText = document.getElementById('sms-text');
    const smsCount = document.getElementById('sms-counter');
    const smsCost = document.getElementById('sms-cost');
    const smsTo = document.getElementById('sms-to');
    function updateSms() {
      const len = smsText.value.length;
      const seg = Math.max(1, Math.ceil(len / 160));
      smsCount.textContent = len + ' / 160 (' + seg + ' segment' + (seg > 1 ? 's' : '') + ')';
      const to = smsTo.value.trim();
      const isVn = /^(\+84|0)/.test(to);
      const perSeg = isVn ? 250 : 1200; // ~250đ Stringee / ~1200đ Twilio
      smsCost.textContent = '~ ' + fmtVnd(seg * perSeg) + (isVn ? ' (Stringee)' : ' (Twilio)');
    }
    smsText.addEventListener('input', updateSms);
    smsTo.addEventListener('input', updateSms);
    updateSms();

    // Slack mode toggle
    document.querySelectorAll('input[name=slack-mode]').forEach(r => {
      r.addEventListener('change', () => {
        const mode = r.value;
        document.getElementById('slack-webhook-row').style.display = mode === 'webhook' ? '' : 'none';
        document.getElementById('slack-bot-row').style.display = mode === 'bot' ? '' : 'none';
      });
    });

    // History store (in-memory cho session)
    const history = [];
    function pushHistory(item) {
      history.unshift(item);
      if (history.length > 10) history.length = 10;
      const target = document.getElementById('ss-history');
      target.innerHTML = history.map(h => `
        <div class="ext-history-row${h.success ? '' : ' err'}">
          <div style="display:flex;gap:10px;align-items:center;flex:1;min-width:0;">
            <span class="pill ${h.success ? 'pill-ok' : 'pill-err'}">${escHtml(h.kind)}</span>
            <span class="mono" style="color:var(--ink-200);">${escHtml(h.to || '—')}</span>
            <span style="color:var(--ink-400);font-size:11px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escHtml((h.text || '').slice(0, 80))}</span>
          </div>
          <span style="color:var(--ink-500);font-size:11px;">${timeAgo(h.ts)}</span>
        </div>
      `).join('');
    }

    document.getElementById('sms-send').onclick = async () => {
      const to = smsTo.value.trim();
      const text = smsText.value.trim();
      if (!to || !text) { emit('warn', 'Nhập số điện thoại và nội dung'); return; }
      if (!/^[+0-9]{8,16}$/.test(to)) { emit('warn', 'Số điện thoại không hợp lệ'); return; }
      const ok = await confirmDialog('Gửi SMS?', 'Sẽ gửi đến ' + to + '. Chi phí dự kiến: ' + smsCost.textContent.replace('~ ', ''));
      if (!ok) return;
      try {
        const r = await Z.sms.send({ to, text });
        emit('ok', '✓ Đã gửi SMS · ' + (r.provider || ''));
        pushHistory({ kind: 'SMS', to, text, success: true, ts: new Date().toISOString() });
      } catch (e) {
        emit('err', 'Lỗi gửi: ' + e.message);
        pushHistory({ kind: 'SMS', to, text, success: false, ts: new Date().toISOString() });
      }
    };

    document.getElementById('slack-send').onclick = async () => {
      const mode = document.querySelector('input[name=slack-mode]:checked').value;
      const text = document.getElementById('slack-text').value.trim();
      if (!text) { emit('warn', 'Nhập nội dung'); return; }
      try {
        let r, summary;
        if (mode === 'webhook') {
          const url = document.getElementById('slack-webhook').value.trim();
          if (!url || !url.startsWith('https://hooks.slack.com/')) { emit('warn', 'Webhook URL không hợp lệ'); return; }
          r = await Z.slack.webhook({ webhook_url: url, text });
          summary = 'webhook';
        } else {
          const token = document.getElementById('slack-token').value.trim();
          const channel = document.getElementById('slack-channel').value.trim();
          if (!token || !channel) { emit('warn', 'Cần token + channel'); return; }
          r = await Z.slack.post({ token, channel, text });
          summary = channel;
        }
        emit('ok', '✓ Đã gửi Slack');
        pushHistory({ kind: 'Slack', to: summary, text, success: true, ts: new Date().toISOString() });
      } catch (e) {
        emit('err', 'Lỗi gửi: ' + e.message);
        pushHistory({ kind: 'Slack', to: 'failed', text, success: false, ts: new Date().toISOString() });
      }
    };
  }

  /* ═══════════════════════════════════════════════════════════════
     10. RENDER VIEW: PHÁP NHÂN & DOANH THU (admin)
     ═══════════════════════════════════════════════════════════════ */
  async function renderEntities() {
    setCrumb('Pháp nhân & Doanh thu');
    const root = getContentRoot();
    if (!root) return;
    const me = window.__ZENI_REAL_USER || (window.state && window.state.currentUser) || {};
    const isAdmin = me.role === 'Owner' || me.role === 'Admin' || me.is_admin === true;

    root.innerHTML = pageHeader(
      'Pháp nhân & Doanh thu · Multi-Entity',
      'Tổng hợp doanh thu theo từng pháp nhân (Zeni Holdings + 5 công ty con) và xử lý chuyển khoản nội bộ định kỳ.',
      '#FDE68A'
    ) + `
      <div class="card">
        <div class="card-head">
          <div class="card-title"><span>Danh sách pháp nhân</span></div>
          <div class="card-actions">
            <select class="ext-form-select" id="ent-period" style="width:auto;padding:8px 12px;font-size:12px;">
              <option value="last_30d">30 ngày qua</option>
              <option value="this_month" selected>Tháng này</option>
              <option value="last_month">Tháng trước</option>
            </select>
            <button class="btn btn-md btn-ghost" id="ent-refresh">Tải lại</button>
            ${isAdmin ? '<button class="btn btn-md btn-accent" id="ent-intercompany">▶ Chạy intercompany</button>' : ''}
          </div>
        </div>
        <div id="ent-cards">${loadingBlock('Đang tải pháp nhân…')}</div>
      </div>
      <div class="card" style="margin-top:14px;">
        <div class="card-head"><div class="card-title"><span>Doanh thu theo pháp nhân</span></div></div>
        <div id="ent-chart">${loadingBlock('Đang tổng hợp…')}</div>
      </div>
    `;

    function periodToYm() {
      const sel = document.getElementById('ent-period').value;
      const now = new Date();
      if (sel === 'this_month') return now.toISOString().slice(0, 7);
      if (sel === 'last_month') {
        const d = new Date(now.getFullYear(), now.getMonth() - 1, 15);
        return d.toISOString().slice(0, 7);
      }
      return null; // last_30d → backend mặc định
    }

    async function loadAll() {
      // Cards
      try {
        const list = await Z.legalEntities.list();
        const period = periodToYm();
        const rev = await Z.legalEntities.revenueByEntity(period).catch(() => ({ entries: [] }));
        const map = {};
        (rev.entries || rev || []).forEach(e => { map[e.legal_entity_id || e.id] = e; });
        const cards = (list || []).map(e => {
          const r = map[e.id] || {};
          const amt = r.revenue_vnd || r.amount_vnd || 0;
          return `
            <div class="ext-card" style="background:linear-gradient(135deg, rgba(${e.is_master ? '253,230,138' : '99,102,241'},0.08), rgba(168,85,247,0.04));">
              <div style="display:flex;justify-content:space-between;align-items:flex-start;">
                <div>
                  <div style="font-family:var(--font-mono);font-size:10px;text-transform:uppercase;letter-spacing:0.12em;color:${e.is_master ? 'var(--crown-gold)' : 'var(--ink-400)'};">${e.is_master ? 'HOLDINGS' : 'SUBSIDIARY'}</div>
                  <div style="font-size:18px;font-weight:700;color:var(--ink-50);margin-top:4px;">${escHtml(e.name)}</div>
                  <div class="mono" style="font-size:11px;color:var(--ink-400);margin-top:2px;">${escHtml(e.id)}</div>
                </div>
                <div style="text-align:right;">
                  <div style="font-family:var(--font-mono);font-size:9px;text-transform:uppercase;color:var(--ink-400);">Doanh thu</div>
                  <div style="font-size:22px;font-weight:800;color:${amt > 0 ? 'var(--aurora-cyan)' : 'var(--ink-500)'};">${fmtVnd(amt)}</div>
                </div>
              </div>
              ${e.parent_id ? `<div style="font-size:11px;color:var(--ink-400);margin-top:8px;">↳ thuộc ${escHtml(e.parent_id)}</div>` : ''}
              ${e.bank_account ? `<div class="mono" style="font-size:11px;color:var(--ink-400);margin-top:4px;">${escHtml(e.bank_account)}</div>` : ''}
            </div>
          `;
        }).join('');
        document.getElementById('ent-cards').innerHTML = cards
          ? `<div class="ext-grid-3">${cards}</div>`
          : emptyBlock('Chưa có pháp nhân nào.');

        // Chart bar
        const chartEntries = (rev.entries || rev || []).filter(e => (e.revenue_vnd || e.amount_vnd || 0) > 0);
        if (chartEntries.length === 0) {
          document.getElementById('ent-chart').innerHTML = emptyBlock('Chưa có doanh thu trong kỳ này.');
        } else {
          const max = Math.max(...chartEntries.map(e => e.revenue_vnd || e.amount_vnd || 0));
          document.getElementById('ent-chart').innerHTML = chartEntries.map(e => {
            const amt = e.revenue_vnd || e.amount_vnd || 0;
            const w = max ? (amt / max) * 100 : 0;
            return `
              <div class="ext-bar-row">
                <div class="ext-bar-label">${escHtml(e.entity_name || e.name || e.legal_entity_id || e.id)}</div>
                <div class="ext-bar-track"><div class="ext-bar-fill" style="width:${w}%;"></div></div>
                <div class="ext-bar-val">${fmtVnd(amt)}</div>
              </div>
            `;
          }).join('');
        }
      } catch (e) {
        document.getElementById('ent-cards').innerHTML = errorBlock('Lỗi tải pháp nhân: ' + e.message);
        document.getElementById('ent-chart').innerHTML = '';
      }
    }

    document.getElementById('ent-period').onchange = loadAll;
    document.getElementById('ent-refresh').onclick = loadAll;

    if (isAdmin) {
      document.getElementById('ent-intercompany').onclick = async () => {
        const period = periodToYm() || new Date().toISOString().slice(0, 7);
        const start = period + '-01';
        const endDate = new Date(period + '-01');
        endDate.setMonth(endDate.getMonth() + 1);
        endDate.setDate(0);
        const end = endDate.toISOString().slice(0, 10);
        const ok = await confirmDialog(
          'Chạy Intercompany Transfer?',
          'Sẽ tổng hợp & tạo các transfer record từ ' + start + ' đến ' + end + '. Hành động này ảnh hưởng đến ledger.'
        );
        if (!ok) return;
        try {
          const r = await Z.legalEntities.runIntercompany({ period_start: start, period_end: end });
          emit('ok', '✓ Đã tạo ' + (r.transfers || r.count || '?') + ' transfer record');
          loadAll();
        } catch (e) {
          emit('err', 'Lỗi: ' + e.message);
        }
      };
    }

    loadAll();
  }

  /* ═══════════════════════════════════════════════════════════════
     11. HOOK setView (giữ behavior cũ, thêm 5 view mới)
     ═══════════════════════════════════════════════════════════════ */
  const renderers = {
    vector: renderVector,
    'cache-queue': renderCacheQueue,
    'ocr-translate': renderOcrTranslate,
    'sms-slack': renderSmsSlack,
    entities: renderEntities,
  };

  const _origSetView = window.setView;
  window.setView = function (view) {
    if (renderers[view]) {
      // Đánh dấu nav-item active đúng
      window.state.currentView = view;
      document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.view === view));
      try { renderers[view](); }
      catch (e) {
        console.error('[zeni-ext] render', view, 'failed:', e);
        const root = getContentRoot();
        if (root) root.innerHTML = errorBlock('Render lỗi: ' + e.message);
      }
      return;
    }
    return _origSetView(view);
  };

  // Re-render khi workspace switch
  if (window.ZeniRealData && window.ZeniRealData.bootstrap) {
    const _origBoot = window.ZeniRealData.bootstrap;
    window.ZeniRealData.bootstrap = async function () {
      const r = await _origBoot();
      if (window.state && renderers[window.state.currentView]) {
        try { renderers[window.state.currentView](); } catch (e) {}
      }
      return r;
    };
  }

  log('extended modules loaded — vector, cache-queue, ocr-translate, sms-slack, entities');

  // expose for debugging
  window.ZeniExt = { renderers, openModal, closeModal };
})();
