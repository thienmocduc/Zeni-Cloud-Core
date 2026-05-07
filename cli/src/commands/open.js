/**
 * zeni open — Open project URL trong browser
 */
import { apiCall, requireAuth, requireWorkspace } from '../config.js';
import { spawn } from 'node:child_process';

function openUrl(url) {
  const cmd = process.platform === 'darwin' ? 'open'
            : process.platform === 'win32' ? 'start'
            : 'xdg-open';
  spawn(cmd, [url], { stdio: 'ignore', detached: true, shell: process.platform === 'win32' }).unref();
}

export async function open(args) {
  requireAuth();
  const ws = requireWorkspace();
  const projectName = args.find(a => !a.startsWith('--'));
  if (!projectName) {
    // Open dashboard
    const url = 'https://zenicloud.io/app';
    console.log(`\x1b[36m▸\x1b[0m Opening dashboard: ${url}`);
    openUrl(url);
    return;
  }
  const projects = await apiCall('GET', `/projects?ws=${encodeURIComponent(ws)}`);
  const proj = projects.find(p => p.name === projectName || p.name.endsWith('-' + projectName));
  if (!proj || !proj.domain) {
    console.error(`\x1b[31m✗\x1b[0m Project '${projectName}' not found or chưa có URL.`);
    process.exit(1);
  }
  console.log(`\x1b[36m▸\x1b[0m Opening ${proj.domain}`);
  openUrl(proj.domain);
}
