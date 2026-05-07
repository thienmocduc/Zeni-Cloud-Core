/**
 * zeni alias — Manage multi-project tokens (NEW v0.2)
 *
 * Usage:
 *   zeni alias add <name> --token <PAT> --workspace <ws>
 *   zeni alias list
 *   zeni alias remove <name>
 *   zeni alias use <name>
 */
import { readTokens, setTokenAlias, writeTokens, readConfig, writeConfig } from '../config.js';

export async function alias(args) {
  const sub = args[0];
  if (!sub || sub === 'list' || sub === 'ls') {
    const tokens = readTokens();
    const aliases = Object.keys(tokens);
    if (!aliases.length) {
      console.log('Chưa có alias nào.');
      console.log('Add: zeni alias add <name> --token <PAT> --workspace <ws>');
      return;
    }
    console.log('\x1b[36m▸\x1b[0m Token aliases:');
    aliases.forEach(name => {
      const t = tokens[name];
      const tokenPreview = t.token ? t.token.slice(0, 16) + '…' : '(no token)';
      console.log(`  ${name.padEnd(20)} ws=${(t.workspace || '?').padEnd(20)} ${tokenPreview}`);
    });
    return;
  }

  if (sub === 'add') {
    const name = args[1];
    if (!name) { console.error('\x1b[31m✗\x1b[0m Usage: zeni alias add <name> --token <PAT> --workspace <ws>'); process.exit(1); }
    const tokenIdx = args.indexOf('--token');
    const wsIdx = args.indexOf('--workspace');
    if (tokenIdx < 0) { console.error('\x1b[31m✗\x1b[0m Missing --token <PAT>'); process.exit(1); }
    const token = args[tokenIdx + 1];
    const workspace = wsIdx >= 0 ? args[wsIdx + 1] : null;
    if (!token) { console.error('\x1b[31m✗\x1b[0m Token empty'); process.exit(1); }
    if (!workspace) { console.error('\x1b[31m✗\x1b[0m Missing --workspace <ws> (default workspace cho alias)'); process.exit(1); }
    setTokenAlias(name, token, workspace);
    console.log(`\x1b[32m✓\x1b[0m Saved alias '${name}' → workspace=${workspace}`);
    console.log(`  Use trong project: tạo zeni.json với { "token_alias": "${name}", "workspace": "${workspace}" }`);
    return;
  }

  if (sub === 'remove' || sub === 'rm') {
    const name = args[1];
    if (!name) { console.error('\x1b[31m✗\x1b[0m Usage: zeni alias remove <name>'); process.exit(1); }
    const tokens = readTokens();
    if (!tokens[name]) {
      console.error(`\x1b[31m✗\x1b[0m Alias '${name}' not found`);
      process.exit(1);
    }
    delete tokens[name];
    writeTokens(tokens);
    console.log(`\x1b[32m✓\x1b[0m Removed alias '${name}'`);
    return;
  }

  if (sub === 'use') {
    const name = args[1];
    if (!name) { console.error('\x1b[31m✗\x1b[0m Usage: zeni alias use <name>'); process.exit(1); }
    const tokens = readTokens();
    if (!tokens[name]) {
      console.error(`\x1b[31m✗\x1b[0m Alias '${name}' not found. Run: zeni alias list`);
      process.exit(1);
    }
    // Set as global default
    const cfg = readConfig();
    cfg.token = tokens[name].token;
    cfg.workspace = tokens[name].workspace;
    cfg.activeAlias = name;
    writeConfig(cfg);
    console.log(`\x1b[32m✓\x1b[0m Active alias: '${name}' (workspace: ${tokens[name].workspace})`);
    return;
  }

  console.error(`\x1b[31m✗\x1b[0m Unknown subcommand: ${sub}`);
  console.log('Available: add | list | remove | use');
  process.exit(1);
}
