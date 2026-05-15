/**
 * zeni env — Manage project environment variables
 *
 * Usage:
 *   zeni env list                       List env vars (masked)
 *   zeni env set KEY=value [KEY2=v2]    Set one or more
 *   zeni env unset KEY                  Delete a variable
 *   zeni env pull > .env.production     Export to file
 *
 * Reads project from ./zeni.json (or --project flag).
 */
import fs from 'node:fs';
import { apiCall, requireWorkspace, readProjectConfig } from '../config.js';

function _findProjectId() {
  // Try ./zeni.json first
  const proj = readProjectConfig();
  if (proj && proj.project_id) return proj.project_id;
  // CLI flag --project=ID
  const flag = process.argv.find(a => a.startsWith('--project='));
  if (flag) return flag.split('=')[1];
  return null;
}

async function _resolveProject(ws) {
  const id = _findProjectId();
  if (id) return id;
  // Fallback: list projects, ask user to pick (for now error)
  console.error('\x1b[31m✗\x1b[0m Không tìm thấy project_id. Add vào zeni.json hoặc dùng --project=<UUID>.');
  process.exit(1);
}

export async function env(args) {
  const sub = args[0];
  const ws = requireWorkspace();
  const project_id = await _resolveProject(ws);

  switch (sub) {
    case 'list':
    case 'ls':
      return _list(ws, project_id);
    case 'set':
      return _set(ws, project_id, args.slice(1));
    case 'unset':
    case 'rm':
      return _unset(ws, project_id, args.slice(1));
    case 'pull':
      return _pull(ws, project_id);
    default:
      console.error('Usage:');
      console.error('  zeni env list');
      console.error('  zeni env set KEY=value [KEY2=v2 ...]');
      console.error('  zeni env unset KEY');
      console.error('  zeni env pull > .env.production');
      process.exit(1);
  }
}

async function _list(ws, project_id) {
  const result = await apiCall('GET', `/projects/${project_id}/env?ws=${ws}`);
  const keys = result.env_keys || [];
  if (!keys.length) {
    console.log('(không có env vars)');
    return;
  }
  console.log(`\x1b[36m${keys.length} env vars:\x1b[0m`);
  for (const k of keys) {
    console.log(`  ${k}`);
  }
}

async function _set(ws, project_id, kvs) {
  if (!kvs.length) {
    console.error('Usage: zeni env set KEY=value [KEY2=v2 ...]');
    process.exit(1);
  }
  const env_vars = {};
  for (const kv of kvs) {
    const eq = kv.indexOf('=');
    if (eq < 0) {
      console.error(`Invalid format: ${kv} (expected KEY=value)`);
      process.exit(1);
    }
    env_vars[kv.slice(0, eq)] = kv.slice(eq + 1);
  }
  const result = await apiCall('POST', `/projects/${project_id}/env?ws=${ws}`, { env_vars });
  console.log(`\x1b[32m✓\x1b[0m Set ${Object.keys(env_vars).length} env vars`);
  console.log(`  ${result.message || 'Cloud Run revision will update with new env.'}`);
}

async function _unset(ws, project_id, keys) {
  if (!keys.length) {
    console.error('Usage: zeni env unset KEY');
    process.exit(1);
  }
  // POST với empty value = remove (depending on backend impl)
  // For now: POST với null
  const env_vars = {};
  for (const k of keys) env_vars[k] = null;
  await apiCall('POST', `/projects/${project_id}/env?ws=${ws}`, { env_vars });
  console.log(`\x1b[32m✓\x1b[0m Unset: ${keys.join(', ')}`);
}

async function _pull(ws, project_id) {
  const result = await apiCall('GET', `/projects/${project_id}/env?ws=${ws}`);
  const keys = result.env_keys || [];
  for (const k of keys) {
    // Note: backend returns only KEYS (not values for security)
    // For full pull, would need a separate authorized endpoint
    console.log(`${k}=<from-zeni-secret-manager>`);
  }
}
