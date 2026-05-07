/**
 * Zeni Cloud SDK · Node.js / TypeScript
 *
 *   import { ZeniCloud } from '@zenicloud/sdk';
 *
 *   const zeni = new ZeniCloud({
 *     token: process.env.ZENI_TOKEN!,
 *     workspace: 'nexbuild',
 *   });
 *
 *   const result = await zeni.agents.architecture.run({
 *     brief: 'Biet thu 3 tang 200m2 Tropical Modern',
 *     generate_renders: true,
 *     n_renders: 2,
 *   });
 */

export interface ZeniConfig {
  token: string;
  workspace?: string;
  baseUrl?: string;
  timeoutMs?: number;
}

export class ZeniError extends Error {
  status: number;
  detail: any;
  constructor(status: number, detail: any) {
    super(typeof detail === 'string' ? detail : JSON.stringify(detail));
    this.status = status;
    this.detail = detail;
  }
}

interface AgentRunInput {
  brief: string;
  reference_image_uri?: string;
  reference_image_url?: string;
  generate_renders?: boolean;
  n_renders?: number;
  aspect_ratio?: '1:1' | '9:16' | '16:9' | '3:4' | '4:3';
  constraints?: Record<string, any>;
}

interface AgentRunResult {
  kind: string;
  concept: string;
  critique?: string;
  renders: Array<{ data_uri: string; size_bytes: number }>;
  reference_analysis?: string;
  tokens: { input: number; output: number };
  renders_count: number;
  cost_usd: number;
  timings_ms: Record<string, number>;
}

export class ZeniCloud {
  private token: string;
  private workspace: string;
  private baseUrl: string;
  private timeoutMs: number;

  // Resources
  public auth: AuthAPI;
  public projects: ProjectsAPI;
  public data: DataAPI;
  public ai: AIAPI;
  public agents: AgentsAPI;
  public automation: AutomationAPI;
  public identity: IdentityAPI;
  public web3: Web3API;
  public billing: BillingAPI;
  public tokens: TokensAPI;
  public email: EmailAPI;

  constructor(config: ZeniConfig) {
    if (!config.token) throw new Error('Zeni token required');
    this.token = config.token;
    this.workspace = config.workspace || '';
    this.baseUrl = (config.baseUrl || 'https://zenicloud.io/api/v1').replace(/\/$/, '');
    this.timeoutMs = config.timeoutMs || 120_000;

    this.auth = new AuthAPI(this);
    this.projects = new ProjectsAPI(this);
    this.data = new DataAPI(this);
    this.ai = new AIAPI(this);
    this.agents = new AgentsAPI(this);
    this.automation = new AutomationAPI(this);
    this.identity = new IdentityAPI(this);
    this.web3 = new Web3API(this);
    this.billing = new BillingAPI(this);
    this.tokens = new TokensAPI(this);
    this.email = new EmailAPI(this);
  }

  /** @internal */
  async _request<T = any>(
    method: string, path: string, body?: any, opts: { ws?: boolean | string; query?: Record<string, any> } = {}
  ): Promise<T> {
    let url = this.baseUrl + path;
    const params = new URLSearchParams();
    const wsValue = opts.ws === true ? this.workspace : (typeof opts.ws === 'string' ? opts.ws : null);
    if (wsValue) params.set('ws', wsValue);
    if (opts.query) {
      for (const [k, v] of Object.entries(opts.query)) {
        if (v !== undefined && v !== null) params.set(k, String(v));
      }
    }
    if ([...params].length) url += '?' + params.toString();

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const r = await fetch(url, {
        method,
        headers: {
          'Authorization': `Bearer ${this.token}`,
          'Content-Type': 'application/json',
          'Accept': 'application/json',
          'User-Agent': '@zenicloud/sdk-node/1.0.0',
        },
        body: body ? JSON.stringify(body) : undefined,
        signal: controller.signal,
      });
      const text = await r.text();
      let parsed: any = null;
      try { parsed = text ? JSON.parse(text) : null; } catch { parsed = text; }
      if (!r.ok) throw new ZeniError(r.status, parsed?.detail ?? parsed ?? r.statusText);
      return parsed as T;
    } finally {
      clearTimeout(timer);
    }
  }

  setWorkspace(ws: string) { this.workspace = ws; }
  getWorkspace(): string { return this.workspace; }
}

// ─── Auth ───────────────────────────────────────────────────
class AuthAPI {
  constructor(private z: ZeniCloud) {}
  me() { return this.z._request('GET', '/auth/me'); }
  refresh(refresh_token: string) {
    return this.z._request('POST', '/auth/refresh', { refresh_token });
  }
  logout(refresh_token: string) {
    return this.z._request('POST', '/auth/logout', { refresh_token });
  }
}

// ─── Projects (L1 Compute) ──────────────────────────────────
class ProjectsAPI {
  constructor(private z: ZeniCloud) {}
  list() { return this.z._request<any[]>('GET', '/projects', undefined, { ws: true }); }
  get(id: string) { return this.z._request('GET', `/projects/${id}`, undefined, { ws: true }); }
  deploy(input: {
    name: string;
    type?: 'web' | 'api' | 'worker' | 'agent';
    runtime?: string;
    size?: 'xs' | 's' | 'm' | 'l';
    region?: string;
    image: string;
    port?: number;
    allow_unauthenticated?: boolean;
    env_vars?: Record<string, string>;
    secrets?: Record<string, string>;
    git_ref?: string;
  }) {
    return this.z._request('POST', '/projects', input, { ws: true });
  }
  delete(id: string) { return this.z._request('DELETE', `/projects/${id}`, undefined, { ws: true }); }

  // Custom domain mapping
  addDomain(projectId: string, domain: string) {
    return this.z._request('POST', `/projects/${projectId}/domain`, { domain }, { ws: true });
  }
  listDomains(projectId: string) {
    return this.z._request('GET', `/projects/${projectId}/domains`, undefined, { ws: true });
  }
  removeDomain(projectId: string, domain: string) {
    return this.z._request('DELETE', `/projects/${projectId}/domain/${domain}`, undefined, { ws: true });
  }
}

// ─── Data (L2) ──────────────────────────────────────────────
class DataAPI {
  constructor(private z: ZeniCloud) {}
  listDatabases() { return this.z._request('GET', '/data/databases', undefined, { ws: true }); }
  listTables() { return this.z._request('GET', '/data/tables', undefined, { ws: true }); }
  query(input: { qtype: 'sql' | 'vector' | 'object'; target: string; query: string }) {
    return this.z._request('POST', '/data/query', input, { ws: true });
  }
  /** Convenience: execute SQL string */
  sql(sqlStmt: string, target = '_') {
    return this.query({ qtype: 'sql', target, query: sqlStmt });
  }
}

// ─── AI Core (L3) ───────────────────────────────────────────
class AIAPI {
  constructor(private z: ZeniCloud) {}
  models() { return this.z._request('GET', '/ai/models'); }

  complete(input: { model: string; prompt: string; system?: string; temperature?: number; max_tokens?: number }) {
    return this.z._request('POST', '/ai/complete', input, { ws: true });
  }

  generateImage(input: {
    prompt: string;
    aspect_ratio?: '1:1' | '9:16' | '16:9' | '3:4' | '4:3';
    n?: 1 | 2 | 3 | 4;
    negative_prompt?: string;
    seed?: number;
  }) {
    return this.z._request<{
      model: string; count: number; aspect_ratio: string;
      images: Array<{ data_uri: string; size_bytes: number }>;
      cost_usd: number;
    }>('POST', '/ai/generate-image', input, { ws: true });
  }

  analyzeImage(input: {
    prompt: string;
    image_data_uri?: string;
    image_url?: string;
    model?: string;
    max_tokens?: number;
    temperature?: number;
  }) {
    return this.z._request('POST', '/ai/analyze-image', input, { ws: true });
  }

  embed(input: { texts: string[]; model?: string; task_type?: string }) {
    return this.z._request('POST', '/ai/embed', input, { ws: true });
  }

  /** Streaming completion via SSE.
   * Returns async iterator yielding text chunks. */
  async *stream(input: { prompt: string; model?: string; system?: string; temperature?: number; max_tokens?: number }) {
    const url = `${(this.z as any).baseUrl}/ai/complete-stream?ws=${(this.z as any).workspace}`;
    const r = await fetch(url, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${(this.z as any).token}`,
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream',
      },
      body: JSON.stringify(input),
    });
    if (!r.ok || !r.body) {
      throw new ZeniError(r.status, await r.text());
    }
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop() || '';
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try {
            const event = JSON.parse(line.slice(6));
            if (event.chunk) yield event.chunk as string;
            if (event.done) return;
          } catch {}
        }
      }
    }
  }
}

// ─── Specialized Design Agents ──────────────────────────────
class AgentsAPI {
  public architecture: SpecializedAgent;
  public interior: SpecializedAgent;
  public product: SpecializedAgent;
  public fashion: SpecializedAgent;
  public structural: SpecializedAgent;

  constructor(private z: ZeniCloud) {
    this.architecture = new SpecializedAgent(z, 'architecture');
    this.interior = new SpecializedAgent(z, 'interior');
    this.product = new SpecializedAgent(z, 'product');
    this.fashion = new SpecializedAgent(z, 'fashion');
    this.structural = new SpecializedAgent(z, 'structural');
  }

  kinds() { return this.z._request('GET', '/agents/kinds'); }
}

class SpecializedAgent {
  constructor(private z: ZeniCloud, private kind: string) {}
  run(input: AgentRunInput) {
    return this.z._request<AgentRunResult>('POST', `/agents/${this.kind}/run`, input, { ws: true });
  }
  runStructured(brief: any, opts: { n_renders_per_room?: number; aspect_ratio?: string; enable_verify?: boolean } = {}) {
    return this.z._request('POST', `/agents/${this.kind}/run-structured`, brief, {
      ws: true,
      query: opts,
    });
  }
  schema() {
    return this.z._request('GET', `/agents/${this.kind}/structured-schema`);
  }
  refine(input: { previous_concept: string; feedback: string; keep_concept?: boolean; n_renders?: number; aspect_ratio?: string }) {
    return this.z._request('POST', `/agents/${this.kind}/refine`, input, { ws: true });
  }
}

// ─── Automation (L4) ────────────────────────────────────────
class AutomationAPI {
  constructor(private z: ZeniCloud) {}
  catalog() { return this.z._request('GET', '/automation/catalog'); }
  listConnectors() { return this.z._request('GET', '/automation/connectors', undefined, { ws: true }); }
  addConnector(input: { type: string; config: Record<string, any> }) {
    return this.z._request('POST', '/automation/connectors', input, { ws: true });
  }
  deleteConnector(id: string) {
    return this.z._request('DELETE', `/automation/connectors/${id}`, undefined, { ws: true });
  }
  fireEvent(input: { source: string; action: string; payload?: any; connector_id?: string }) {
    return this.z._request('POST', '/automation/events/fire', input, { ws: true });
  }
  listEvents(limit = 50) { return this.z._request('GET', '/automation/events', undefined, { ws: true, query: { limit } }); }
  // DLQ / retry
  listWebhookAttempts(opts: { status?: 'pending' | 'succeeded' | 'failed' | 'dlq'; limit?: number } = {}) {
    return this.z._request('GET', '/automation/webhook-attempts', undefined, { ws: true, query: opts });
  }
  retryDLQ(attempt_id: number) {
    return this.z._request('POST', `/automation/webhook-attempts/${attempt_id}/retry`, undefined, { ws: true });
  }
  // Crons
  listCrons() { return this.z._request('GET', '/automation/crons', undefined, { ws: true }); }
  createCron(input: { name: string; schedule: string; target_url: string; method?: string; headers?: any; body?: string; timezone?: string; description?: string }) {
    return this.z._request('POST', '/automation/crons', input, { ws: true });
  }
  deleteCron(name: string) { return this.z._request('DELETE', `/automation/crons/${name}`, undefined, { ws: true }); }
  pauseCron(name: string) { return this.z._request('POST', `/automation/crons/${name}/pause`, undefined, { ws: true }); }
  resumeCron(name: string) { return this.z._request('POST', `/automation/crons/${name}/resume`, undefined, { ws: true }); }
  runCronNow(name: string) { return this.z._request('POST', `/automation/crons/${name}/run-now`, undefined, { ws: true }); }
}

// ─── Identity (L5) ──────────────────────────────────────────
class IdentityAPI {
  constructor(private z: ZeniCloud) {}
  listSecrets() { return this.z._request('GET', '/identity/secrets', undefined, { ws: true }); }
  createSecret(input: { name: string; value: string; env?: 'dev' | 'staging' | 'prod' }) {
    return this.z._request('POST', '/identity/secrets', input, { ws: true });
  }
  deleteSecret(id: string) {
    return this.z._request('DELETE', `/identity/secrets/${id}`, undefined, { ws: true });
  }
  rotateSecret(id: string) { return this.z._request('POST', `/identity/secrets/${id}/rotate`, undefined, { ws: true }); }
  revealSecret(id: string) { return this.z._request('GET', `/identity/secrets/${id}/reveal`, undefined, { ws: true }); }
  // MFA
  setupMFA() { return this.z._request('POST', '/auth/mfa/setup', {}); }
  verifyMFA(code: string) { return this.z._request('POST', '/auth/mfa/verify', { code }); }
  disableMFA(password: string, code: string) {
    return this.z._request('POST', '/auth/mfa/disable', { password, code });
  }
}

// ─── Web3 (L6) ──────────────────────────────────────────────
class Web3API {
  constructor(private z: ZeniCloud) {}
  chains() { return this.z._request('GET', '/web3/chains'); }
  zeniStack() { return this.z._request('GET', '/web3/zeni-stack'); }
  read(input: { chain: string; address: string; owner?: string; kind?: 'erc20' | 'erc721' | 'native' }) {
    return this.z._request('POST', '/web3/read', input);
  }
  txReceipt(chain: string, txHash: string) {
    return this.z._request('GET', `/web3/tx/${chain}/${txHash}`);
  }
  listContracts() { return this.z._request('GET', '/web3/contracts', undefined, { ws: true }); }
}

// ─── Billing ────────────────────────────────────────────────
class BillingAPI {
  constructor(private z: ZeniCloud) {}
  wallet() { return this.z._request('GET', '/billing/wallet', undefined, { ws: true }); }
  subscription() { return this.z._request('GET', '/billing/subscription', undefined, { ws: true }); }
  transactions(limit = 20) {
    return this.z._request('GET', '/billing/transactions', undefined, { ws: true, query: { limit } });
  }
  priceBook() { return this.z._request('GET', '/billing/price-book'); }
  // Dashboard
  dashboardSummary(days = 30) {
    return this.z._request('GET', '/billing/dashboard/summary', undefined, { ws: true, query: { days } });
  }
  dashboardTimeseries(days = 30, granularity: 'hour' | 'day' = 'day') {
    return this.z._request('GET', '/billing/dashboard/timeseries', undefined, { ws: true, query: { days, granularity } });
  }
  topActions(days = 30, limit = 20) {
    return this.z._request('GET', '/billing/dashboard/top-actions', undefined, { ws: true, query: { days, limit } });
  }
}

// ─── API Tokens ─────────────────────────────────────────────
class TokensAPI {
  constructor(private z: ZeniCloud) {}
  list() { return this.z._request('GET', '/api-tokens', undefined, { ws: true }); }
  create(input: { name: string; scopes?: string; expires_in_days?: number }) {
    return this.z._request<{
      id: string; name: string; scopes: string; token_prefix: string;
      workspace_id: string; token: string; // FULL TOKEN — store immediately
    }>('POST', '/api-tokens', input, { ws: true });
  }
  revoke(token_id: string) { return this.z._request('DELETE', `/api-tokens/${token_id}`, undefined, { ws: true }); }
}

// ─── Email ──────────────────────────────────────────────────
class EmailAPI {
  constructor(private z: ZeniCloud) {}
  send(input: {
    to: string | string[];
    subject: string;
    body_html: string;
    body_text?: string;
    reply_to?: string;
    tag?: string;
  }) {
    return this.z._request<{
      sent: number; failed: number; cost_vnd: number;
      message_ids: string[]; quota_remaining_today: number;
    }>('POST', '/email/send', input, { ws: true });
  }
  quota() {
    return this.z._request<{
      tier: string; daily_cap: number; sent_last_24h: number;
      remaining: number; cost_per_email_vnd: number;
    }>('GET', '/email/quota', undefined, { ws: true });
  }
}

// ─── Default export ─────────────────────────────────────────
export default ZeniCloud;
