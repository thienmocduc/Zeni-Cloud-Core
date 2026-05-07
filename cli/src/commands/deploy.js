/**
 * zeni deploy — Zip current dir → upload → poll → return URL
 */
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import { apiCall, requireAuth, requireWorkspace, API_BASE } from '../config.js';

// Minimal ZIP writer (no external deps)
function makeZip(files) {
  const fileEntries = [];
  const centralDir = [];
  let offset = 0;

  for (const { name, data } of files) {
    const nameBuf = Buffer.from(name, 'utf-8');
    const compressed = zlib.deflateRawSync(data);
    const crc = computeCrc32(data);
    const local = Buffer.concat([
      // Local file header signature
      Buffer.from([0x50, 0x4b, 0x03, 0x04]),
      // Version + flags
      Buffer.from([0x14, 0x00, 0x00, 0x00]),
      // Compression method (deflate=8)
      Buffer.from([0x08, 0x00]),
      // Mod time + date (dummy)
      Buffer.from([0x00, 0x00, 0x21, 0x00]),
      // CRC32
      uint32(crc),
      // Compressed size
      uint32(compressed.length),
      // Uncompressed size
      uint32(data.length),
      // Filename length
      uint16(nameBuf.length),
      // Extra field length
      uint16(0),
      nameBuf,
      compressed,
    ]);
    fileEntries.push(local);

    // Central directory entry
    const central = Buffer.concat([
      Buffer.from([0x50, 0x4b, 0x01, 0x02]),
      Buffer.from([0x14, 0x00, 0x14, 0x00, 0x00, 0x00, 0x08, 0x00]),
      Buffer.from([0x00, 0x00, 0x21, 0x00]),
      uint32(crc),
      uint32(compressed.length),
      uint32(data.length),
      uint16(nameBuf.length),
      uint16(0), uint16(0), uint16(0), uint16(0),
      uint32(0),
      uint32(offset),
      nameBuf,
    ]);
    centralDir.push(central);

    offset += local.length;
  }

  const cdSize = centralDir.reduce((s, b) => s + b.length, 0);
  const cdOffset = offset;
  const eocd = Buffer.concat([
    Buffer.from([0x50, 0x4b, 0x05, 0x06]),
    uint16(0), uint16(0),
    uint16(files.length),
    uint16(files.length),
    uint32(cdSize),
    uint32(cdOffset),
    uint16(0),
  ]);

  return Buffer.concat([...fileEntries, ...centralDir, eocd]);
}

function uint16(n) { const b = Buffer.alloc(2); b.writeUInt16LE(n, 0); return b; }
function uint32(n) { const b = Buffer.alloc(4); b.writeUInt32LE(n >>> 0, 0); return b; }

const CRC_TABLE = (() => {
  const table = new Array(256);
  for (let i = 0; i < 256; i++) {
    let c = i;
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1);
    table[i] = c >>> 0;
  }
  return table;
})();

function computeCrc32(data) {
  let crc = 0xffffffff;
  for (let i = 0; i < data.length; i++) {
    crc = (crc >>> 8) ^ CRC_TABLE[(crc ^ data[i]) & 0xff];
  }
  return (crc ^ 0xffffffff) >>> 0;
}

const DEFAULT_IGNORE = [
  'node_modules', '.git', '__pycache__', '.next', 'dist', 'build',
  '.venv', 'venv', '.pytest_cache', '.cache',
  '.env', '.env.local', '.env.production',
  '.DS_Store', 'Thumbs.db',
  'zeni.json',
];

function shouldIgnore(relPath, ignorePatterns) {
  const parts = relPath.split(/[\\/]/);
  for (const part of parts) {
    if (ignorePatterns.includes(part)) return true;
  }
  return false;
}

function collectFiles(rootDir, ignorePatterns) {
  const files = [];
  function walk(dir, rel = '') {
    const entries = fs.readdirSync(dir, { withFileTypes: true });
    for (const entry of entries) {
      const fullPath = path.join(dir, entry.name);
      const relPath = rel ? path.join(rel, entry.name) : entry.name;
      const normalized = relPath.replace(/\\/g, '/');
      if (shouldIgnore(normalized, ignorePatterns)) continue;
      if (entry.isDirectory()) {
        walk(fullPath, relPath);
      } else if (entry.isFile()) {
        const stat = fs.statSync(fullPath);
        if (stat.size > 50 * 1024 * 1024) {
          console.warn(`\x1b[33m⚠\x1b[0m Skipping large file (>50MB): ${normalized}`);
          continue;
        }
        files.push({ name: normalized, data: fs.readFileSync(fullPath) });
      }
    }
  }
  walk(rootDir);
  return files;
}

export async function deploy(args) {
  requireAuth();
  const ws = requireWorkspace();
  const cwd = process.cwd();

  // Read zeni.json
  const cfgPath = path.join(cwd, 'zeni.json');
  let cfg = {};
  if (fs.existsSync(cfgPath)) {
    cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf-8'));
  } else {
    console.log('\x1b[33m⚠\x1b[0m zeni.json không tồn tại, dùng defaults. Run: zeni init');
  }

  const name = cfg.name || path.basename(cwd).toLowerCase().replace(/[^a-z0-9-]/g, '-');
  const framework = cfg.framework || 'auto';
  const port = cfg.port || 8080;
  const region = cfg.region || 'asia-southeast1';
  const ignorePatterns = [...DEFAULT_IGNORE, ...(cfg.ignore || [])];

  console.log('\x1b[36m▸\x1b[0m Deploying:');
  console.log('  workspace:', ws);
  console.log('  name:     ', name);
  console.log('  framework:', framework);
  console.log('  port:     ', port);
  console.log('  region:   ', region);
  console.log();

  // Collect files
  process.stdout.write('⏳ Zipping...');
  const files = collectFiles(cwd, ignorePatterns);
  if (files.length === 0) {
    console.error('\n\x1b[31m✗\x1b[0m No files to deploy');
    process.exit(1);
  }
  const zipBuf = makeZip(files);
  const zipMb = (zipBuf.length / 1024 / 1024).toFixed(2);
  console.log(`\r✓ Zipped ${files.length} files (${zipMb} MB)        `);

  if (zipBuf.length > 50 * 1024 * 1024) {
    console.error('\x1b[31m✗\x1b[0m ZIP > 50MB. Add more entries to .ignore in zeni.json');
    process.exit(1);
  }

  // Upload + queue deploy
  process.stdout.write('⏳ Uploading + queueing build...');
  const result = await apiCall('POST', `/deploy/quick?ws=${encodeURIComponent(ws)}`, {
    zip_base64: zipBuf.toString('base64'),
    name,
    framework,
    port,
    region,
    env_vars: cfg.env_vars || {},
  });
  console.log(`\r✓ Queued (deploy_id: ${result.deploy_id})        `);

  // Poll
  console.log('\x1b[36m▸\x1b[0m Building & deploying (~60-90s)...');
  const startTime = Date.now();
  let lastStatus = '';
  for (let i = 0; i < 30; i++) {
    await new Promise(r => setTimeout(r, 5000));
    let status;
    try {
      status = await apiCall('GET', `/upload/source/${result.deploy_id}?ws=${encodeURIComponent(ws)}`);
    } catch (e) {
      console.warn('\x1b[33m⚠\x1b[0m Poll error:', e.message);
      continue;
    }
    const elapsed = Math.floor((Date.now() - startTime) / 1000);
    if (status.status !== lastStatus) {
      console.log(`  [${elapsed}s] ${status.status}${status.image_url ? ' → ' + status.image_url.split('/').pop() : ''}`);
      lastStatus = status.status;
    }
    if (status.status === 'success') {
      console.log();
      console.log(`\x1b[32m✓ DEPLOYED\x1b[0m`);
      if (status.deploy_url) console.log(`  URL:   ${status.deploy_url}`);
      if (status.image_url) console.log(`  Image: ${status.image_url}`);
      return;
    }
    if (status.status === 'failed') {
      console.error(`\n\x1b[31m✗ Deploy failed:\x1b[0m ${status.error_message || 'unknown'}`);
      process.exit(1);
    }
  }
  console.warn('\n\x1b[33m⚠\x1b[0m Deploy quá lâu (>2.5 phút). Check dashboard: https://zenicloud.io/app');
}
