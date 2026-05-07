/**
 * Zeni CLI router — dispatch to command handlers.
 */
import { login } from './commands/login.js';
import { whoami } from './commands/whoami.js';
import { logout } from './commands/logout.js';
import { initCmd } from './commands/init.js';
import { deploy } from './commands/deploy.js';
import { logs } from './commands/logs.js';
import { listProjects } from './commands/list.js';
import { open as openCmd } from './commands/open.js';
import { help } from './commands/help.js';
import { alias } from './commands/alias.js';

const COMMANDS = {
  login,
  whoami,
  logout,
  init: initCmd,
  deploy,
  logs,
  list: listProjects,
  ls: listProjects,
  open: openCmd,
  alias,         // NEW v0.2: multi-project tokens
  help,
  '-h': help,
  '--help': help,
  '-v': () => console.log('zeni-cli v0.2.0'),
  '--version': () => console.log('zeni-cli v0.2.0'),
};

export async function run(args) {
  const [cmd, ...rest] = args;
  if (!cmd) return help();
  const fn = COMMANDS[cmd];
  if (!fn) {
    console.error(`\x1b[31m✗\x1b[0m Unknown command: ${cmd}`);
    console.error(`Try: zeni help`);
    process.exit(1);
  }
  await fn(rest);
}
