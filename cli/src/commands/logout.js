/**
 * zeni logout — Clear local config
 */
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';

export async function logout() {
  const file = path.join(os.homedir(), '.zeni', 'config.json');
  if (fs.existsSync(file)) {
    fs.unlinkSync(file);
    console.log('\x1b[32m✓\x1b[0m Logged out — ~/.zeni/config.json deleted');
  } else {
    console.log('Đã logout (không có config).');
  }
}
