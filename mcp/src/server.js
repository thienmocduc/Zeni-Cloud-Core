/**
 * Zeni Cloud MCP Server
 * Implements MCP protocol with 12 tools for AI agents.
 *
 * Required env:
 *   ZENI_TOKEN     — PAT (zeni_pat_xxx)
 *   ZENI_WORKSPACE — workspace ID
 *   ZENI_API       — optional, default https://zenicloud.io/api/v1
 */
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js';

const API_BASE = process.env.ZENI_API || 'https://zenicloud.io/api/v1';
const TOKEN = process.env.ZENI_TOKEN;
const WS = process.env.ZENI_WORKSPACE;

if (!TOKEN) { console.error('Missing ZENI_TOKEN env var'); process.exit(1); }
if (!WS)    { console.error('Missing ZENI_WORKSPACE env var'); process.exit(1); }

async function api(method, path, body) {
  const url = path.startsWith('http') ? path : API_BASE + path;
  const opts = {
    method,
    headers: { 'Authorization': `Bearer ${TOKEN}`, 'Accept': 'application/json' },
  };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(url, opts);
  let data;
  try { data = await r.json(); } catch { data = await r.text(); }
  if (!r.ok) {
    const msg = (data && data.detail) || `HTTP ${r.status}`;
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return data;
}

// ─── 12 Tools cho AI Agents ─────────────────────────────────────
const TOOLS = [
  {
    name: 'zeni_deploy',
    description: 'Deploy app to Zeni Cloud. Pass either docker image URL OR base64-encoded ZIP source. Returns deploy_id + poll_url.',
    inputSchema: {
      type: 'object',
      properties: {
        name: { type: 'string', description: 'Project name (lowercase + hyphens)' },
        image: { type: 'string', description: 'Pre-built Docker image URL (alternative to zip_base64)' },
        zip_base64: { type: 'string', description: 'Base64-encoded ZIP source (alternative to image)' },
        framework: { type: 'string', description: 'auto | nextjs | react | vue | static | fastapi | express', default: 'auto' },
        port: { type: 'number', default: 8080 },
        region: { type: 'string', default: 'asia-southeast1' },
        env_vars: { type: 'object', description: 'Environment variables' },
      },
      required: ['name'],
    },
    handler: async (args) => api('POST', `/deploy/quick?ws=${WS}`, args),
  },
  {
    name: 'zeni_list_projects',
    description: 'List all projects (deployed apps) trong workspace hiện tại.',
    inputSchema: { type: 'object', properties: {} },
    handler: async () => api('GET', `/projects?ws=${WS}`),
  },
  {
    name: 'zeni_get_project',
    description: 'Get detail của 1 project (status, URL, image).',
    inputSchema: {
      type: 'object',
      properties: { project_id: { type: 'string' } },
      required: ['project_id'],
    },
    handler: async ({ project_id }) => api('GET', `/projects/${project_id}?ws=${WS}`),
  },
  {
    name: 'zeni_redeploy',
    description: 'Re-deploy 1 project (rebuild same image).',
    inputSchema: {
      type: 'object',
      properties: { project_id: { type: 'string' } },
      required: ['project_id'],
    },
    handler: async ({ project_id }) => api('POST', `/projects/${project_id}/redeploy?ws=${WS}`),
  },
  {
    name: 'zeni_ai_call',
    description: 'Call ZeniRouter AI — auto-route Claude/GPT/Gemini theo task. Cheaper than direct provider.',
    inputSchema: {
      type: 'object',
      properties: {
        messages: { type: 'array', items: { type: 'object' } },
        max_tokens: { type: 'number', default: 500 },
        temperature: { type: 'number', default: 0.7 },
        model_hint: { type: 'string', enum: ['fast', 'balanced', 'premium', 'reasoning', 'frontier'] },
      },
      required: ['messages'],
    },
    handler: async (args) => api('POST', `/router/route?ws=${WS}`, args),
  },
  {
    name: 'zeni_vector_create_collection',
    description: 'Tạo vector collection (pgvector) cho RAG / semantic search.',
    inputSchema: {
      type: 'object',
      properties: {
        name: { type: 'string', description: 'Collection name (a-z, 0-9, _)' },
        dim: { type: 'number', default: 768 },
      },
      required: ['name'],
    },
    handler: async (args) => api('POST', `/vector/collections?ws=${WS}`, args),
  },
  {
    name: 'zeni_vector_search',
    description: 'Search semantic similarity trong vector collection.',
    inputSchema: {
      type: 'object',
      properties: {
        collection: { type: 'string' },
        query: { type: 'string' },
        top_k: { type: 'number', default: 5 },
      },
      required: ['collection', 'query'],
    },
    handler: async ({ collection, query, top_k }) =>
      api('POST', `/vector/collections/${collection}/search?ws=${WS}`, { query, top_k }),
  },
  {
    name: 'zeni_secret_set',
    description: 'Lưu secret (API key, password) trong vault encrypted KMS. Reference trong project env vars.',
    inputSchema: {
      type: 'object',
      properties: {
        name: { type: 'string' },
        value: { type: 'string' },
      },
      required: ['name', 'value'],
    },
    handler: async (args) => api('POST', `/identity/secrets?ws=${WS}`, args),
  },
  {
    name: 'zeni_billing_status',
    description: 'Get current subscription tier, quota, usage MTD.',
    inputSchema: { type: 'object', properties: {} },
    handler: async () => api('GET', `/billing/subscription?ws=${WS}`),
  },
  {
    name: 'zeni_trial_status',
    description: 'Check 14-day trial status: days remaining, expired or active.',
    inputSchema: { type: 'object', properties: {} },
    handler: async () => api('GET', `/trial/status?ws=${WS}`),
  },
  {
    name: 'zeni_audit_log',
    description: 'Read audit log của workspace (recent actions: deploys, logins, API calls).',
    inputSchema: {
      type: 'object',
      properties: { limit: { type: 'number', default: 20 } },
    },
    handler: async ({ limit = 20 }) => api('GET', `/audit?ws=${WS}&limit=${limit}`),
  },
  {
    name: 'zeni_oauth_provider_setup',
    description: 'Setup customer OAuth provider (Zalo/Apple/Facebook/Line/Kakao/TikTok/LinkedIn) cho app của khách.',
    inputSchema: {
      type: 'object',
      properties: {
        provider: { type: 'string', enum: ['zalo', 'apple', 'facebook', 'line', 'kakao', 'tiktok', 'linkedin'] },
        display_name: { type: 'string' },
        client_id: { type: 'string' },
        client_secret: { type: 'string' },
        app_callback_url: { type: 'string' },
      },
      required: ['provider', 'client_id', 'client_secret', 'app_callback_url'],
    },
    handler: async (args) => api('POST', `/identity/oauth-providers?ws=${WS}`, args),
  },
];

// ─── MCP Server setup ───────────────────────────────────────────
export async function startServer() {
  const server = new Server(
    { name: 'zenicloud', version: '0.1.0' },
    { capabilities: { tools: {} } }
  );

  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: TOOLS.map(t => ({
      name: t.name,
      description: t.description,
      inputSchema: t.inputSchema,
    })),
  }));

  server.setRequestHandler(CallToolRequestSchema, async (req) => {
    const { name, arguments: args = {} } = req.params;
    const tool = TOOLS.find(t => t.name === name);
    if (!tool) {
      return {
        content: [{ type: 'text', text: `Unknown tool: ${name}` }],
        isError: true,
      };
    }
    try {
      const result = await tool.handler(args);
      return {
        content: [{ type: 'text', text: JSON.stringify(result, null, 2) }],
      };
    } catch (e) {
      return {
        content: [{ type: 'text', text: `Error: ${e.message}` }],
        isError: true,
      };
    }
  });

  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error(`[Zeni MCP] Server started — ${TOOLS.length} tools available for ws=${WS}`);
}
