/*
 * ZENI CLOUD CORE · Real-data view renderers
 * Loaded AFTER zeni-realdata.js. Hooks into setView to render real data
 * for each dashboard tab without modifying existing index.html IIFE code.
 *
 * Tabs covered:
 *   - compute   (L1: list real Cloud Run projects)
 *   - data      (L2: list ws schema tables + recent SQL queries)
 *   - ai        (L3: model list + agent kinds + recent runs)
 *   - auto      (L4: connectors + cron jobs + webhook attempts)
 *   - identity  (L5: secrets + MFA status + tokens)
 *   - web3      (L6: live $ZENI / Badge / Polygon stats)
 *   - billing   (NEW tab: cost dashboard, wallet, subscription)
 */
(function () {
  'use strict';
  if (!window.ZeniAPI) return;

  const log = (...a) => console.log('[views]', ...a);
  const $ = (id) => document.getElementById(id);

  function jwtHeaders() {
    return {
      'Authorization': 'Bearer ' + (localStorage.getItem('zeni.jwt.access') || ''),
      'Content-Type': 'application/json',
    };
  }

  async function apiGet(path) {
    const r = await fetch('/api/v1' + path, { headers: jwtHeaders() });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  // Helpers
  function fmtVnd(n) {
    return Math.round(n || 0).toLocaleString('vi-VN') + 'đ';
  }
  function fmtPct(n) { return (n || 0).toFixed(1) + '%'; }
  function timeAgo(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    const s = Math.floor((Date.now() - d.getTime()) / 1000);
    if (s < 60) return s + 's ago';
    if (s < 3600) return Math.floor(s/60) + 'm ago';
    if (s < 86400) return Math.floor(s/3600) + 'h ago';
    return Math.floor(s/86400) + 'd ago';
  }
  function escHtml(s) {
    return String(s||'').replace(/[&<>"']/g, c =>
      ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  // ─── Generic overlay renderer ────────────────────────────
  function injectOverlay(viewId, html) {
    let target = $('view-' + viewId) || $('content') || document.querySelector('main');
    if (!target) return;
    let overlay = $('zeni-real-overlay-' + viewId);
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.id = 'zeni-real-overlay-' + viewId;
      overlay.style.cssText = 'background:rgba(10,5,32,0.85);border:1px solid rgba(168,139,250,0.2);border-radius:12px;padding:20px;margin:16px 0;backdrop-filter:blur(10px);';
      target.insertBefore(overlay, target.firstChild);
    }
    overlay.innerHTML = html;
  }

  // ─── L1 Compute view ────────────────────────────────────
  async function renderCompute() {
    const ws = window.state.currentWs;
    try {
      const projects = await apiGet('/projects?ws=' + encodeURIComponent(ws));
      const html = `
        <h3 style="font-size:13px;letter-spacing:0.1em;color:#FDE68A;text-transform:uppercase;margin-bottom:14px;">
          ⚡ L1 COMPUTE · REAL DATA · workspace=${ws}
        </h3>
        <div style="font-size:12px;color:#C4B5FD;margin-bottom:12px;">
          ${projects.length} project(s) deployed Cloud Run
        </div>
        <table style="width:100%;font-size:12px;color:#EDE9FE;border-collapse:collapse;">
          <thead style="border-bottom:1px solid rgba(168,139,250,0.2);">
            <tr style="text-align:left;color:#9E8BE5;">
              <th style="padding:6px;">Name</th><th>Status</th><th>Region</th>
              <th>CPU/Mem</th><th>URL</th><th>Last deploy</th>
            </tr>
          </thead>
          <tbody>
            ${projects.length === 0 ? '<tr><td colspan=6 style="padding:14px;color:#7C6BB0;">Chưa có project nào — POST /projects để deploy.</td></tr>' :
              projects.map(p => `
                <tr style="border-bottom:1px solid rgba(168,139,250,0.08);">
                  <td style="padding:8px 6px;font-weight:600;">${escHtml(p.name)}</td>
                  <td><span style="padding:2px 8px;border-radius:4px;font-size:10px;background:${p.status==='running'?'#22D3EE22':p.status==='deploying'?'#FDE68A22':'#F8717122'};color:${p.status==='running'?'#22D3EE':p.status==='deploying'?'#FDE68A':'#F87171'};">${p.status}</span></td>
                  <td style="color:#9E8BE5;">${escHtml(p.region)}</td>
                  <td style="font-family:monospace;font-size:11px;">${escHtml(p.cpu)} / ${escHtml(p.memory)}</td>
                  <td>${p.domain ? `<a href="${escHtml(p.domain)}" target=_blank style="color:#22D3EE;">${escHtml(p.domain.slice(0,40))}…</a>` : '—'}</td>
                  <td style="color:#9E8BE5;font-size:11px;">${timeAgo(p.last_deploy)}</td>
                </tr>
              `).join('')}
          </tbody>
        </table>
      `;
      injectOverlay('compute', html);
    } catch (e) { log('compute fail:', e); }
  }

  // ─── L2 Data view ───────────────────────────────────────
  async function renderData() {
    const ws = window.state.currentWs;
    try {
      const tables = await apiGet('/data/tables?ws=' + encodeURIComponent(ws));
      const html = `
        <h3 style="font-size:13px;letter-spacing:0.1em;color:#FDE68A;text-transform:uppercase;margin-bottom:14px;">
          🗄️ L2 DATA · REAL CLOUD SQL · schema=${tables.schema}
        </h3>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;">
          ${tables.tables.length === 0 ? '<div style="color:#7C6BB0;">Chưa có bảng. CREATE TABLE qua /data/query.</div>' :
            tables.tables.map(t => `
              <div style="background:rgba(99,102,241,0.08);border:1px solid rgba(99,102,241,0.18);border-radius:8px;padding:12px;">
                <div style="font-family:monospace;font-size:13px;color:#A5B4FC;font-weight:700;">${escHtml(t.name)}</div>
                <div style="font-size:11px;color:#9E8BE5;margin-top:4px;">${t.columns} cột</div>
              </div>
            `).join('')}
        </div>
      `;
      injectOverlay('data', html);
    } catch (e) { log('data fail:', e); }
  }

  // ─── L3 AI view ─────────────────────────────────────────
  async function renderAI() {
    try {
      const [models, agents] = await Promise.all([
        apiGet('/ai/models'),
        apiGet('/agents/kinds'),
      ]);
      const html = `
        <h3 style="font-size:13px;letter-spacing:0.1em;color:#FDE68A;text-transform:uppercase;margin-bottom:14px;">
          🤖 L3 AI · REAL VERTEX AI · GCP-only
        </h3>
        <div style="margin-bottom:18px;">
          <div style="font-size:11px;color:#C4B5FD;letter-spacing:0.1em;margin-bottom:8px;">TEXT MODELS</div>
          ${(models.text||[]).map(m => `
            <div style="display:flex;justify-content:space-between;padding:6px 10px;background:rgba(34,211,238,0.05);border-radius:6px;margin-bottom:4px;font-size:12px;">
              <span style="font-family:monospace;color:#22D3EE;">${escHtml(m.id)}</span>
              <span style="color:#9E8BE5;">$${m.input_per_1m}/1M in · $${m.output_per_1m}/1M out</span>
            </div>
          `).join('')}
        </div>
        <div style="margin-bottom:18px;">
          <div style="font-size:11px;color:#C4B5FD;letter-spacing:0.1em;margin-bottom:8px;">IMAGE / EMBEDDING</div>
          ${(models.image||[]).concat(models.embedding||[]).map(m => `
            <div style="padding:6px 10px;background:rgba(216,180,254,0.05);border-radius:6px;margin-bottom:4px;font-size:12px;">
              <span style="font-family:monospace;color:#D8B4FE;">${escHtml(m.id)}</span>
              <span style="color:#9E8BE5;margin-left:10px;">${m.cost_per_image ? '$'+m.cost_per_image+'/ảnh' : '$'+m.input_per_1m+'/1M tok'}</span>
            </div>
          `).join('')}
        </div>
        <div>
          <div style="font-size:11px;color:#C4B5FD;letter-spacing:0.1em;margin-bottom:8px;">SPECIALIZED DESIGN AGENTS (5)</div>
          <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;">
          ${(agents.agents||[]).map(a => `
            <div style="background:rgba(168,85,247,0.08);border:1px solid rgba(168,85,247,0.2);border-radius:8px;padding:10px;">
              <div style="font-weight:700;color:#FDE68A;font-size:13px;">${escHtml(a.name)}</div>
              <div style="font-size:10px;color:#9E8BE5;margin-top:4px;">${escHtml(a.expertise)}</div>
              <div style="font-size:10px;color:#22D3EE;margin-top:6px;">${a.supports_render?'+ ảnh render':'(text only)'}</div>
            </div>
          `).join('')}
          </div>
        </div>
      `;
      injectOverlay('ai', html);
    } catch (e) { log('ai fail:', e); }
  }

  // ─── L4 Automation view ────────────────────────────────
  async function renderAuto() {
    const ws = window.state.currentWs;
    try {
      const [conns, crons, attempts] = await Promise.all([
        apiGet('/automation/connectors?ws=' + encodeURIComponent(ws)),
        apiGet('/automation/crons?ws=' + encodeURIComponent(ws)),
        apiGet('/automation/webhook-attempts?ws=' + encodeURIComponent(ws) + '&limit=10'),
      ]);
      const html = `
        <h3 style="font-size:13px;letter-spacing:0.1em;color:#FDE68A;text-transform:uppercase;margin-bottom:14px;">
          🔌 L4 AUTOMATION · workspace=${ws}
        </h3>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;">
          <div>
            <div style="font-size:11px;color:#C4B5FD;letter-spacing:0.1em;margin-bottom:8px;">CONNECTORS (${conns.length})</div>
            ${conns.length === 0 ? '<div style="font-size:11px;color:#7C6BB0;">Chưa có connector</div>' :
              conns.map(c => `
                <div style="font-size:11px;padding:6px 10px;background:rgba(253,230,138,0.06);border-radius:6px;margin-bottom:4px;">
                  <strong style="color:#FDE68A;">${escHtml(c.type)}</strong>
                  <span style="color:#9E8BE5;margin-left:8px;">${c.events_7d} events 7d</span>
                </div>
              `).join('')}
          </div>
          <div>
            <div style="font-size:11px;color:#C4B5FD;letter-spacing:0.1em;margin-bottom:8px;">CRONS (${crons.count||0})</div>
            ${(crons.crons||[]).slice(0,8).map(c => `
              <div style="font-size:11px;padding:6px 10px;background:rgba(34,211,238,0.05);border-radius:6px;margin-bottom:4px;">
                <strong style="color:#22D3EE;">${escHtml(c.name)}</strong>
                <span style="color:#9E8BE5;margin-left:8px;font-family:monospace;">${escHtml(c.schedule)}</span>
                <span style="color:${c.state==='ENABLED'?'#22D3EE':'#7C6BB0'};margin-left:8px;">${c.state}</span>
              </div>
            `).join('')}
          </div>
        </div>
        <div style="margin-top:14px;">
          <div style="font-size:11px;color:#C4B5FD;letter-spacing:0.1em;margin-bottom:8px;">WEBHOOK ATTEMPTS (DLQ)</div>
          ${attempts.attempts.length === 0 ? '<div style="font-size:11px;color:#22D3EE;">✓ Tất cả webhooks dispatch thành công, không có retry pending</div>' :
            `<table style="width:100%;font-size:11px;">
              <tr style="color:#9E8BE5;"><th align=left>id</th><th align=left>action</th><th>status</th><th>attempts</th><th>next/done</th></tr>
              ${attempts.attempts.map(a => `<tr>
                <td style="font-family:monospace;color:#9E8BE5;">${a.id}</td>
                <td style="color:#EDE9FE;">${escHtml(a.action)}</td>
                <td><span style="color:${a.status==='succeeded'?'#22D3EE':a.status==='dlq'?'#F87171':'#FDE68A'};">${a.status}</span></td>
                <td align=center>${a.attempts}</td>
                <td style="color:#9E8BE5;">${timeAgo(a.next_attempt_at || a.succeeded_at || a.dlq_at)}</td>
              </tr>`).join('')}
            </table>`}
        </div>
      `;
      injectOverlay('auto', html);
    } catch (e) { log('auto fail:', e); }
  }

  // ─── L5 Identity view ──────────────────────────────────
  async function renderIdentity() {
    const ws = window.state.currentWs;
    try {
      const [secrets, tokens, me] = await Promise.all([
        apiGet('/identity/secrets?ws=' + encodeURIComponent(ws)),
        apiGet('/api-tokens?ws=' + encodeURIComponent(ws)).catch(() => []),
        apiGet('/auth/me'),
      ]);
      const html = `
        <h3 style="font-size:13px;letter-spacing:0.1em;color:#FDE68A;text-transform:uppercase;margin-bottom:14px;">
          🔐 L5 IDENTITY · workspace=${ws}
        </h3>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;">
          <div>
            <div style="font-size:11px;color:#C4B5FD;letter-spacing:0.1em;margin-bottom:8px;">YOUR ACCOUNT</div>
            <div style="background:rgba(168,85,247,0.08);border-radius:8px;padding:10px;font-size:12px;">
              <div><strong style="color:#FAF5FF;">${escHtml(me.name)}</strong> · ${escHtml(me.email)}</div>
              <div style="color:#9E8BE5;font-size:11px;margin-top:4px;">Role: ${me.role} · MFA: ${me.mfa_enabled?'<span style="color:#22D3EE;">✓ enabled</span>':'<span style="color:#FDE68A;">⚠ chưa bật</span>'}</div>
              <div style="color:#9E8BE5;font-size:11px;">Workspaces: ${(me.workspaces||[]).join(', ')}</div>
            </div>
          </div>
          <div>
            <div style="font-size:11px;color:#C4B5FD;letter-spacing:0.1em;margin-bottom:8px;">API TOKENS (${tokens.length||0})</div>
            ${(tokens||[]).slice(0,5).map(t => `
              <div style="font-size:11px;padding:6px 10px;background:rgba(34,211,238,0.05);border-radius:6px;margin-bottom:4px;">
                <strong style="color:#22D3EE;">${escHtml(t.name)}</strong>
                <div style="color:#9E8BE5;font-family:monospace;">${escHtml(t.token_prefix)} · ${t.scopes} · used ${t.use_count}</div>
              </div>
            `).join('')}
          </div>
        </div>
        <div style="margin-top:14px;">
          <div style="font-size:11px;color:#C4B5FD;letter-spacing:0.1em;margin-bottom:8px;">SECRETS VAULT (${secrets.length})</div>
          ${secrets.length === 0 ? '<div style="font-size:11px;color:#7C6BB0;">Chưa có secret. POST /identity/secrets để thêm.</div>' :
            `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:6px;">
              ${secrets.map(s => `
                <div style="font-size:11px;padding:6px 10px;background:rgba(253,230,138,0.06);border-radius:6px;">
                  <strong style="color:#FDE68A;font-family:monospace;">${escHtml(s.name)}</strong>
                  <div style="color:#9E8BE5;">env: ${escHtml(s.env)} · rotations: ${s.rotations}</div>
                </div>
              `).join('')}
            </div>`}
        </div>
      `;
      injectOverlay('identity', html);
    } catch (e) { log('identity fail:', e); }
  }

  // ─── L6 Web3 view ──────────────────────────────────────
  async function renderWeb3() {
    try {
      const [stack, chains] = await Promise.all([
        apiGet('/web3/zeni-stack'),
        apiGet('/web3/chains'),
      ]);
      const t = stack.ZENI_TOKEN || {};
      const b = stack.BADGE_SBT || {};
      const n = stack.DEPLOYER_BALANCE || {};
      const html = `
        <h3 style="font-size:13px;letter-spacing:0.1em;color:#FDE68A;text-transform:uppercase;margin-bottom:14px;">
          🪙 L6 WEB3 · POLYGON MAINNET LIVE
        </h3>
        <div style="background:rgba(216,180,254,0.06);border:1px solid rgba(216,180,254,0.2);border-radius:8px;padding:14px;margin-bottom:14px;">
          <div style="font-size:13px;font-weight:700;color:#D8B4FE;">$ZENI Token</div>
          <div style="font-size:11px;color:#9E8BE5;margin-top:4px;">Address: <code style="font-family:monospace;color:#FDE68A;">${escHtml(t.address||'')}</code></div>
          <div style="font-size:11px;color:#9E8BE5;">Symbol: ${escHtml(t.symbol)} · Decimals: ${t.decimals}</div>
          <div style="font-size:18px;font-weight:800;color:#FAF5FF;margin-top:6px;">Total supply: ${(t.total_supply||0).toLocaleString('vi-VN')} ZENI</div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px;">
          <div style="background:rgba(168,85,247,0.06);border:1px solid rgba(168,85,247,0.2);border-radius:8px;padding:12px;">
            <div style="font-size:11px;color:#C4B5FD;text-transform:uppercase;">Zeni Badge SBT</div>
            <div style="color:#FAF5FF;margin-top:4px;">${escHtml(b.name||'-')} (${escHtml(b.symbol||'-')})</div>
            <div style="font-size:11px;color:#9E8BE5;margin-top:4px;">Total: ${b.total_supply ?? 'N/A'}</div>
          </div>
          <div style="background:rgba(253,230,138,0.06);border:1px solid rgba(253,230,138,0.2);border-radius:8px;padding:12px;">
            <div style="font-size:11px;color:#FDE68A;text-transform:uppercase;">Deployer wallet</div>
            <div style="font-size:18px;font-weight:800;color:#FDE68A;margin-top:4px;">${(n.balance||0).toFixed(4)} ${escHtml(n.symbol||'MATIC')}</div>
          </div>
        </div>
        <div>
          <div style="font-size:11px;color:#C4B5FD;letter-spacing:0.1em;margin-bottom:8px;">SUPPORTED CHAINS · LIVE STATUS</div>
          ${(chains.status||[]).map(c => `
            <div style="display:flex;justify-content:space-between;padding:6px 10px;background:rgba(34,211,238,0.05);border-radius:6px;margin-bottom:4px;font-size:11px;">
              <span><strong style="color:#22D3EE;">${escHtml(c.chain)}</strong> ${c.connected?'✓':'✗'}</span>
              <span style="color:#9E8BE5;font-family:monospace;">block ${c.block_number} · gas ${c.gas_price_gwei} gwei</span>
            </div>
          `).join('')}
        </div>
      `;
      injectOverlay('web3', html);
    } catch (e) { log('web3 fail:', e); }
  }

  // ─── Billing tab (NEW) ─────────────────────────────────
  async function renderBilling() {
    const ws = window.state.currentWs;
    try {
      const summary = await apiGet('/billing/dashboard/summary?ws=' + encodeURIComponent(ws) + '&days=30');
      const sub = summary.subscription;
      const html = `
        <h3 style="font-size:13px;letter-spacing:0.1em;color:#FDE68A;text-transform:uppercase;margin-bottom:14px;">
          💰 BILLING · workspace=${ws} · last 30 days
        </h3>
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:18px;">
          <div style="background:rgba(34,211,238,0.08);border-radius:10px;padding:14px;">
            <div style="font-size:10px;color:#22D3EE;text-transform:uppercase;letter-spacing:0.1em;">Wallet balance</div>
            <div style="font-size:22px;font-weight:800;color:#FAF5FF;margin-top:4px;">${fmtVnd(summary.wallet.balance_vnd)}</div>
          </div>
          <div style="background:rgba(253,230,138,0.08);border-radius:10px;padding:14px;">
            <div style="font-size:10px;color:#FDE68A;text-transform:uppercase;letter-spacing:0.1em;">Spent (30d)</div>
            <div style="font-size:22px;font-weight:800;color:#FAF5FF;margin-top:4px;">${fmtVnd(summary.total_spent_vnd)}</div>
            <div style="font-size:11px;color:#9E8BE5;margin-top:2px;">${summary.total_charges_count} transactions</div>
          </div>
          <div style="background:rgba(168,85,247,0.08);border-radius:10px;padding:14px;">
            <div style="font-size:10px;color:#A855F7;text-transform:uppercase;letter-spacing:0.1em;">Tier</div>
            <div style="font-size:22px;font-weight:800;color:#FAF5FF;margin-top:4px;">${sub ? sub.tier.toUpperCase() : 'FREE'}</div>
            ${sub ? `<div style="font-size:11px;color:#9E8BE5;margin-top:2px;">đến ${sub.period_end ? sub.period_end.slice(0,10) : '-'}</div>` : ''}
          </div>
        </div>
        ${sub ? `
        <div style="margin-bottom:18px;">
          <div style="font-size:11px;color:#C4B5FD;letter-spacing:0.1em;margin-bottom:8px;">QUOTA USAGE THÁNG NÀY</div>
          ${[
            {k:'runs', label:'Agent runs'},
            {k:'renders', label:'Image renders'},
            {k:'tokens', label:'Text tokens out'}
          ].map(it => `
            <div style="margin-bottom:8px;">
              <div style="display:flex;justify-content:space-between;font-size:12px;color:#EDE9FE;">
                <span>${it.label}</span>
                <span style="font-family:monospace;">${(sub.used[it.k]||0).toLocaleString()}/${(sub.quota[it.k]||0).toLocaleString()} (${fmtPct(sub.usage_pct[it.k])})</span>
              </div>
              <div style="height:6px;background:rgba(168,139,250,0.1);border-radius:3px;overflow:hidden;margin-top:3px;">
                <div style="height:100%;background:linear-gradient(90deg,#22D3EE,#A855F7);width:${Math.min(100, sub.usage_pct[it.k])}%;"></div>
              </div>
            </div>
          `).join('')}
        </div>` : ''}
        <div>
          <div style="font-size:11px;color:#C4B5FD;letter-spacing:0.1em;margin-bottom:8px;">CHI PHÍ THEO LAYER</div>
          ${Object.entries(summary.by_layer || {}).map(([layer, info]) => `
            <div style="display:flex;justify-content:space-between;padding:6px 10px;background:rgba(168,139,250,0.05);border-radius:6px;margin-bottom:4px;font-size:12px;">
              <span><strong style="color:#FDE68A;">${escHtml(layer)}</strong> <span style="color:#9E8BE5;">${info.total_count} actions</span></span>
              <span style="font-family:monospace;color:#22D3EE;">$${(info.total_cost_usd||0).toFixed(4)}</span>
            </div>
          `).join('')}
        </div>
      `;
      injectOverlay('billing', html);
    } catch (e) { log('billing fail:', e); }
  }

  // ─── Hook into setView ─────────────────────────────────
  const renderers = {
    compute: renderCompute,
    data: renderData,
    ai: renderAI,
    auto: renderAuto,
    automation: renderAuto,
    identity: renderIdentity,
    web3: renderWeb3,
    billing: renderBilling,
  };

  function attach() {
    if (!window.setView || !window.state) {
      setTimeout(attach, 200); return;
    }
    const _origSetView = window.setView;
    window.setView = function (view) {
      _origSetView(view);
      if (window.ZeniAPI && window.ZeniAPI.isAuthed() && renderers[view]) {
        try { renderers[view](); } catch (e) { log('render', view, 'failed:', e); }
      }
    };
    log('hooked setView for 7 views (compute/data/ai/auto/identity/web3/billing)');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', attach);
  } else {
    attach();
  }

  // Expose for manual testing
  window.ZeniAppViews = { renderers };
})();
