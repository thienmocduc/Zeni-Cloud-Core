/*
 * ZENI CLOUD CORE · API CLIENT + LOGIN PATCH
 * Kết nối HTML demo với FastAPI backend thật.
 *
 *   - window.ZeniAPI         : fetch wrapper với JWT auto-refresh
 *   - window.ZeniAPI.login() : POST /api/v1/auth/login, lưu access+refresh
 *   - Override window.doLogin / window.doLogout sau khi demo IIFE chạy xong
 *     → login thật qua backend; fallback demo nếu backend không phản hồi.
 *   - Các action khác (deploy, query, rotate …) có thể gọi ZeniAPI.<method>().
 */
(function () {
  'use strict';

  const ACCESS_KEY  = 'zeni.jwt.access';
  const REFRESH_KEY = 'zeni.jwt.refresh';
  const ACCOUNTS_KEY = 'zeni.accounts.v1';   // NEW: multi-account support
  const ACTIVE_ACCOUNT_KEY = 'zeni.active.account';
  const API_BASE    = (typeof window.ZENI_API_BASE === 'string' && window.ZENI_API_BASE) || '/api/v1';

  // ─── Multi-account storage helpers ─────────────
  // accounts: [{id, email, name, access_token, refresh_token, workspace, ws_list, savedAt}]
  function _getAccounts() {
    try { return JSON.parse(localStorage.getItem(ACCOUNTS_KEY) || '[]'); }
    catch (e) { return []; }
  }
  function _saveAccounts(list) {
    localStorage.setItem(ACCOUNTS_KEY, JSON.stringify(list || []));
  }
  function _getActiveId() { return localStorage.getItem(ACTIVE_ACCOUNT_KEY) || null; }
  function _setActiveId(id) {
    if (id) localStorage.setItem(ACTIVE_ACCOUNT_KEY, id);
    else localStorage.removeItem(ACTIVE_ACCOUNT_KEY);
  }

  function getActiveAccount() {
    const id = _getActiveId();
    if (!id) return null;
    return _getAccounts().find(a => a.id === id) || null;
  }

  // Backwards-compat: getAccess uses ACTIVE account's token
  const getAccess = () => {
    const acc = getActiveAccount();
    if (acc) return acc.access_token;
    return localStorage.getItem(ACCESS_KEY);  // fallback for legacy users
  };
  const getRefresh = () => {
    const acc = getActiveAccount();
    if (acc) return acc.refresh_token;
    return localStorage.getItem(REFRESH_KEY);
  };

  // saveTokens — adds/updates account in multi-account list
  const saveTokens = (pair, userInfo) => {
    if (!pair || !pair.access_token) return;
    // Legacy single-token storage
    localStorage.setItem(ACCESS_KEY, pair.access_token);
    if (pair.refresh_token) localStorage.setItem(REFRESH_KEY, pair.refresh_token);
    // Multi-account storage
    if (userInfo && userInfo.id) {
      const accounts = _getAccounts();
      const existing = accounts.findIndex(a => a.id === userInfo.id);
      const account = {
        id: userInfo.id,
        email: userInfo.email,
        name: userInfo.name || userInfo.email,
        access_token: pair.access_token,
        refresh_token: pair.refresh_token,
        workspace: (userInfo.workspaces || [])[0] || null,
        ws_list: userInfo.workspaces || [],
        savedAt: new Date().toISOString(),
      };
      if (existing >= 0) accounts[existing] = account;
      else accounts.push(account);
      _saveAccounts(accounts);
      _setActiveId(userInfo.id);
    }
  };

  const clearTokens = () => {
    // Clear legacy + remove active account from multi-account
    localStorage.removeItem(ACCESS_KEY);
    localStorage.removeItem(REFRESH_KEY);
    const id = _getActiveId();
    if (id) {
      const accounts = _getAccounts().filter(a => a.id !== id);
      _saveAccounts(accounts);
      // Switch to next account if any
      _setActiveId(accounts.length > 0 ? accounts[0].id : null);
    }
  };

  // Public: switch active account
  function switchAccount(accountId) {
    const accounts = _getAccounts();
    const acc = accounts.find(a => a.id === accountId);
    if (!acc) throw new Error('Account not found: ' + accountId);
    _setActiveId(accountId);
    // Update legacy keys for backwards compat
    localStorage.setItem(ACCESS_KEY, acc.access_token);
    if (acc.refresh_token) localStorage.setItem(REFRESH_KEY, acc.refresh_token);
    return acc;
  }
  function listAccounts() { return _getAccounts(); }

  async function _fetch(path, { method = 'GET', body, headers = {}, noAuth = false } = {}) {
    const url = API_BASE + path;
    const opts = {
      method,
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json', ...headers },
    };
    if (!noAuth) {
      const tok = getAccess();
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

    if (res.status === 401 && !noAuth && getRefresh()) {
      const ok = await refresh();
      if (ok) {
        opts.headers.Authorization = 'Bearer ' + getAccess();
        res = await fetch(url, opts);
      }
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

  async function login(email, password) {
    // Trim trailing/leading whitespace (common copy-paste mistake)
    const cleanEmail = (email || '').trim().toLowerCase();
    const cleanPass = (password || '').trim();
    if (!cleanEmail || !cleanPass) {
      const e = new Error('Email và mật khẩu không được để trống');
      e.status = 400;
      throw e;
    }
    try {
      const pair = await _fetch('/auth/login', { method: 'POST', body: { email: cleanEmail, password: cleanPass }, noAuth: true });
      // Pass user info from login response to saveTokens for multi-account
      saveTokens(pair, pair.user || null);
      console.log('[ZeniAPI] login OK:', cleanEmail);
      return pair;
    } catch (e) {
      // Rich diagnostic — show backend's actual error
      console.warn('[ZeniAPI] login fail:', { status: e.status, body: e.body, msg: e.message });
      if (e.status === 429) {
        e.message = 'Quá nhiều lần đăng nhập sai. Vui lòng đợi 15 phút.';
      } else if (e.status === 401) {
        e.message = 'Email hoặc mật khẩu không đúng. Lưu ý: password phân biệt hoa/thường + ký tự đặc biệt.';
      } else if (!e.status) {
        e.message = 'Không kết nối được backend. Kiểm tra mạng + thử lại.';
      }
      throw e;
    }
  }

  async function refresh() {
    const rt = getRefresh();
    if (!rt) return false;
    try {
      const pair = await _fetch('/auth/refresh', { method: 'POST', body: { refresh_token: rt }, noAuth: true });
      saveTokens(pair);
      return true;
    } catch {
      clearTokens();
      return false;
    }
  }

  async function logoutAPI() {
    const rt = getRefresh();
    if (rt) {
      try { await _fetch('/auth/logout', { method: 'POST', body: { refresh_token: rt } }); }
      catch { /* swallow */ }
    }
    clearTokens();
  }

  async function me() { return await _fetch('/auth/me'); }

  const ZeniAPI = {
    // Auth
    login, logout: logoutAPI, me, refresh,
    isAuthed: () => !!getAccess(),
    clearTokens,
    // Multi-account (NEW v107)
    switchAccount, listAccounts, getActiveAccount,

    // Workspaces
    listWorkspaces: () => _fetch('/workspaces'),

    // L1 Compute
    listProjects: (ws) => _fetch('/projects?ws=' + encodeURIComponent(ws)),
    deployProject: (ws, data) => _fetch('/projects?ws=' + encodeURIComponent(ws), { method: 'POST', body: data }),
    deleteProject: (ws, id) => _fetch('/projects/' + id + '?ws=' + encodeURIComponent(ws), { method: 'DELETE' }),

    // L2 Data
    listDatabases: (ws) => _fetch('/data/databases?ws=' + encodeURIComponent(ws)),
    runQuery: (ws, body) => _fetch('/data/query?ws=' + encodeURIComponent(ws), { method: 'POST', body }),

    // L3 AI
    listModels: () => _fetch('/ai/models'),
    listAgents: (ws) => _fetch('/ai/agents?ws=' + encodeURIComponent(ws)),
    createAgent: (ws, body) => _fetch('/ai/agents?ws=' + encodeURIComponent(ws), { method: 'POST', body }),
    toggleAgent: (ws, id) => _fetch('/ai/agents/' + id + '/toggle?ws=' + encodeURIComponent(ws), { method: 'PATCH' }),
    complete: (ws, body) => _fetch('/ai/complete?ws=' + encodeURIComponent(ws), { method: 'POST', body }),

    // L4 Automation
    listConnectors: (ws) => _fetch('/automation/connectors?ws=' + encodeURIComponent(ws)),
    fireEvent: (ws, body) => _fetch('/automation/events/fire?ws=' + encodeURIComponent(ws), { method: 'POST', body }),
    connectorCatalog: () => _fetch('/automation/catalog'),

    // L5 Identity
    listSecrets: (ws) => _fetch('/identity/secrets?ws=' + encodeURIComponent(ws)),
    createSecret: (ws, body) => _fetch('/identity/secrets?ws=' + encodeURIComponent(ws), { method: 'POST', body }),
    rotateSecret: (ws, id) => _fetch('/identity/secrets/' + id + '/rotate?ws=' + encodeURIComponent(ws), { method: 'POST' }),
    revealSecret: (ws, id) => _fetch('/identity/secrets/' + id + '/reveal?ws=' + encodeURIComponent(ws)),
    deleteSecret: (ws, id) => _fetch('/identity/secrets/' + id + '?ws=' + encodeURIComponent(ws), { method: 'DELETE' }),
    identityFlow: (ws, body) => _fetch('/identity/flow?ws=' + encodeURIComponent(ws), { method: 'POST', body }),

    // L6 Web3
    listContracts: (ws) => _fetch('/web3/contracts?ws=' + encodeURIComponent(ws)),
    executeWeb3: (ws, body) => _fetch('/web3/execute?ws=' + encodeURIComponent(ws), { method: 'POST', body }),

    // Members
    listMembers: (ws) => _fetch('/members' + (ws ? '?ws=' + encodeURIComponent(ws) : '')),
    inviteMember: (body) => _fetch('/members/invite', { method: 'POST', body }),

    // Audit / Billing
    listAudit: (opts = {}) => {
      const p = new URLSearchParams(opts).toString();
      return _fetch('/audit' + (p ? '?' + p : ''));
    },
    billingSummary: (opts = {}) => {
      const p = new URLSearchParams(opts).toString();
      return _fetch('/billing/summary' + (p ? '?' + p : ''));
    },
    billingByEntity: () => _fetch('/billing/by-entity'),
  };

  window.ZeniAPI = ZeniAPI;

  /* ─── Patch vào UI demo ──────────────────────────────────────────
   * Demo IIFE sau script này sẽ gán window.doLogin / window.doLogout.
   * Ta đợi DOMContentLoaded + requestAnimationFrame để đảm bảo đã gán,
   * rồi override.
   */
  function attachPatches() {
    if (typeof window.doLogin !== 'function') {
      setTimeout(attachPatches, 50);
      return;
    }

    const originalLogin  = window.doLogin;
    const originalLogout = window.doLogout;

    window.doLogin = async function () {
      const emailEl = document.getElementById('login-email');
      const passEl  = document.getElementById('login-pass');
      const errEl   = document.getElementById('login-err');
      if (!emailEl || !passEl) { return originalLogin && originalLogin(); }

      const email = emailEl.value.trim().toLowerCase();
      const pass  = passEl.value;
      if (errEl) errEl.classList.remove('show');

      if (!email || !pass) {
        if (errEl) { errEl.textContent = 'Nhập đủ email và mật khẩu.'; errEl.classList.add('show'); }
        return;
      }

      try {
        await ZeniAPI.login(email, pass);
        const user = await ZeniAPI.me();
        window.__ZENI_REAL_USER = user;

        const mappedUser = {
          id: user.id,
          email: user.email,
          name: user.name,
          pass: '',
          role: user.role,
          avatar: user.name.split(' ').map(w => w[0]).join('').slice(0, 2).toUpperCase(),
          ws: (user.workspaces && user.workspaces.length) ? user.workspaces : [],
        };
        // Map backend user → state shape demo đang dùng
        if (window.state) {
          window.state.currentUser = mappedUser;
          window.state.currentWs = mappedUser.ws[0];
          if (typeof window.saveSession === 'function') window.saveSession();
        }
        // Populate WORKSPACES dict (frontend uses WORKSPACES[id] in render)
        // Use workspace_details if backend provides, else workspaces array
        try {
          const wsDetails = user.workspace_details || (user.workspaces || []).map(id => ({id, name: id}));
          if (window.WORKSPACES) {
            wsDetails.forEach(w => {
              if (!window.WORKSPACES[w.id]) {
                window.WORKSPACES[w.id] = {
                  id: w.id, name: w.name || w.id, role: 'Owner',
                  code: (w.id || '').slice(0,8).toUpperCase(),
                  color: 'var(--crown)',
                };
              }
            });
          }
        } catch (e) { console.warn('[ZeniAPI] populate WORKSPACES failed:', e); }
        // Persist session to localStorage so demo IIFE (which uses state closure) picks it up on reload
        try {
          localStorage.setItem('zeniCloud_session_v1', JSON.stringify({
            userId: mappedUser.id, currentWs: mappedUser.ws[0], ts: Date.now(),
            user: mappedUser,
          }));
        } catch {}

        if (typeof window.bootApp === 'function') {
          window.bootApp();
        } else {
          // Fallback: IIFE demo chưa expose bootApp → manual UI transition
          const loginEl = document.getElementById('login');
          const appEl   = document.getElementById('app');
          if (loginEl) loginEl.classList.add('hidden');
          if (appEl)   appEl.classList.add('active');
        }
        if (typeof window.toast === 'function') window.toast('✓ Đăng nhập backend thật · ' + user.email, 'ok');
      } catch (err) {
        console.warn('[ZeniAPI] login thất bại:', err);
        if (err.network) {
          // Backend offline → rơi về demo mode (USERS cục bộ)
          if (typeof window.toast === 'function') window.toast('⚠ Backend offline — dùng demo mode', 'warn');
          return originalLogin && originalLogin();
        }
        if (errEl) {
          errEl.textContent = err.message || 'Đăng nhập thất bại';
          errEl.classList.add('show');
        }
      }
    };

    window.doLogout = async function () {
      try { await ZeniAPI.logout(); } catch { /* ignore */ }
      return originalLogout && originalLogout();
    };

    // Nếu đã có JWT trong localStorage → auto-verify và tự đăng nhập
    if (ZeniAPI.isAuthed()) {
      ZeniAPI.me()
        .then(user => {
          window.__ZENI_REAL_USER = user;
          if (!window.state || !window.state.currentUser) {
            window.state = window.state || {};
            window.state.currentUser = {
              id: user.id, email: user.email, name: user.name, pass: '',
              role: user.role,
              avatar: user.name.split(' ').map(w => w[0]).join('').slice(0, 2).toUpperCase(),
              ws: (user.workspaces && user.workspaces.length) ? user.workspaces : [],
            };
            window.state.currentWs = window.state.currentUser.ws[0];
            if (typeof window.saveSession === 'function') window.saveSession();
            if (typeof window.bootApp === 'function') window.bootApp();
          }
          console.log('[ZeniAPI] phiên backend hợp lệ:', user.email);
        })
        .catch(err => {
          if (err.network) {
            console.warn('[ZeniAPI] backend offline — giữ phiên demo');
          } else {
            console.warn('[ZeniAPI] phiên cũ không hợp lệ, xoá:', err.message);
            clearTokens();
          }
        });
    }

    console.log('[ZeniAPI] doLogin / doLogout đã patch — backend:', API_BASE);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', attachPatches);
  } else {
    attachPatches();
  }
})();
