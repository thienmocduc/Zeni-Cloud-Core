/*
 * ZENI CLOUD CORE · Real-data adapter
 *
 * Loads AFTER index.html IIFE finishes. Replaces demo state.* arrays with
 * real API data via ZeniAPI. Reactive: re-fetches whenever workspace switches
 * or view changes. Falls back gracefully to demo if backend offline.
 *
 *   - Workspaces  → GET /api/v1/workspaces
 *   - Projects    → GET /api/v1/projects?ws=<ws>
 *   - Tables      → GET /api/v1/data/tables?ws=<ws>
 *   - Connectors  → GET /api/v1/automation/connectors?ws=<ws>
 *   - Secrets     → GET /api/v1/identity/secrets?ws=<ws>
 *   - Web3 stack  → GET /api/v1/web3/zeni-stack (Polygon mainnet live)
 *
 *   Patches window.state, then calls window.setView(state.currentView)
 *   to trigger rerender if the IIFE exposed setView.
 */
(function () {
  'use strict';
  if (!window.ZeniAPI) {
    console.warn('[zeni-realdata] ZeniAPI missing — abort');
    return;
  }

  const log = (...a) => console.log('[real]', ...a);
  const warn = (...a) => console.warn('[real]', ...a);

  // Map backend project shape → demo state.projects shape
  function mapProject(p) {
    return {
      id: p.id,
      ws: p.workspace_id,
      name: p.name,
      region: p.region,
      runtime: p.runtime,
      status: p.status,
      instances: p.instances || 0,
      mem: p.memory || '—',
      cpu: p.cpu || '—',
      domain: p.domain || '',
      lastDeploy: p.last_deploy ? new Date(p.last_deploy).getTime() : Date.now(),
      version: p.version || '—',
      gitRef: p.git_ref || 'main',
      image: p.image || '',
      cloud_run_service: p.cloud_run_service || '',
    };
  }

  function mapConnector(c) {
    return {
      id: c.id,
      ws: c.workspace_id,
      type: c.type,
      status: c.status,
      events_7d: c.events_7d || 0,
      config: c.config || {},
    };
  }

  function mapSecret(s) {
    return {
      id: s.id,
      ws: s.workspace_id,
      name: s.name,
      env: s.env,
      masked: s.masked || '••••••••',
      rotations: s.rotations || 0,
      updated_at: s.updated_at,
    };
  }

  // Fetch all data for a workspace in parallel
  async function fetchWorkspaceData(wsId) {
    const tasks = await Promise.allSettled([
      ZeniAPI.listProjects(wsId),
      fetch('/api/v1/data/tables?ws=' + encodeURIComponent(wsId), {
        headers: { Authorization: 'Bearer ' + (localStorage.getItem('zeni.jwt.access') || '') }
      }).then(r => r.ok ? r.json() : { tables: [] }),
      ZeniAPI.listConnectors(wsId),
      ZeniAPI.listSecrets(wsId),
      ZeniAPI.listContracts(wsId),
    ]);
    return {
      projects:    tasks[0].status === 'fulfilled' ? (tasks[0].value || []).map(mapProject) : [],
      tables:      tasks[1].status === 'fulfilled' ? (tasks[1].value.tables || []) : [],
      connectors:  tasks[2].status === 'fulfilled' ? (tasks[2].value || []).map(mapConnector) : [],
      secrets:     tasks[3].status === 'fulfilled' ? (tasks[3].value || []).map(mapSecret) : [],
      contracts:   tasks[4].status === 'fulfilled' ? (tasks[4].value || []) : [],
    };
  }

  async function fetchWorkspacesList() {
    try {
      const list = await ZeniAPI.listWorkspaces();
      const map = {};
      for (const w of list) {
        map[w.id] = {
          id: w.id, code: w.code, name: w.name, role: 'Owner',
          color: '#A855F7', sector: w.tagline || '',
        };
      }
      return map;
    } catch (e) { warn('listWorkspaces failed:', e); return null; }
  }

  async function fetchWeb3Stack() {
    try {
      const r = await fetch('/api/v1/web3/zeni-stack', {
        headers: { Authorization: 'Bearer ' + (localStorage.getItem('zeni.jwt.access') || '') }
      });
      if (!r.ok) return null;
      return await r.json();
    } catch (e) { return null; }
  }

  // Bootstrap: after auth ready, populate state with real data
  async function bootstrap() {
    if (!ZeniAPI.isAuthed()) return;
    log('bootstrap start');

    // 1) Workspaces
    const wsMap = await fetchWorkspacesList();
    if (wsMap && Object.keys(wsMap).length) {
      // Merge with demo WORKSPACES (preserve color from demo if backend doesn't have)
      if (window.WORKSPACES) {
        for (const id of Object.keys(wsMap)) {
          if (window.WORKSPACES[id]) {
            wsMap[id].color = window.WORKSPACES[id].color || wsMap[id].color;
            wsMap[id].sector = window.WORKSPACES[id].sector || wsMap[id].sector;
          }
          window.WORKSPACES[id] = wsMap[id];
        }
      }
      window.__zeniRealWorkspaces = wsMap;
      log('workspaces:', Object.keys(wsMap).join(', '));
    }

    // 2) Pick current workspace — never hardcode a tenant slug.
    //    Always derive from the authenticated user's owned workspaces.
    const userWs = (window.__ZENI_REAL_USER && window.__ZENI_REAL_USER.workspaces) || [];
    let currentWs = (window.state && window.state.currentWs) || userWs[0] || null;
    if (userWs.length) {
      if (!userWs.includes(currentWs)) {
        currentWs = userWs[0];
      }
    }
    if (!currentWs) {
      warn('no workspace available for user — abort bootstrap');
      return;
    }

    // 3) Fetch all per-workspace data
    const data = await fetchWorkspaceData(currentWs);
    log('workspace', currentWs, 'projects:', data.projects.length, 'tables:', data.tables.length,
        'connectors:', data.connectors.length, 'secrets:', data.secrets.length);

    // 4) Web3 stack (Polygon live — only need fetch once)
    const web3 = await fetchWeb3Stack();
    if (web3) {
      window.__zeniWeb3Stack = web3;
      log('web3 stack: ZENI', web3.ZENI_TOKEN && web3.ZENI_TOKEN.total_supply, 'block', web3.chain_status && web3.chain_status.block_number);
    }

    // 5) Patch state
    if (window.state) {
      window.state.projects = data.projects;
      window.state.connectors = data.connectors;
      window.state.secrets = data.secrets;
      window.state.tables = data.tables;
      window.state.web3Stack = web3;
      if (window.saveState) window.saveState();
    }

    // 6) Trigger rerender via setView if available
    if (window.setView && window.state && window.state.currentView) {
      try { window.setView(window.state.currentView); } catch (e) { warn('setView failed:', e); }
    } else if (window.refreshSidebar) {
      try { window.refreshSidebar(); } catch (e) {}
    }

    if (window.toast) window.toast('✓ Đã đồng bộ dữ liệu thật từ Zeni Cloud', 'ok');
  }

  // Re-bootstrap on workspace switch
  function hookWorkspaceSwitch() {
    let lastWs = window.state && window.state.currentWs;
    setInterval(() => {
      if (!window.state || !ZeniAPI.isAuthed()) return;
      if (window.state.currentWs !== lastWs) {
        lastWs = window.state.currentWs;
        log('workspace changed →', lastWs, '· refetching');
        fetchWorkspaceData(lastWs).then((data) => {
          window.state.projects = data.projects;
          window.state.connectors = data.connectors;
          window.state.secrets = data.secrets;
          window.state.tables = data.tables;
          if (window.saveState) window.saveState();
          if (window.setView && window.state.currentView) {
            try { window.setView(window.state.currentView); } catch (e) {}
          }
        });
      }
    }, 1500);
  }

  // Expose helpers for view renderers (so they can refresh themselves)
  window.ZeniRealData = {
    bootstrap,
    fetchWorkspaceData,
    fetchWorkspacesList,
    fetchWeb3Stack,
  };

  // Auto-bootstrap when the IIFE exposes window.state and user is authed
  function tryBootstrap(retries) {
    if (window.state && window.bootApp && ZeniAPI.isAuthed()) {
      bootstrap().then(hookWorkspaceSwitch).catch((e) => warn('bootstrap error:', e));
      return;
    }
    if (retries > 0) setTimeout(() => tryBootstrap(retries - 1), 300);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => tryBootstrap(40));
  } else {
    tryBootstrap(40);
  }

  // Also bootstrap right after login (when user clicks login button)
  const _origPatch = window.ZeniAPI.login;
  window.ZeniAPI.login = async function (email, pass) {
    const r = await _origPatch(email, pass);
    setTimeout(() => bootstrap().then(hookWorkspaceSwitch), 500);
    return r;
  };
})();
