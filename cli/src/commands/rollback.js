/**
 * zeni rollback — 1-click rollback to previous revision
 *
 * Usage:
 *   zeni revisions                          List revisions
 *   zeni rollback                           Rollback to PREVIOUS revision
 *   zeni rollback <revision-name>           Rollback to specific revision
 *
 * Vercel pattern — instant traffic flip, 0 downtime.
 */
import { apiCall, requireWorkspace, readProjectConfig } from '../config.js';

function _findProjectId() {
  const proj = readProjectConfig();
  if (proj && proj.project_id) return proj.project_id;
  const flag = process.argv.find(a => a.startsWith('--project='));
  if (flag) return flag.split('=')[1];
  return null;
}

export async function revisions(args) {
  const ws = requireWorkspace();
  const project_id = _findProjectId();
  if (!project_id) { console.error('No project_id'); process.exit(1); }
  const result = await apiCall('GET', `/projects/${project_id}/revisions?ws=${ws}`);
  const revs = result.revisions || [];
  if (!revs.length) {
    console.log('(no revisions)');
    return;
  }
  console.log(`\x1b[36m${revs.length} revision(s) cho ${result.service_name}:\x1b[0m\n`);
  for (const r of revs) {
    const traffic = r.traffic_percent || 0;
    const marker = traffic === 100 ? '\x1b[32m● LIVE\x1b[0m' : (traffic > 0 ? `\x1b[33m${traffic}%\x1b[0m` : '\x1b[90m  -\x1b[0m');
    const tag = r.tag ? `\x1b[35m[${r.tag}]\x1b[0m` : '';
    console.log(`  ${marker}  ${r.name.padEnd(35)} ${(r.created_at || '').slice(0, 19)} ${tag}`);
  }
}

export async function rollback(args) {
  const ws = requireWorkspace();
  const project_id = _findProjectId();
  if (!project_id) { console.error('No project_id'); process.exit(1); }

  let targetRevision = args[0];

  // No arg → auto-pick PREVIOUS revision (2nd most recent that's not currently 100%)
  if (!targetRevision) {
    const listResult = await apiCall('GET', `/projects/${project_id}/revisions?ws=${ws}`);
    const revs = listResult.revisions || [];
    const current = revs.find(r => r.traffic_percent === 100);
    const previous = revs.find(r => r.traffic_percent !== 100);
    if (!previous) {
      console.error('\x1b[31m✗\x1b[0m Không có revision trước để rollback');
      process.exit(1);
    }
    console.log(`\x1b[33m⚠\x1b[0m Hiện tại: \x1b[32m${current?.name || '?'}\x1b[0m (100%)`);
    console.log(`   Sẽ rollback về: \x1b[33m${previous.name}\x1b[0m (${previous.created_at})`);
    console.log(`   Press Enter để confirm, Ctrl+C để hủy...`);
    await new Promise(resolve => process.stdin.once('data', resolve));
    targetRevision = previous.name;
  }

  console.log(`\x1b[36m→\x1b[0m Rollback đến ${targetRevision}...`);
  const result = await apiCall('POST',
    `/projects/${project_id}/rollback?ws=${ws}&target_revision=${encodeURIComponent(targetRevision)}`);

  if (result.status === 'success') {
    console.log(`\x1b[32m✓\x1b[0m Rollback thành công!`);
    console.log(`   Từ:  ${result.previous_revision}`);
    console.log(`   Về:  ${result.rolled_back_to}`);
    console.log(`   Tag: ${result.tag}`);
  } else {
    console.error(`\x1b[31m✗\x1b[0m Rollback failed: ${result.error || 'unknown'}`);
    process.exit(1);
  }
}
