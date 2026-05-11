/**
 * zeni domain — Map custom domain to project
 *
 * Usage:
 *   zeni domain add <domain>             Add domain → returns DNS instructions
 *   zeni domain status <domain>          Poll cert + DNS status
 *   zeni domain list                     List domains for project
 *   zeni domain remove <domain>          Remove mapping
 *
 * Backend: v169 endpoint /projects/{id}/domain — fully automated HTTPS LB
 * (no Search Console verification needed).
 */
import { apiCall, requireWorkspace, readProjectConfig } from '../config.js';

function _findProjectId() {
  const proj = readProjectConfig();
  if (proj && proj.project_id) return proj.project_id;
  const flag = process.argv.find(a => a.startsWith('--project='));
  if (flag) return flag.split('=')[1];
  return null;
}

export async function domain(args) {
  const sub = args[0];
  const ws = requireWorkspace();
  const project_id = _findProjectId();
  if (!project_id) {
    console.error('\x1b[31m✗\x1b[0m Không tìm thấy project_id (add vào zeni.json hoặc --project=)');
    process.exit(1);
  }

  switch (sub) {
    case 'add':
      return _add(ws, project_id, args[1]);
    case 'status':
    case 'check':
      return _status(ws, project_id, args[1]);
    case 'list':
    case 'ls':
      return _list(ws, project_id);
    case 'remove':
    case 'rm':
      return _remove(ws, project_id, args[1]);
    default:
      console.error('Usage:');
      console.error('  zeni domain add <domain>            Map custom domain');
      console.error('  zeni domain status <domain>         Check provisioning status');
      console.error('  zeni domain list                    List domains');
      console.error('  zeni domain remove <domain>         Remove domain');
      process.exit(1);
  }
}

async function _add(ws, project_id, dom) {
  if (!dom) { console.error('Usage: zeni domain add <domain>'); process.exit(1); }
  console.log(`\x1b[36m→\x1b[0m Mapping ${dom} → Zeni Cloud...`);
  const result = await apiCall('POST', `/projects/${project_id}/domain?ws=${ws}`, { domain: dom });

  if (result.state === 'FAILED') {
    console.error(`\x1b[31m✗\x1b[0m ${result.error || 'Unknown error'}`);
    process.exit(1);
  }

  console.log(`\x1b[32m✓\x1b[0m Zeni Cloud đã setup infrastructure cho ${dom}\n`);
  console.log(`  State: \x1b[33m${result.state}\x1b[0m`);
  console.log(`  LB IP: ${result.lb_ip}`);
  console.log(`  SSL:   ${result.cert_status} (Google-managed)\n`);
  console.log(`\x1b[1mBƯỚC TIẾP THEO bên anh:\x1b[0m\n`);
  for (const rec of result.dns_records_to_add || []) {
    console.log(`  ┌─ Add DNS record tại registrar ─────────┐`);
    console.log(`  │ Type:  ${rec.type}                              │`);
    console.log(`  │ Host:  ${(rec.name || '').split('.')[0]}                            │`);
    console.log(`  │ Value: ${rec.value}                  │`);
    console.log(`  │ TTL:   ${rec.ttl || 300}                              │`);
    console.log(`  └─────────────────────────────────────────┘\n`);
  }
  console.log(`Đợi 5-30 phút DNS propagate + Google cấp SSL.\n`);
  console.log(`Check status: \x1b[36mzeni domain status ${dom}\x1b[0m`);
}

async function _status(ws, project_id, dom) {
  if (!dom) { console.error('Usage: zeni domain status <domain>'); process.exit(1); }
  const result = await apiCall('GET', `/projects/${project_id}/domain/${dom}/status?ws=${ws}`);

  const stateColors = {
    'PENDING_DNS':       '\x1b[33m',   // yellow
    'PROVISIONING_SSL':  '\x1b[36m',   // cyan
    'LIVE':              '\x1b[32m',   // green
  };
  const c = stateColors[result.state] || '';
  console.log(`Domain: ${dom}`);
  console.log(`State:  ${c}${result.state}\x1b[0m`);
  console.log(`DNS:    ${result.dns_resolved ? '\x1b[32m✓ resolved\x1b[0m' : '\x1b[31m✗ not yet\x1b[0m'} (target: ${result.dns_target})`);
  console.log(`Cert:   ${result.cert_status}`);
  if (result.live_url) {
    console.log(`\n\x1b[32m🚀 LIVE:\x1b[0m ${result.live_url}`);
  }
}

async function _list(ws, project_id) {
  const result = await apiCall('GET', `/projects/${project_id}/domains?ws=${ws}`);
  const domains = result.domains || [];
  if (!domains.length) {
    console.log('(no custom domains)');
    return;
  }
  console.log(`\x1b[36m${domains.length} domain(s):\x1b[0m`);
  for (const d of domains) {
    console.log(`  ${d.domain} ${d.ready ? '\x1b[32m✓ live\x1b[0m' : '\x1b[33m...provisioning\x1b[0m'}`);
  }
}

async function _remove(ws, project_id, dom) {
  if (!dom) { console.error('Usage: zeni domain remove <domain>'); process.exit(1); }
  await apiCall('DELETE', `/projects/${project_id}/domain/${dom}?ws=${ws}`);
  console.log(`\x1b[32m✓\x1b[0m Removed ${dom}`);
}
