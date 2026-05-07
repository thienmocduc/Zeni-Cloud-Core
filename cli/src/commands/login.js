/**
 * zeni login — Login bằng email/password hoặc API token
 */
import readline from 'node:readline';
import { writeConfig, readConfig, API_BASE } from '../config.js';

function ask(question, hidden = false) {
  return new Promise(resolve => {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    if (hidden) {
      // Hide input (password) — basic implementation
      const onData = char => {
        char = char + '';
        switch (char) {
          case '\n':
          case '\r':
          case '':
            process.stdin.removeListener('data', onData);
            break;
          default:
            process.stdout.write('\x1b[2K\x1b[200D' + question + '*'.repeat(rl.line.length));
            break;
        }
      };
      process.stdin.on('data', onData);
    }
    rl.question(question, ans => {
      rl.close();
      resolve(ans.trim());
    });
  });
}

export async function login(args) {
  const config = readConfig();
  console.log('\x1b[36m▸\x1b[0m Zeni Cloud login');
  console.log('  API:', API_BASE);
  console.log();

  // Option 1: Pass token directly via flag
  if (args.includes('--token')) {
    const idx = args.indexOf('--token');
    const token = args[idx + 1];
    if (!token) {
      console.error('\x1b[31m✗\x1b[0m Missing token after --token');
      process.exit(1);
    }
    const wsIdx = args.indexOf('--workspace');
    const workspace = wsIdx >= 0 ? args[wsIdx + 1] : await ask('Workspace ID: ');
    writeConfig({ ...config, token, workspace, savedAt: new Date().toISOString() });
    console.log(`\x1b[32m✓\x1b[0m Saved token + workspace=${workspace} to ~/.zeni/config.json`);
    return;
  }

  // Option 2: Email + password
  const email = await ask('Email: ');
  if (!email) { console.error('\x1b[31m✗\x1b[0m Email empty'); process.exit(1); }
  const password = await ask('Password: ', true);
  process.stdout.write('\n');
  if (!password) { console.error('\x1b[31m✗\x1b[0m Password empty'); process.exit(1); }

  // Login API call
  const r = await fetch(`${API_BASE}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  });
  if (!r.ok) {
    const data = await r.json().catch(() => ({}));
    console.error('\x1b[31m✗\x1b[0m Login failed:', data.detail || r.statusText);
    process.exit(1);
  }
  const data = await r.json();
  const accessToken = data.access_token;

  // Get user info + workspaces
  const meR = await fetch(`${API_BASE}/auth/me`, {
    headers: { 'Authorization': `Bearer ${accessToken}` },
  });
  const me = await meR.json();
  const workspaces = me.workspaces || [];

  // Select workspace
  let workspace;
  if (workspaces.length === 0) {
    console.error('\x1b[31m✗\x1b[0m Tài khoản này không có workspace nào. Liên hệ admin.');
    process.exit(1);
  } else if (workspaces.length === 1) {
    workspace = workspaces[0];
    console.log(`\x1b[36m▸\x1b[0m Auto-select workspace: ${workspace}`);
  } else {
    console.log('\nWorkspaces của bạn:');
    workspaces.forEach((w, i) => console.log(`  ${i + 1}. ${w}`));
    const choice = await ask('Chọn workspace (số): ');
    workspace = workspaces[parseInt(choice) - 1] || workspaces[0];
  }

  // Recommend creating a long-lived PAT instead of using JWT
  console.log('\n\x1b[33m⚠\x1b[0m JWT chỉ sống 1h. Em đề xuất tạo PAT (Personal Access Token):');
  console.log('  → Vào https://zenicloud.io/app → API Tokens → "+ Create"');
  console.log('  → Sau đó: zeni login --token zeni_pat_xxx --workspace ' + workspace);
  console.log();
  console.log('Hiện tại đang dùng JWT (expires in 1h):');

  writeConfig({
    ...readConfig(),
    token: accessToken,
    workspace,
    email,
    savedAt: new Date().toISOString(),
  });
  console.log(`\x1b[32m✓\x1b[0m Logged in as ${email} (workspace: ${workspace})`);
  console.log('  Config saved to ~/.zeni/config.json');
}
