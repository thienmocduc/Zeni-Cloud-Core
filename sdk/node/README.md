# @zenicloud/sdk · Node.js / TypeScript

Official SDK for [Zeni Cloud](https://zenicloud.io) — Cloud OS thống nhất cho doanh nghiệp Việt Nam (100% Google Cloud).

## Install

```bash
npm install @zenicloud/sdk
# hoặc
pnpm add @zenicloud/sdk
yarn add @zenicloud/sdk
```

## Quick start

```typescript
import { ZeniCloud } from '@zenicloud/sdk';

const zeni = new ZeniCloud({
  token: process.env.ZENI_TOKEN!,
  workspace: 'nexbuild',
});

// 1. Architecture agent — KTS Senior level concept + 2 ảnh render
const result = await zeni.agents.architecture.run({
  brief: 'Biet thu 3 tang Phu My Hung 200m2 Tropical Modern',
  generate_renders: true,
  n_renders: 2,
  aspect_ratio: '16:9',
  constraints: { area_m2: 200, floors: 3, budget_vnd: 8_000_000_000 },
});

console.log('Concept:', result.concept.slice(0, 500));
console.log('Renders:', result.renders.length, '× ảnh');
console.log('Cost: $', result.cost_usd);

// 2. Image generation Imagen 3
const img = await zeni.ai.generateImage({
  prompt: 'Vietnamese tropical villa, golden hour, 8K',
  aspect_ratio: '16:9',
  n: 1,
});
const dataUri = img.images[0].data_uri;  // base64 PNG

// 3. Streaming text completion
for await (const chunk of zeni.ai.stream({
  prompt: 'Liệt kê 5 phong cách thiết kế nội thất hot 2026',
  model: 'gemini-2.5-pro',
})) {
  process.stdout.write(chunk);
}

// 4. Deploy app to Cloud Run
const project = await zeni.projects.deploy({
  name: 'my-app',
  image: 'us-central1-docker.pkg.dev/zeni-cloud-core/zeni-images/my-app:v1',
  size: 'm',
  port: 8080,
});

// 5. Run SQL on Cloud SQL workspace schema
const rows = await zeni.data.sql('SELECT * FROM kv LIMIT 10');

// 6. Live read $ZENI token on Polygon
const stack = await zeni.web3.zeniStack();
console.log('$ZENI total supply:', stack.ZENI_TOKEN.total_supply);

// 7. Setup cron job (Cloud Scheduler)
await zeni.automation.createCron({
  name: 'daily-report',
  schedule: '0 9 * * *',
  target_url: 'https://my-app.run.app/api/cron',
  method: 'POST',
  timezone: 'Asia/Ho_Chi_Minh',
});

// 8. Check billing
const sub = await zeni.billing.subscription();
console.log('Tier:', sub.tier, '| Used runs:', sub.used.agent_runs);
```

## API Reference

| Resource | Methods |
|----------|---------|
| `zeni.auth` | `me()`, `refresh()`, `logout()` |
| `zeni.projects` | `list()`, `deploy()`, `delete()`, `addDomain()` |
| `zeni.data` | `sql()`, `listTables()`, `query()` |
| `zeni.ai` | `complete()`, `generateImage()`, `analyzeImage()`, `embed()`, `stream()` |
| `zeni.agents.architecture` | `run()`, `runStructured()`, `refine()`, `schema()` |
| `zeni.agents.interior` | (5 specialized agents: architecture, interior, product, fashion, structural) |
| `zeni.automation` | `createCron()`, `addConnector()`, `fireEvent()`, `listWebhookAttempts()` |
| `zeni.identity` | `createSecret()`, `setupMFA()`, `verifyMFA()` |
| `zeni.web3` | `chains()`, `zeniStack()`, `read()`, `txReceipt()` |
| `zeni.billing` | `wallet()`, `subscription()`, `dashboardSummary()` |
| `zeni.tokens` | `list()`, `create()`, `revoke()` |

## Error handling

```typescript
import { ZeniError } from '@zenicloud/sdk';

try {
  await zeni.ai.generateImage({ prompt: '...' });
} catch (e) {
  if (e instanceof ZeniError) {
    if (e.status === 402) console.log('Hết quota — top-up wallet');
    if (e.status === 429) console.log('Rate limited');
    if (e.status === 401) console.log('Token sai/hết hạn');
  }
}
```

## License

MIT · © Zeni Holdings 2026
