/**
 * zeni list — Liệt kê projects trong workspace
 */
import { apiCall, requireAuth, requireWorkspace } from '../config.js';

export async function listProjects() {
  requireAuth();
  const ws = requireWorkspace();
  const projects = await apiCall('GET', `/projects?ws=${encodeURIComponent(ws)}`);
  if (!Array.isArray(projects) || projects.length === 0) {
    console.log('Chưa có project nào trong workspace ' + ws);
    console.log('Run: zeni deploy');
    return;
  }
  console.log(`\x1b[36m▸\x1b[0m Projects in ${ws}:`);
  console.log('');
  console.log('  NAME                          STATUS       REGION              URL');
  console.log('  ----                          ------       ------              ---');
  projects.forEach(p => {
    const name = (p.name || '?').padEnd(28).slice(0, 28);
    const status = (p.status || '?').padEnd(12).slice(0, 12);
    const region = (p.region || '?').padEnd(20).slice(0, 20);
    const url = p.domain || '(deploying...)';
    const statusColor = p.status === 'running' ? '\x1b[32m' : (p.status === 'failed' ? '\x1b[31m' : '\x1b[33m');
    console.log(`  ${name}  ${statusColor}${status}\x1b[0m ${region} ${url}`);
  });
}
