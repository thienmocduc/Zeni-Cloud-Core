#!/usr/bin/env node
/**
 * Zeni Cloud MCP Server entry point
 */
import { startServer } from '../src/server.js';

startServer().catch(err => {
  console.error('MCP Server fatal:', err);
  process.exit(1);
});
