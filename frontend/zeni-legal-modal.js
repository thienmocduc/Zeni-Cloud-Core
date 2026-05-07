/*
 * ZENI CLOUD CORE · LEGAL ACCEPTANCE MODAL
 *
 * Blocking first-use modal that asks user to:
 *   - REQUIRED: accept ToS + Privacy Policy + DPA
 *   - OPTIONAL: opt-in to AI training (–20% giá)
 *
 *   window.ZeniLegalModal.show(callback)
 *     → shows modal; calls callback(opts) when accepted, where opts = {ai_training_optin: bool}
 *
 *   window.ZeniLegalModal.hasAccepted()
 *     → true if user already accepted in this browser (localStorage)
 *
 * Persists acceptance to localStorage AND POSTs to /api/v1/privacy/preferences
 * if the user is logged in.
 */
(function () {
  'use strict';

  const ACCEPTED_KEY = 'zeni.legal.accepted_v1';
  const ACCESS_KEY = 'zeni.jwt.access';
  const API_BASE = (typeof window.ZENI_API_BASE === 'string' && window.ZENI_API_BASE) || '/api/v1';
  const STYLE_ID = 'zeni-legal-modal-styles';

  function injectStyles() {
    if (document.getElementById(STYLE_ID)) return;
    const css = `
      .zlm-bg {
        position: fixed; inset: 0; z-index: 99997;
        background: rgba(3, 0, 20, 0.86);
        backdrop-filter: blur(8px);
        display: flex; align-items: center; justify-content: center;
        padding: 16px;
        font-family: 'Inter', system-ui, -apple-system, sans-serif;
      }
      .zlm-card {
        background: #08051F;
        border: 1px solid rgba(168, 139, 250, 0.30);
        border-radius: 18px;
        max-width: 540px; width: 100%;
        max-height: calc(100vh - 32px);
        overflow-y: auto;
        padding: 32px 30px 26px;
        color: #e5e7eb;
        box-shadow: 0 32px 100px rgba(0, 0, 0, 0.6),
                    inset 0 0 60px rgba(168, 85, 247, 0.04);
      }
      .zlm-brand {
        display: flex; align-items: center; gap: 12px;
        margin-bottom: 18px;
      }
      .zlm-logo {
        width: 40px; height: 40px; border-radius: 10px;
        background: linear-gradient(135deg, #fde68a, #A855F7, #7E22CE);
        color: #1a0938; font-weight: 900; font-size: 20px;
        display: grid; place-items: center;
      }
      .zlm-brand-name { font-size: 14px; font-weight: 800; letter-spacing: -0.01em; }
      .zlm-brand-name em { font-style: normal; color: #fde68a; }
      .zlm-h {
        font-size: 22px; font-weight: 800; letter-spacing: -0.02em;
        margin: 0 0 8px; color: #fde68a;
      }
      .zlm-sub {
        font-size: 13px; color: #C4B5FD;
        margin: 0 0 18px; line-height: 1.55;
      }
      .zlm-bullets {
        background: rgba(168, 139, 250, 0.06);
        border: 1px solid rgba(168, 139, 250, 0.18);
        border-radius: 12px;
        padding: 14px 16px;
        margin-bottom: 18px;
        font-size: 13px; line-height: 1.6;
        color: #EDE9FE;
      }
      .zlm-bullets li { margin: 6px 0; padding-left: 4px; }
      .zlm-bullets a { color: #fde68a; text-decoration: underline; }
      .zlm-bullets a:hover { color: #fff; }
      .zlm-bullets strong { color: #fde68a; }
      .zlm-checkbox-row {
        display: flex; gap: 10px; align-items: flex-start;
        cursor: pointer; padding: 12px 14px;
        background: rgba(255, 255, 255, 0.03);
        border: 1px solid rgba(168, 139, 250, 0.15);
        border-radius: 10px;
        margin-bottom: 10px;
        font-size: 13px; line-height: 1.55;
        transition: 0.15s;
      }
      .zlm-checkbox-row:hover {
        border-color: rgba(253, 230, 138, 0.42);
        background: rgba(253, 230, 138, 0.04);
      }
      .zlm-checkbox-row input[type="checkbox"] {
        width: 18px; height: 18px; flex-shrink: 0;
        accent-color: #fde68a; cursor: pointer; margin-top: 1px;
      }
      .zlm-checkbox-row a {
        color: #fde68a; text-decoration: underline;
      }
      .zlm-checkbox-row a:hover { color: #fff; }
      .zlm-checkbox-row strong { color: #fde68a; }
      .zlm-actions {
        display: flex; gap: 10px;
        justify-content: flex-end;
        margin-top: 22px;
        flex-wrap: wrap;
      }
      .zlm-btn {
        padding: 12px 22px; border-radius: 10px; border: 0;
        cursor: pointer; font-size: 13px; font-weight: 700;
        letter-spacing: 0.02em; font-family: inherit;
        transition: 0.15s;
      }
      .zlm-btn-primary {
        background: linear-gradient(135deg, #fde68a, #F59E0B);
        color: #1a0938;
      }
      .zlm-btn-primary:hover:not(:disabled) {
        transform: translateY(-1px);
        box-shadow: 0 8px 24px rgba(253, 230, 138, 0.32);
      }
      .zlm-btn-primary:disabled {
        opacity: 0.45; cursor: not-allowed;
        background: rgba(168, 139, 250, 0.16);
        color: #7C6BB0;
      }
      .zlm-btn-ghost {
        background: rgba(255, 255, 255, 0.06);
        border: 1px solid rgba(168, 139, 250, 0.20);
        color: #C4B5FD;
      }
      .zlm-btn-ghost:hover {
        border-color: #fde68a;
        color: #fde68a;
      }
      .zlm-error {
        display: none;
        margin-top: 12px;
        padding: 10px 14px;
        background: rgba(248, 113, 113, 0.12);
        border: 1px solid rgba(248, 113, 113, 0.30);
        border-radius: 8px;
        color: #FCA5A5; font-size: 12px;
      }
      .zlm-error.show { display: block; }
      .zlm-foot {
        margin-top: 16px;
        font-size: 11px;
        color: #7C6BB0;
        text-align: center;
        line-height: 1.5;
      }
      @media (max-width: 540px) {
        .zlm-card { padding: 22px 18px 20px; border-radius: 14px; }
        .zlm-h { font-size: 18px; }
        .zlm-actions { flex-direction: column-reverse; }
        .zlm-btn { width: 100%; }
      }
    `;
    const s = document.createElement('style');
    s.id = STYLE_ID;
    s.textContent = css;
    document.head.appendChild(s);
  }

  function escHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function buildModal() {
    const bg = document.createElement('div');
    bg.className = 'zlm-bg';
    bg.setAttribute('role', 'dialog');
    bg.setAttribute('aria-modal', 'true');
    bg.setAttribute('aria-labelledby', 'zlm-title');
    bg.innerHTML = `
      <div class="zlm-card">
        <div class="zlm-brand">
          <div class="zlm-logo">Z</div>
          <div>
            <div class="zlm-brand-name">Zeni<em>Cloud</em></div>
            <div style="font-size:10px;color:#7C6BB0;letter-spacing:0.1em;font-family:monospace;">PRIVACY GATE</div>
          </div>
        </div>

        <h2 class="zlm-h" id="zlm-title">Trước khi bắt đầu</h2>
        <p class="zlm-sub">
          Để bảo vệ quyền riêng tư của bạn theo GDPR và Nghị định 13/2023/NĐ-CP,
          vui lòng đọc và xác nhận các nội dung sau:
        </p>

        <ul class="zlm-bullets">
          <li>Dữ liệu được lưu tại
            <strong>asia-southeast1 (Singapore)</strong> — bạn có thể đổi region trong Privacy Settings.</li>
          <li>Mọi dữ liệu được mã hoá AES-256 (rest) + TLS 1.3 (transit).
            Nâng cấp lên CMEK/HSM nếu cần.</li>
          <li>PII được tự động phát hiện và ẩn (CCCD, số tài khoản, mật khẩu).
            Xem
            <a href="/legal/privacy.html" target="_blank" rel="noopener">Chính sách Bảo mật</a>.</li>
          <li>Khi kỹ sư Zeni cần truy cập workspace của bạn, bạn sẽ được hỏi trước.
            Mọi truy cập đều có audit log.</li>
          <li>Bạn có thể xuất hoặc xoá toàn bộ dữ liệu bất kỳ lúc nào — chi tiết tại
            <a href="/legal/dpa.html" target="_blank" rel="noopener">DPA</a>.</li>
        </ul>

        <label class="zlm-checkbox-row">
          <input type="checkbox" id="zlm-cb-tos">
          <span>
            Tôi đã đọc và đồng ý
            <a href="/legal/terms.html" target="_blank" rel="noopener">Điều khoản Dịch vụ</a>,
            <a href="/legal/privacy.html" target="_blank" rel="noopener">Chính sách Bảo mật</a>,
            và
            <a href="/legal/dpa.html" target="_blank" rel="noopener">Data Processing Addendum</a>.
            <span style="color:#FCA5A5;">(bắt buộc)</span>
          </span>
        </label>

        <label class="zlm-checkbox-row">
          <input type="checkbox" id="zlm-cb-ai">
          <span>
            <strong>(Tuỳ chọn — giảm 20% giá)</strong>
            Tôi đồng ý chia sẻ dữ liệu đã ẩn danh hoá cho Zeni AI training để cải thiện dịch vụ.
            Chi tiết tại
            <a href="/legal/ai-data-usage.html" target="_blank" rel="noopener">AI Data Usage Policy</a>.
          </span>
        </label>

        <div class="zlm-error" id="zlm-error"></div>

        <div class="zlm-actions">
          <button class="zlm-btn zlm-btn-ghost" id="zlm-cancel" type="button">Để sau</button>
          <button class="zlm-btn zlm-btn-primary" id="zlm-continue" type="button" disabled>Tiếp tục</button>
        </div>

        <div class="zlm-foot">
          Bạn có thể thay đổi các tuỳ chọn này bất kỳ lúc nào tại
          <strong style="color:#C4B5FD;">Settings → Privacy</strong>.
        </div>
      </div>
    `;
    return bg;
  }

  async function persistOnBackend(prefs) {
    const tok = localStorage.getItem(ACCESS_KEY);
    if (!tok) return false;
    const ws = (window.state && window.state.currentWs) ||
               (window.__ZENI_REAL_USER && window.__ZENI_REAL_USER.workspaces && window.__ZENI_REAL_USER.workspaces[0]) ||
               null;
    if (!ws) return false;
    try {
      const r = await fetch(API_BASE + '/privacy/preferences?ws=' + encodeURIComponent(ws), {
        method: 'POST',
        headers: {
          'Authorization': 'Bearer ' + tok,
          'Content-Type': 'application/json',
          'Accept': 'application/json',
        },
        body: JSON.stringify(prefs),
      });
      return r.ok;
    } catch (e) {
      console.warn('[ZeniLegalModal] persist failed:', e);
      return false;
    }
  }

  function persistLocal(prefs) {
    try {
      localStorage.setItem(ACCEPTED_KEY, JSON.stringify({
        ts: Date.now(),
        ai_training_optin: !!prefs.ai_training_optin,
        version: 1,
      }));
    } catch {}
  }

  function hasAccepted() {
    try {
      const raw = localStorage.getItem(ACCEPTED_KEY);
      if (!raw) return false;
      const obj = JSON.parse(raw);
      return !!(obj && obj.ts);
    } catch { return false; }
  }

  function show(callback) {
    injectStyles();

    // If already shown / accepted, callback immediately
    if (hasAccepted()) {
      if (typeof callback === 'function') {
        try {
          const raw = JSON.parse(localStorage.getItem(ACCEPTED_KEY) || '{}');
          callback({ ai_training_optin: !!raw.ai_training_optin });
        } catch { callback({ ai_training_optin: false }); }
      }
      return;
    }

    // Avoid double-stacking
    if (document.querySelector('.zlm-bg')) return;

    const modal = buildModal();
    document.body.appendChild(modal);
    document.body.style.overflow = 'hidden';

    const cbTos = modal.querySelector('#zlm-cb-tos');
    const cbAi = modal.querySelector('#zlm-cb-ai');
    const btnContinue = modal.querySelector('#zlm-continue');
    const btnCancel = modal.querySelector('#zlm-cancel');
    const errEl = modal.querySelector('#zlm-error');

    function syncBtn() {
      btnContinue.disabled = !cbTos.checked;
      if (errEl.classList.contains('show') && cbTos.checked) {
        errEl.classList.remove('show');
      }
    }
    cbTos.addEventListener('change', syncBtn);
    syncBtn();

    function close() {
      document.body.style.overflow = '';
      modal.remove();
    }

    btnCancel.addEventListener('click', () => {
      close();
      if (typeof callback === 'function') callback(null);
    });

    btnContinue.addEventListener('click', async () => {
      if (!cbTos.checked) {
        errEl.textContent = 'Bạn cần đồng ý Điều khoản Dịch vụ và Chính sách Bảo mật để tiếp tục.';
        errEl.classList.add('show');
        return;
      }
      const prefs = {
        tos_accepted: true,
        privacy_accepted: true,
        dpa_accepted: true,
        ai_training_optin: !!cbAi.checked,
      };
      btnContinue.disabled = true;
      const orig = btnContinue.textContent;
      btnContinue.textContent = 'Đang lưu...';
      // Try backend (best effort) + always persist locally
      try { await persistOnBackend(prefs); } catch {}
      persistLocal(prefs);
      btnContinue.textContent = orig;
      close();
      if (typeof callback === 'function') {
        callback({ ai_training_optin: prefs.ai_training_optin });
      }
    });

    // ESC to cancel
    function onKey(e) {
      if (e.key === 'Escape') {
        document.removeEventListener('keydown', onKey);
        btnCancel.click();
      }
    }
    document.addEventListener('keydown', onKey);

    // Focus first checkbox
    setTimeout(() => { try { cbTos.focus(); } catch {} }, 50);
  }

  function reset() {
    try { localStorage.removeItem(ACCEPTED_KEY); } catch {}
  }

  window.ZeniLegalModal = {
    show,
    hasAccepted,
    reset,
  };

  console.log('[zeni-legal-modal] ready · call ZeniLegalModal.show(cb)');
})();
