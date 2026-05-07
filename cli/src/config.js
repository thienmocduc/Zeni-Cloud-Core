/**
 * Zeni CLI — Config management (multi-project support v0.2)
 *
 * Architecture:
 *   ~/.zeni/tokens.json      → Map of alias → token (global, secrets)
 *   ~/.zeni/config.json      → Default workspace + active alias (legacy)
 *   ./zeni.json              → Per-project config (workspace + token_alias)
 *
 * Token resolution priority:
 *   1. ZENI_TOKEN env var
 *   2. ./zeni.json `token_alias` → ~/.zeni/tokens.json[alias]
 *   3. ~/.zeni/config.json `token` (legacy single-token)
 */
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';

const CONFIG_DIR = path.join(os.homedir(), '.zeni');
const CONFIG_FILE = path.join(CONFIG_DIR, 'config.json');
const TOKENS_FILE = path.join(CONFIG_DIR, 'tokens.json');
const PROJECT_FILE = 'zeni.json';
export const API_BASE = process.env.ZENI_API || 'https://zenicloud.io/api/v1';

function _ensureDir() {
  if (!fs.existsSync(CONFIG_DIR)) {
    fs.mkdirSync(CONFIG_DIR, { recursive: true, mode: 0o700 });
  }
}

export function readConfig() {
  try {
    if (!fs.existsSync(CONFIG_FILE)) return {};
    return JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf-8'));
  } catch (e) { return {}; }
}

export function writeConfig(data) {
  _ensureDir();
  fs.writeFileSync(CONFIG_FILE, JSON.stringify(data, null, 2), { mode: 0o600 });
}

// Tokens map (alias → token)
export function readTokens() {
  try {
    if (!fs.existsSync(TOKENS_FILE)) return {};
    return JSON.parse(fs.readFileSync(TOKENS_FILE, 'utf-8'));
  } catch (e) { return {}; }
}

export function writeTokens(map) {
  _ensureDir();
  fs.writeFileSync(TOKENS_FILE, JSON.stringify(map, null, 2), { mode: 0o600 });
}

export function setTokenAlias(alias, token, workspace) {
  const tokens = readTokens();
  tokens[alias] = { token, workspace, savedAt: new Date().toISOString() };
  writeTokens(tokens);
}

export function getTokenByAlias(alias) {
  const tokens = readTokens();
  return tokens[alias] || null;
}

// Per-project zeni.json (in CWD or any parent dir)
export function readProjectConfig(startDir = process.cwd()) {
  let dir = startDir;
  while (dir && dir !== path.dirname(dir)) {
    const cfgPath = path.join(dir, PROJECT_FILE);
    if (fs.existsSync(cfgPath)) {
      try {
        const cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf-8'));
        cfg.__path = cfgPath;
        return cfg;
      } catch (e) { return null; }
    }
    dir = path.dirname(dir);
  }
  return null;
}

// Resolve token + workspace from environment
export function getToken() {
  // Priority 1: ENV
  if (process.env.ZENI_TOKEN) return process.env.ZENI_TOKEN;
  // Priority 2: per-project zeni.json with token_alias
  const proj = readProjectConfig();
  if (proj && proj.token_alias) {
    const aliased = getTokenByAlias(proj.token_alias);
    if (aliased && aliased.token) return aliased.token;
  }
  // Priority 3: legacy single-token in config.json
  return readConfig().token;
}

export function getWorkspace() {
  // Priority 1: ENV
  if (process.env.ZENI_WORKSPACE) return process.env.ZENI_WORKSPACE;
  // Priority 2: per-project zeni.json
  const proj = readProjectConfig();
  if (proj && proj.workspace) return proj.workspace;
  // Priority 3: from token alias
  if (proj && proj.token_alias) {
    const aliased = getTokenByAlias(proj.token_alias);
    if (aliased && aliased.workspace) return aliased.workspace;
  }
  // Priority 4: legacy config
  return readConfig().workspace;
}

export function requireAuth() {
  const token = getToken();
  if (!token) {
    console.error('\x1b[31m✗\x1b[0m Chưa đăng nhập. Chạy: zeni login');
    process.exit(1);
  }
  return token;
}

export function requireWorkspace() {
  const ws = getWorkspace();
  if (!ws) {
    console.error('\x1b[31m✗\x1b[0m Chưa chọn workspace. Chạy: zeni login (chọn lại) hoặc set ZENI_WORKSPACE env');
    process.exit(1);
  }
  return ws;
}

export async function apiCall(method, path, body) {
  const token = requireAuth();
  const url = path.startsWith('http') ? path : API_BASE + path;
  const opts = {
    method,
    headers: {
      'Authorization': `Bearer ${token}`,
      'Accept': 'application/json',
    },
  };
  if (body !== undefined) {
    if (body instanceof FormData) {
      opts.body = body;
    } else {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
  }
  const r = await fetch(url, opts);
  let data = null;
  try { data = await r.json(); } catch (e) { data = await r.text(); }
  if (!r.ok) {
    const msg = (data && data.detail) || `HTTP ${r.status}`;
    const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    err.status = r.status;
    err.body = data;
    throw err;
  }
  return data;
}
