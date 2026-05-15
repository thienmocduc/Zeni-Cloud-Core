const express = require('express');

const app = express();
const PORT = parseInt(process.env.PORT, 10) || 3000;

const HTML = `<!doctype html>
<html><head><meta charset='utf-8'>
<title>Express on Zeni Cloud</title>
<meta name='viewport' content='width=device-width,initial-scale=1'/>
</head><body style='margin:0;font-family:system-ui;background:linear-gradient(135deg,#0f0f23,#1a1a3e);color:#fff;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px'>
<div style='max-width:720px;text-align:center'>
  <div style='font-size:64px'>🚂</div>
  <h1 style='color:#22c55e;font-size:36px;margin:8px 0'>Hello from Express on Zeni Cloud</h1>
  <p style='color:#cbd5e1'>Your Node.js API is live. Replace server.js to ship your routes.</p>
  <ul style='list-style:none;padding:0;text-align:left;background:rgba(255,255,255,0.04);border:1px solid rgba(34,197,94,0.3);border-radius:12px;padding:24px'>
    <li style='padding:6px 0'>Node.js 20 LTS runtime (alpine, ~50 MB image)</li>
    <li style='padding:6px 0'>Auto-scale on Zeni Cloud Run</li>
    <li style='padding:6px 0'>Health endpoint at <code style='color:#22c55e'>/health</code></li>
    <li style='padding:6px 0'>JSON API endpoint at <code style='color:#22c55e'>/api</code></li>
    <li style='padding:6px 0'>Edit server.js and redeploy</li>
  </ul>
  <p style='font-size:12px;color:#64748b;margin-top:24px'>Powered by <strong style='color:#22c55e'>Zeni Cloud</strong> · Express + Node 20</p>
</div></body></html>`;

app.get('/', (_req, res) => res.type('html').send(HTML));

app.get('/api', (_req, res) => res.json({
  service: 'Express on Zeni Cloud',
  status: 'ok',
  version: '1.0.0'
}));

app.get('/health', (_req, res) => res.json({ status: 'healthy' }));

app.listen(PORT, '0.0.0.0', () => {
  console.log(`Express listening on 0.0.0.0:${PORT}`);
});
