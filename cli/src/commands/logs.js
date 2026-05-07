/**
 * zeni logs — Get recent logs cho project (Cloud Logging integration)
 * Usage: zeni logs [project-name] [--tail]
 */
import { apiCall, requireAuth, requireWorkspace } from '../config.js';

export async function logs(args) {
  requireAuth();
  const ws = requireWorkspace();
  const projectName = args.find(a => !a.startsWith('--')) || null;

  if (!projectName) {
    console.error('\x1b[31m✗\x1b[0m Usage: zeni logs <project-name>');
    console.error('  Run "zeni list" để xem projects.');
    process.exit(1);
  }

  // Find project
  const projects = await apiCall('GET', `/projects?ws=${encodeURIComponent(ws)}`);
  const proj = projects.find(p => p.name === projectName || p.name.endsWith('-' + projectName));
  if (!proj) {
    console.error(`\x1b[31m✗\x1b[0m Project '${projectName}' not found.`);
    console.error(`  Available: ${projects.map(p => p.name).join(', ')}`);
    process.exit(1);
  }

  console.log(`\x1b[36m▸\x1b[0m Logs cho ${proj.name} (${proj.cloud_run_service || proj.id}):`);
  console.log(`  Region: ${proj.region}`);
  console.log(`  Domain: ${proj.domain || '(deploying)'}`);
  console.log();
  console.log('  Cloud Run logs viewer:');
  console.log(`  https://console.cloud.google.com/run/detail/${proj.region}/${proj.cloud_run_service}/logs?project=zeni-cloud-core`);
  console.log();
  console.log('  Realtime tail trong terminal đang được phát triển (Sprint A12).');
  console.log('  Tạm thời dùng: gcloud logging read \'resource.labels.service_name="' + proj.cloud_run_service + '"\' --limit=50');
}
