/*
 * ZENI CLOUD CORE · PRIVACY MODULE
 *
 * Standalone, lazy-loaded module for the SPA Privacy Settings tab.
 *
 *   - Extends window.ZeniAPI with .privacy namespace (getPreferences,
 *     updatePreferences, listAdminAccessLog, approve/deny, listViolations,
 *     requestDataDelete, exportData).
 *   - Exposes window.ZeniPrivacyUI.render(rootEl, ws) that paints the entire
 *     Privacy tab into rootEl.
 *
 * Vanilla JS, no framework. Dark theme. Vietnamese strings.
 */
(function () {
  'use strict';

  if (!window.ZeniAPI) {
    console.warn('[zeni-privacy] ZeniAPI missing — abort');
    return;
  }

  const ACCESS_KEY = 'zeni.jwt.access';
  const API_BASE = (typeof window.ZENI_API_BASE === 'string' && window.ZENI_API_BASE) || '/api/v1';

  // ─── helpers ────────────────────────────────────────────
  function authHeaders() {
    return {
      'Authorization': 'Bearer ' + (localStorage.getItem(ACCESS_KEY) || ''),
      'Content-Type': 'application/json',
      'Accept': 'application/json',
    };
  }

  async function _req(path, opts) {
    opts = opts || {};
    const r = await fetch(API_BASE + path, {
      method: opts.method || 'GET',
      headers: authHeaders(),
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    });
    if (!r.ok) {
      const detail = await r.json().catch(() => ({ detail: r.statusText }));
      const err = new Error(typeof detail.detail === 'string' ? detail.detail : ('HTTP ' + r.status));
      err.status = r.status;
      throw err;
    }
    if (r.status === 204) return null;
    const ct = r.headers.get('content-type') || '';
    return ct.includes('application/json') ? r.json() : r.text();
  }

  function escHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function fmtDate(iso) {
    if (!iso) return '—';
    try {
      const d = new Date(iso);
      const pad = n => String(n).padStart(2, '0');
      return pad(d.getDate()) + '/' + pad(d.getMonth() + 1) + '/' + d.getFullYear() +
        ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
    } catch { return iso; }
  }

  function toast(msg, kind) {
    if (typeof window.toast === 'function') {
      try { window.toast(msg, kind || 'ok'); return; } catch {}
    }
    // Fallback inline toast
    let host = document.getElementById('zeni-priv-toast-host');
    if (!host) {
      host = document.createElement('div');
      host.id = 'zeni-priv-toast-host';
      host.style.cssText = 'position:fixed;top:20px;right:20px;z-index:99999;display:flex;flex-direction:column;gap:8px;';
      document.body.appendChild(host);
    }
    const el = document.createElement('div');
    const isErr = kind === 'err' || kind === 'error';
    el.style.cssText = 'padding:10px 14px;border-radius:8px;font-size:13px;font-weight:500;' +
      'background:' + (isErr ? 'rgba(248,113,113,0.18)' : 'rgba(34,211,238,0.16)') + ';' +
      'border:1px solid ' + (isErr ? 'rgba(248,113,113,0.45)' : 'rgba(34,211,238,0.45)') + ';' +
      'color:' + (isErr ? '#FCA5A5' : '#67E8F9') + ';' +
      'box-shadow:0 8px 28px rgba(0,0,0,0.4);';
    el.textContent = msg;
    host.appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity 0.3s'; }, 3000);
    setTimeout(() => el.remove(), 3400);
  }

  function downloadBlob(content, filename, mime) {
    const blob = new Blob([content], { type: mime || 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 200);
  }

  // ─── ZeniAPI.privacy namespace ─────────────────────────
  const privacyApi = {
    getPreferences(ws) {
      return _req('/privacy/preferences?ws=' + encodeURIComponent(ws));
    },
    updatePreferences(ws, prefs) {
      return _req('/privacy/preferences?ws=' + encodeURIComponent(ws),
        { method: 'POST', body: prefs });
    },
    listAdminAccessLog(ws) {
      return _req('/privacy/admin-access-log?ws=' + encodeURIComponent(ws));
    },
    approveAdminAccess(requestId) {
      return _req('/privacy/admin-access-log/' + encodeURIComponent(requestId) + '/approve',
        { method: 'POST' });
    },
    denyAdminAccess(requestId) {
      return _req('/privacy/admin-access-log/' + encodeURIComponent(requestId) + '/deny',
        { method: 'POST' });
    },
    listViolations(ws) {
      return _req('/privacy/violations?ws=' + encodeURIComponent(ws));
    },
    requestDataDelete(ws) {
      return _req('/privacy/delete-request?ws=' + encodeURIComponent(ws),
        { method: 'POST' });
    },
    exportData(ws) {
      return _req('/privacy/export?ws=' + encodeURIComponent(ws));
    },
  };

  window.ZeniAPI.privacy = privacyApi;

  // ─── inline styles (scoped) ────────────────────────────
  const STYLE_ID = 'zeni-privacy-styles';
  function injectStyles() {
    if (document.getElementById(STYLE_ID)) return;
    const css = `
      .zp-root { font-family: 'Inter', system-ui, -apple-system, sans-serif; color:#e5e7eb; padding:24px 16px 80px; max-width:980px; margin:0 auto; }
      .zp-h1 { font-size:22px; font-weight:800; letter-spacing:-0.02em; margin:0 0 6px; color:#fde68a; }
      .zp-sub { font-size:13px; color:#9CA3AF; margin:0 0 28px; line-height:1.5; }
      .zp-card { background:rgba(10,5,32,0.78); border:1px solid rgba(168,139,250,0.20); border-radius:14px; padding:20px 22px; margin-bottom:18px; backdrop-filter:blur(10px); }
      .zp-card-title { font-size:13px; font-weight:700; letter-spacing:0.08em; text-transform:uppercase; color:#fde68a; margin:0 0 8px; }
      .zp-card-desc { font-size:13px; color:#C4B5FD; margin:0 0 14px; line-height:1.55; }
      .zp-toggle-row { display:flex; gap:14px; align-items:flex-start; padding:14px 0; border-top:1px solid rgba(168,139,250,0.10); }
      .zp-toggle-row:first-of-type { border-top:0; padding-top:6px; }
      .zp-toggle-info { flex:1; min-width:0; }
      .zp-toggle-label { font-size:14px; font-weight:600; color:#EDE9FE; margin-bottom:3px; }
      .zp-toggle-hint { font-size:12px; color:#9E8BE5; line-height:1.4; }
      .zp-switch { position:relative; width:46px; height:26px; flex-shrink:0; }
      .zp-switch input { opacity:0; width:0; height:0; }
      .zp-switch span { position:absolute; inset:0; background:rgba(168,139,250,0.20); border-radius:13px; cursor:pointer; transition:0.2s; }
      .zp-switch span::before { content:''; position:absolute; left:3px; top:3px; width:20px; height:20px; border-radius:50%; background:#fff; transition:0.2s; }
      .zp-switch input:checked + span { background:linear-gradient(135deg,#fde68a,#F59E0B); }
      .zp-switch input:checked + span::before { transform:translateX(20px); background:#1a0938; }
      .zp-radio-row { display:flex; gap:10px; align-items:center; padding:10px 12px; border-radius:10px; cursor:pointer; border:1px solid rgba(168,139,250,0.15); margin-bottom:8px; transition:0.15s; }
      .zp-radio-row:hover { border-color:rgba(253,230,138,0.45); background:rgba(253,230,138,0.04); }
      .zp-radio-row input[type=radio] { width:16px; height:16px; accent-color:#fde68a; }
      .zp-radio-row.active { border-color:#fde68a; background:rgba(253,230,138,0.06); }
      .zp-radio-label { font-size:13px; color:#EDE9FE; font-weight:600; }
      .zp-radio-desc { font-size:11.5px; color:#9E8BE5; margin-top:2px; }
      .zp-btn { padding:10px 18px; border-radius:10px; border:0; cursor:pointer; font-size:13px; font-weight:700; letter-spacing:0.02em; transition:0.15s; font-family:inherit; }
      .zp-btn-primary { background:linear-gradient(135deg,#fde68a,#F59E0B); color:#1a0938; }
      .zp-btn-primary:hover { transform:translateY(-1px); box-shadow:0 6px 20px rgba(253,230,138,0.3); }
      .zp-btn-primary:disabled { opacity:0.55; cursor:wait; transform:none; box-shadow:none; }
      .zp-btn-ghost { background:rgba(255,255,255,0.06); border:1px solid rgba(168,139,250,0.20); color:#EDE9FE; }
      .zp-btn-ghost:hover { border-color:#fde68a; background:rgba(253,230,138,0.08); }
      .zp-btn-danger { background:rgba(248,113,113,0.12); border:1px solid rgba(248,113,113,0.45); color:#FCA5A5; }
      .zp-btn-danger:hover { background:rgba(248,113,113,0.20); }
      .zp-btn-ok { background:rgba(34,211,238,0.14); border:1px solid rgba(34,211,238,0.45); color:#67E8F9; padding:6px 12px; font-size:12px; }
      .zp-btn-no { background:rgba(248,113,113,0.10); border:1px solid rgba(248,113,113,0.35); color:#FCA5A5; padding:6px 12px; font-size:12px; }
      .zp-tier { display:inline-flex; align-items:center; gap:8px; padding:8px 14px; background:rgba(34,211,238,0.10); border:1px solid rgba(34,211,238,0.30); border-radius:10px; font-size:13px; color:#67E8F9; font-weight:600; }
      .zp-tier-num { background:rgba(34,211,238,0.20); padding:2px 8px; border-radius:6px; font-family:monospace; }
      .zp-table { width:100%; border-collapse:collapse; font-size:12px; }
      .zp-table th { text-align:left; padding:8px 6px; color:#9E8BE5; font-weight:600; font-size:11px; letter-spacing:0.05em; text-transform:uppercase; border-bottom:1px solid rgba(168,139,250,0.18); }
      .zp-table td { padding:10px 6px; border-bottom:1px solid rgba(168,139,250,0.08); color:#EDE9FE; vertical-align:top; }
      .zp-status { padding:2px 8px; border-radius:5px; font-size:10.5px; font-weight:700; text-transform:uppercase; letter-spacing:0.04em; }
      .zp-status-pending { background:rgba(253,230,138,0.16); color:#fde68a; }
      .zp-status-approved { background:rgba(34,211,238,0.14); color:#67E8F9; }
      .zp-status-denied { background:rgba(248,113,113,0.14); color:#FCA5A5; }
      .zp-status-blocked { background:rgba(248,113,113,0.14); color:#FCA5A5; }
      .zp-status-redacted { background:rgba(253,230,138,0.16); color:#fde68a; }
      .zp-empty { padding:18px; text-align:center; color:#7C6BB0; font-size:13px; font-style:italic; }
      .zp-loading { padding:18px; text-align:center; color:#9E8BE5; font-size:12px; }
      .zp-action-row { display:flex; gap:10px; flex-wrap:wrap; margin-top:10px; }
      .zp-modal-bg { position:fixed; inset:0; background:rgba(3,0,20,0.78); backdrop-filter:blur(6px); display:flex; align-items:center; justify-content:center; z-index:99998; padding:16px; }
      .zp-modal { background:#08051F; border:1px solid rgba(248,113,113,0.45); border-radius:16px; padding:28px 26px; max-width:440px; width:100%; box-shadow:0 24px 80px rgba(0,0,0,0.6); }
      .zp-modal-h { font-size:18px; font-weight:800; color:#FCA5A5; margin:0 0 10px; }
      .zp-modal-p { font-size:13px; color:#EDE9FE; line-height:1.5; margin:0 0 18px; }
      .zp-modal-actions { display:flex; gap:10px; justify-content:flex-end; flex-wrap:wrap; }
      .zp-sev-low { color:#9E8BE5; }
      .zp-sev-med { color:#fde68a; }
      .zp-sev-high { color:#FCA5A5; font-weight:700; }
      @media (max-width: 540px) {
        .zp-root { padding:16px 12px 80px; }
        .zp-h1 { font-size:18px; }
        .zp-card { padding:16px 14px; }
        .zp-toggle-row { flex-direction:row; align-items:center; }
        .zp-table th, .zp-table td { padding:6px 4px; font-size:11px; }
        .zp-table th:nth-child(n+5), .zp-table td:nth-child(n+5) { display:none; }
      }
    `;
    const s = document.createElement('style');
    s.id = STYLE_ID;
    s.textContent = css;
    document.head.appendChild(s);
  }

  // ─── UI section renderers ──────────────────────────────
  function renderSec1AiTraining(state) {
    const cur = state.prefs || {};
    const aiOptin = !!cur.ai_training_optin;
    return `
      <div class="zp-card">
        <h3 class="zp-card-title">1 · Quyền riêng tư AI Training</h3>
        <p class="zp-card-desc">
          Cho phép Zeni AI (Make + Claw) học từ dữ liệu đã ẩn danh hoá của bạn để cải thiện chất lượng dịch vụ.
          Khi bật, bạn sẽ được giảm 20% giá toàn bộ gói AI.
        </p>
        <div class="zp-toggle-row">
          <div class="zp-toggle-info">
            <div class="zp-toggle-label">Cho phép Zeni AI học từ data anonymized</div>
            <div class="zp-toggle-hint">
              Dữ liệu sẽ được ẩn danh hoá (PII redacted) trước khi đưa vào training pipeline.
              <strong style="color:#fde68a;">Bật để giảm 20% giá.</strong>
            </div>
          </div>
          <label class="zp-switch">
            <input type="checkbox" id="zp-ai-optin" ${aiOptin ? 'checked' : ''}>
            <span></span>
          </label>
        </div>
        <div class="zp-toggle-row">
          <div class="zp-toggle-info">
            <div class="zp-toggle-label">Cho phép log hoạt động (analytics)</div>
            <div class="zp-toggle-hint">Dùng cho việc cải thiện UX và phát hiện lỗi. Không bao gồm nội dung văn bản.</div>
          </div>
          <label class="zp-switch">
            <input type="checkbox" id="zp-analytics" ${cur.analytics_optin !== false ? 'checked' : ''}>
            <span></span>
          </label>
        </div>
        <div class="zp-action-row">
          <button class="zp-btn zp-btn-primary" id="zp-save-prefs">Lưu thay đổi</button>
        </div>
      </div>
    `;
  }

  function renderSec2Region(state) {
    const cur = (state.prefs && state.prefs.data_region) || 'asia-southeast1';
    const opts = [
      { val: 'asia-southeast1', name: 'asia-southeast1 (Singapore)', desc: 'Khuyến nghị cho khách hàng VN — độ trễ thấp nhất, tuân thủ NĐ 13/2023.' },
      { val: 'us-central1', name: 'us-central1 (Iowa, USA)', desc: 'Khuyến nghị cho khách hàng Mỹ. Một số model AI rẻ hơn 5–8%.' },
    ];
    return `
      <div class="zp-card">
        <h3 class="zp-card-title">2 · Vùng lưu trữ dữ liệu</h3>
        <p class="zp-card-desc">
          Chọn region vật lý nơi dữ liệu (database, storage, KMS keys) sẽ được lưu trữ.
          Lựa chọn này không thể đổi sau khi đã có dữ liệu sản xuất.
        </p>
        ${opts.map(o => `
          <label class="zp-radio-row ${cur === o.val ? 'active' : ''}" data-region="${o.val}">
            <input type="radio" name="zp-region" value="${o.val}" ${cur === o.val ? 'checked' : ''}>
            <div>
              <div class="zp-radio-label">${escHtml(o.name)}</div>
              <div class="zp-radio-desc">${escHtml(o.desc)}</div>
            </div>
          </label>
        `).join('')}
      </div>
    `;
  }

  function renderSec3Encryption(state) {
    const tier = (state.prefs && state.prefs.encryption_tier) || 1;
    const tiers = [
      { n: 1, label: 'Tier 1 — Google-managed encryption (mặc định, miễn phí)' },
      { n: 2, label: 'Tier 2 — Google-managed + audit log mở rộng' },
      { n: 3, label: 'Tier 3 — CMEK (Customer-Managed Encryption Keys)' },
      { n: 4, label: 'Tier 4 — HSM-backed CMEK + private VPC' },
    ];
    return `
      <div class="zp-card">
        <h3 class="zp-card-title">3 · Mức độ mã hoá</h3>
        <p class="zp-card-desc">
          Mọi dữ liệu đều được mã hoá ở trạng thái nghỉ (AES-256) và truyền (TLS 1.3) mặc định.
          Khách hàng enterprise có thể nâng cấp lên CMEK / HSM để giữ quyền kiểm soát khoá.
        </p>
        <div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:12px;">
          <div class="zp-tier">
            <span>Hiện tại:</span>
            <span class="zp-tier-num">Tier ${tier}/4</span>
          </div>
        </div>
        <div style="font-size:12px;color:#9E8BE5;margin-bottom:14px;line-height:1.5;">
          ${tiers.map(t =>
            `<div style="padding:4px 0;${t.n === tier ? 'color:#fde68a;font-weight:600;' : ''}">
              ${t.n <= tier ? '✓' : '○'} ${escHtml(t.label)}
            </div>`).join('')}
        </div>
        <button class="zp-btn zp-btn-ghost" id="zp-upgrade-cmek">Nâng cấp lên CMEK</button>
      </div>
    `;
  }

  function renderSec4AdminLog(state) {
    const rows = state.adminLog || [];
    let body;
    if (state.adminLogLoading) {
      body = '<div class="zp-loading">Đang tải...</div>';
    } else if (!rows.length) {
      body = '<div class="zp-empty">Chưa có yêu cầu admin truy cập nào.</div>';
    } else {
      body = `
        <div style="overflow-x:auto;">
          <table class="zp-table">
            <thead>
              <tr>
                <th>Thời gian</th><th>Admin</th><th>Lý do</th>
                <th>Phạm vi</th><th>Trạng thái</th><th>Hành động</th>
              </tr>
            </thead>
            <tbody>
              ${rows.map(r => {
                const status = (r.status || 'pending').toLowerCase();
                const isPending = status === 'pending';
                return `
                  <tr data-req-id="${escHtml(r.id || '')}">
                    <td>${escHtml(fmtDate(r.requested_at || r.created_at))}</td>
                    <td>${escHtml(r.admin_email || r.admin || '—')}</td>
                    <td style="max-width:240px;overflow:hidden;text-overflow:ellipsis;">${escHtml(r.reason || '')}</td>
                    <td style="font-family:monospace;font-size:11px;">${escHtml(r.scope || 'workspace')}</td>
                    <td><span class="zp-status zp-status-${status}">${escHtml(status)}</span></td>
                    <td>
                      ${isPending ? `
                        <button class="zp-btn zp-btn-ok" data-act="approve">Duyệt</button>
                        <button class="zp-btn zp-btn-no" data-act="deny">Từ chối</button>
                      ` : '—'}
                    </td>
                  </tr>
                `;
              }).join('')}
            </tbody>
          </table>
        </div>
      `;
    }
    return `
      <div class="zp-card">
        <h3 class="zp-card-title">4 · Lịch sử admin truy cập</h3>
        <p class="zp-card-desc">
          Mỗi lần kỹ sư Zeni cần truy cập workspace của bạn (ví dụ: support ticket), một yêu cầu sẽ được ghi nhận tại đây.
          Bạn có quyền duyệt hoặc từ chối; mọi truy cập đều có audit log không thể chỉnh sửa.
        </p>
        ${body}
      </div>
    `;
  }

  function renderSec5Violations(state) {
    const rows = state.violations || [];
    let body;
    if (state.violationsLoading) {
      body = '<div class="zp-loading">Đang tải...</div>';
    } else if (!rows.length) {
      body = '<div class="zp-empty">Không có vi phạm nào trong 30 ngày qua.</div>';
    } else {
      body = `
        <div style="overflow-x:auto;">
          <table class="zp-table">
            <thead>
              <tr>
                <th>Thời gian</th><th>Loại</th><th>Mức độ</th>
                <th>Agent</th><th>Redacted</th><th>Action</th>
              </tr>
            </thead>
            <tbody>
              ${rows.map(r => {
                const sev = (r.severity || 'low').toLowerCase();
                const sevCls = sev === 'high' ? 'zp-sev-high' : sev === 'medium' || sev === 'med' ? 'zp-sev-med' : 'zp-sev-low';
                const blocked = r.blocked === true || r.blocked === 'true';
                return `
                  <tr>
                    <td>${escHtml(fmtDate(r.detected_at || r.created_at))}</td>
                    <td>${escHtml(r.violation_type || r.type || '—')}</td>
                    <td><span class="${sevCls}">${escHtml(sev)}</span></td>
                    <td style="font-family:monospace;font-size:11px;">${escHtml(r.agent_name || '—')}</td>
                    <td style="color:#9E8BE5;">${r.redacted_count || 0}</td>
                    <td>
                      <span class="zp-status zp-status-${blocked ? 'blocked' : 'redacted'}">
                        ${blocked ? 'BLOCKED' : 'REDACTED'}
                      </span>
                    </td>
                  </tr>
                `;
              }).join('')}
            </tbody>
          </table>
        </div>
      `;
    }
    return `
      <div class="zp-card">
        <h3 class="zp-card-title">5 · Vi phạm output filter (30 ngày)</h3>
        <p class="zp-card-desc">
          Output của AI agent được scan tự động để phát hiện PII (CCCD, số tài khoản, mật khẩu) và nội dung không phù hợp.
          Mỗi sự kiện sẽ được redact (che đi) hoặc block (chặn hoàn toàn).
        </p>
        ${body}
      </div>
    `;
  }

  function renderSec6Gdpr() {
    return `
      <div class="zp-card">
        <h3 class="zp-card-title">6 · GDPR & Nghị định 13/2023</h3>
        <p class="zp-card-desc">
          Bạn có quyền: (a) Truy cập và xuất toàn bộ dữ liệu, (b) Yêu cầu xoá vĩnh viễn,
          (c) Sửa đổi dữ liệu cá nhân, (d) Phản đối xử lý dữ liệu, (e) Khiếu nại lên cơ quan bảo vệ dữ liệu.
        </p>
        <div class="zp-action-row">
          <button class="zp-btn zp-btn-ghost" id="zp-export">Xuất toàn bộ dữ liệu (JSON)</button>
          <button class="zp-btn zp-btn-danger" id="zp-delete">Yêu cầu xoá toàn bộ</button>
        </div>
        <p style="font-size:11.5px;color:#7C6BB0;margin-top:14px;line-height:1.5;">
          Yêu cầu xoá sẽ được xử lý trong 30 ngày. Trong thời gian này bạn có thể huỷ yêu cầu.
          Sau khi hoàn tất, dữ liệu không thể khôi phục.
        </p>
      </div>
    `;
  }

  function renderDeleteModal(onConfirm, onCancel) {
    const bg = document.createElement('div');
    bg.className = 'zp-modal-bg';
    bg.innerHTML = `
      <div class="zp-modal" role="dialog" aria-modal="true">
        <h3 class="zp-modal-h">Yêu cầu xoá toàn bộ dữ liệu?</h3>
        <p class="zp-modal-p">
          Hành động này sẽ xoá vĩnh viễn tài khoản, mọi project, database, secret, và lịch sử billing
          sau 30 ngày. Trong 30 ngày đó bạn có thể huỷ; sau đó <strong style="color:#FCA5A5;">không thể khôi phục</strong>.
          <br><br>
          Bạn chắc chắn muốn tiếp tục?
        </p>
        <div class="zp-modal-actions">
          <button class="zp-btn zp-btn-ghost" data-act="cancel">Huỷ</button>
          <button class="zp-btn zp-btn-danger" data-act="confirm">Xác nhận xoá</button>
        </div>
      </div>
    `;
    document.body.appendChild(bg);
    bg.addEventListener('click', (e) => {
      if (e.target === bg) { bg.remove(); onCancel && onCancel(); return; }
      const a = e.target.closest('[data-act]');
      if (!a) return;
      const act = a.getAttribute('data-act');
      bg.remove();
      if (act === 'confirm') onConfirm && onConfirm();
      else onCancel && onCancel();
    });
  }

  // ─── controller / event wiring ─────────────────────────
  async function loadAll(ws, state) {
    state.adminLogLoading = true;
    state.violationsLoading = true;
    const tasks = await Promise.allSettled([
      privacyApi.getPreferences(ws).catch(() => ({})),
      privacyApi.listAdminAccessLog(ws).catch(() => []),
      privacyApi.listViolations(ws).catch(() => []),
    ]);
    state.prefs = tasks[0].status === 'fulfilled' ? (tasks[0].value || {}) : {};
    state.adminLog = tasks[1].status === 'fulfilled'
      ? (Array.isArray(tasks[1].value) ? tasks[1].value : (tasks[1].value && tasks[1].value.items) || [])
      : [];
    state.violations = tasks[2].status === 'fulfilled'
      ? (Array.isArray(tasks[2].value) ? tasks[2].value : (tasks[2].value && tasks[2].value.items) || [])
      : [];
    state.adminLogLoading = false;
    state.violationsLoading = false;
  }

  function paint(rootEl, ws, state) {
    rootEl.innerHTML = `
      <div class="zp-root">
        <h1 class="zp-h1">Quyền riêng tư & Bảo mật</h1>
        <p class="zp-sub">
          Workspace: <strong style="color:#fde68a;font-family:monospace;">${escHtml(ws)}</strong>.
          Mọi cài đặt áp dụng cho riêng workspace này — workspace khác có config độc lập.
        </p>
        ${renderSec1AiTraining(state)}
        ${renderSec2Region(state)}
        ${renderSec3Encryption(state)}
        ${renderSec4AdminLog(state)}
        ${renderSec5Violations(state)}
        ${renderSec6Gdpr()}
      </div>
    `;
    wireEvents(rootEl, ws, state);
  }

  function wireEvents(rootEl, ws, state) {
    // Region radios
    rootEl.querySelectorAll('.zp-radio-row').forEach(row => {
      row.addEventListener('click', async () => {
        const region = row.getAttribute('data-region');
        if (!region || (state.prefs && state.prefs.data_region === region)) return;
        rootEl.querySelectorAll('.zp-radio-row').forEach(r => r.classList.remove('active'));
        row.classList.add('active');
        const inp = row.querySelector('input[type=radio]');
        if (inp) inp.checked = true;
        try {
          await privacyApi.updatePreferences(ws, { data_region: region });
          state.prefs = Object.assign({}, state.prefs, { data_region: region });
          toast('Đã lưu vùng dữ liệu: ' + region, 'ok');
        } catch (err) {
          toast('Lưu thất bại: ' + (err.message || 'lỗi không xác định'), 'err');
        }
      });
    });

    // Save prefs button (Sec 1)
    const saveBtn = rootEl.querySelector('#zp-save-prefs');
    if (saveBtn) {
      saveBtn.addEventListener('click', async () => {
        const aiOptin = !!(rootEl.querySelector('#zp-ai-optin') && rootEl.querySelector('#zp-ai-optin').checked);
        const analytics = !!(rootEl.querySelector('#zp-analytics') && rootEl.querySelector('#zp-analytics').checked);
        saveBtn.disabled = true;
        const orig = saveBtn.textContent;
        saveBtn.textContent = 'Đang lưu...';
        try {
          await privacyApi.updatePreferences(ws, {
            ai_training_optin: aiOptin,
            analytics_optin: analytics,
          });
          state.prefs = Object.assign({}, state.prefs, {
            ai_training_optin: aiOptin,
            analytics_optin: analytics,
          });
          toast('Đã lưu cài đặt riêng tư.' + (aiOptin ? ' Bạn được giảm 20% giá AI.' : ''), 'ok');
        } catch (err) {
          toast('Lưu thất bại: ' + (err.message || 'lỗi không xác định'), 'err');
        } finally {
          saveBtn.disabled = false;
          saveBtn.textContent = orig;
        }
      });
    }

    // CMEK upgrade
    const cmek = rootEl.querySelector('#zp-upgrade-cmek');
    if (cmek) {
      cmek.addEventListener('click', () => {
        toast('CMEK / HSM đang phát triển — vui lòng liên hệ sales@zenicloud.io.', 'ok');
      });
    }

    // Admin access approve / deny
    rootEl.querySelectorAll('.zp-card tbody tr[data-req-id]').forEach(tr => {
      const reqId = tr.getAttribute('data-req-id');
      tr.querySelectorAll('button[data-act]').forEach(btn => {
        btn.addEventListener('click', async () => {
          const act = btn.getAttribute('data-act');
          btn.disabled = true;
          try {
            if (act === 'approve') {
              await privacyApi.approveAdminAccess(reqId);
              toast('Đã duyệt yêu cầu admin truy cập.', 'ok');
            } else {
              await privacyApi.denyAdminAccess(reqId);
              toast('Đã từ chối yêu cầu admin truy cập.', 'ok');
            }
            // Reload admin log
            try {
              const list = await privacyApi.listAdminAccessLog(ws);
              state.adminLog = Array.isArray(list) ? list : (list && list.items) || [];
            } catch {}
            paint(rootEl, ws, state);
          } catch (err) {
            toast((act === 'approve' ? 'Duyệt' : 'Từ chối') + ' thất bại: ' + (err.message || ''), 'err');
            btn.disabled = false;
          }
        });
      });
    });

    // Export
    const exp = rootEl.querySelector('#zp-export');
    if (exp) {
      exp.addEventListener('click', async () => {
        exp.disabled = true;
        const orig = exp.textContent;
        exp.textContent = 'Đang chuẩn bị...';
        try {
          const data = await privacyApi.exportData(ws);
          const fname = 'zeni-export-' + ws + '-' + new Date().toISOString().slice(0, 10) + '.json';
          downloadBlob(JSON.stringify(data, null, 2), fname, 'application/json');
          toast('Đã xuất dữ liệu: ' + fname, 'ok');
        } catch (err) {
          toast('Xuất dữ liệu thất bại: ' + (err.message || ''), 'err');
        } finally {
          exp.disabled = false;
          exp.textContent = orig;
        }
      });
    }

    // Delete request
    const del = rootEl.querySelector('#zp-delete');
    if (del) {
      del.addEventListener('click', () => {
        renderDeleteModal(async () => {
          del.disabled = true;
          try {
            await privacyApi.requestDataDelete(ws);
            toast('Đã gửi yêu cầu xoá. Bạn có 30 ngày để huỷ.', 'ok');
          } catch (err) {
            toast('Yêu cầu xoá thất bại: ' + (err.message || ''), 'err');
          } finally {
            del.disabled = false;
          }
        });
      });
    }
  }

  // ─── public API ────────────────────────────────────────
  async function render(rootEl, ws) {
    if (!rootEl) {
      console.warn('[ZeniPrivacyUI] render: rootEl missing');
      return;
    }
    if (!ws) {
      ws = (window.state && window.state.currentWs) || 'holdings';
    }
    injectStyles();

    rootEl.innerHTML = `<div class="zp-loading" style="padding:60px 20px;">Đang tải cài đặt riêng tư...</div>`;

    const state = {
      prefs: {},
      adminLog: [],
      violations: [],
      adminLogLoading: true,
      violationsLoading: true,
    };

    try {
      await loadAll(ws, state);
    } catch (err) {
      console.warn('[ZeniPrivacyUI] loadAll error:', err);
    }
    paint(rootEl, ws, state);
  }

  window.ZeniPrivacyUI = {
    render,
    api: privacyApi,
  };

  console.log('[zeni-privacy] ready · ZeniAPI.privacy and ZeniPrivacyUI exposed');
})();
