/**
 * zeni init — Detect framework + generate zeni.json config
 */
import fs from 'node:fs';
import path from 'node:path';

export async function initCmd(args) {
  const cwd = process.cwd();
  const cfgPath = path.join(cwd, 'zeni.json');
  if (fs.existsSync(cfgPath)) {
    console.log('\x1b[33m⚠\x1b[0m zeni.json đã tồn tại. Sửa thủ công nếu cần.');
    return;
  }

  // Auto-detect framework
  const files = fs.readdirSync(cwd);
  const hasNext = files.some(f => f.startsWith('next.config.'));
  const hasVite = files.some(f => f.startsWith('vite.config.'));
  const hasVue = files.some(f => f === 'vue.config.js');
  const hasReqs = files.includes('requirements.txt');
  const hasPkg = files.includes('package.json');
  const hasIndexHtml = files.includes('index.html');
  const hasDockerfile = files.includes('Dockerfile') || files.includes('dockerfile');
  // PWA hints: manifest, public/manifest, service worker, next-pwa or vite-plugin-pwa in package.json
  const hasManifest = files.includes('manifest.webmanifest') || files.includes('manifest.json') ||
    (fs.existsSync(path.join(cwd, 'public')) && fs.readdirSync(path.join(cwd, 'public')).some(f => f.startsWith('manifest')));
  let pkgHasPWA = false;
  if (hasPkg) {
    try {
      const pkg = JSON.parse(fs.readFileSync(path.join(cwd, 'package.json'), 'utf-8'));
      const allDeps = { ...(pkg.dependencies || {}), ...(pkg.devDependencies || {}) };
      pkgHasPWA = !!(allDeps['next-pwa'] || allDeps['vite-plugin-pwa'] || allDeps['workbox-webpack-plugin']);
    } catch {}
  }
  const looksLikePWA = hasManifest || pkgHasPWA;

  let framework = 'static';
  let port = 80;
  if (hasDockerfile) { framework = 'custom'; port = 8080; }
  else if (hasNext && looksLikePWA) { framework = 'nextjs-pwa'; port = 3000; }
  else if (hasNext) { framework = 'nextjs'; port = 3000; }
  else if (hasVite && looksLikePWA) { framework = 'vite-pwa'; port = 80; }
  else if (hasVite) { framework = 'react'; port = 80; }
  else if (hasVue) { framework = 'vue'; port = 80; }
  else if (hasReqs) { framework = 'fastapi'; port = 8080; }
  else if (hasPkg) { framework = 'express'; port = 3000; }
  else if (hasIndexHtml) { framework = 'static'; port = 80; }

  const projectName = path.basename(cwd).toLowerCase().replace(/[^a-z0-9-]/g, '-');

  // Try to detect default workspace + token_alias from global config
  const { readConfig, readTokens } = await import('../config.js');
  const globalCfg = readConfig();
  const tokens = readTokens();
  const aliasNames = Object.keys(tokens);

  let workspace = globalCfg.workspace || null;
  let tokenAlias = globalCfg.activeAlias || null;
  // If multiple aliases, keep the active one; if none active, take first
  if (!tokenAlias && aliasNames.length > 0) {
    tokenAlias = aliasNames[0];
    workspace = tokens[tokenAlias].workspace;
  }

  const config = {
    name: projectName,
    framework,
    port,
    region: 'asia-southeast1',
    size: 's',
    workspace: workspace,
    token_alias: tokenAlias,
    env_vars: {},
    ignore: ['node_modules', '.git', '__pycache__', '.next', 'dist', '.env'],
  };

  fs.writeFileSync(cfgPath, JSON.stringify(config, null, 2));
  console.log('\x1b[32m✓\x1b[0m Created zeni.json');
  console.log('  framework:    ', framework);
  console.log('  port:         ', port);
  console.log('  name:         ', projectName);
  console.log('  workspace:    ', workspace || '(không set — chạy: zeni alias add <name>)');
  console.log('  token_alias:  ', tokenAlias || '(không set)');
  console.log();
  if (aliasNames.length > 1) {
    console.log('💡 Multi-project tip: anh có ' + aliasNames.length + ' aliases. Sửa "token_alias" trong zeni.json để dùng alias khác:');
    aliasNames.forEach(a => console.log(`     - ${a} (ws=${tokens[a].workspace})`));
    console.log();
  }
  console.log('Sửa zeni.json nếu cần, sau đó: zeni deploy');
}
