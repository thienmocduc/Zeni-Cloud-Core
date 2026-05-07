#!/usr/bin/env node
/**
 * Zeni Cloud CLI — entry point
 * Usage: zeni <command> [args]
 */
import { run } from '../src/cli.js';

run(process.argv.slice(2)).catch(err => {
  console.error('\x1b[31m✗\x1b[0m', err.message || err);
  if (process.env.ZENI_DEBUG) console.error(err.stack);
  process.exit(1);
});
