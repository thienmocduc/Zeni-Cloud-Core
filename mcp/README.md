# Zeni Cloud MCP Server

> Model Context Protocol server cho [Zeni Cloud](https://zenicloud.io) — 12 tools cho Claude Desktop, Cursor, Replit Agents tự deploy + manage apps.

## Install

```bash
npm install -g @zenicloud/mcp
```

## Setup cho Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (Mac) hoặc `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "zeni": {
      "command": "npx",
      "args": ["@zenicloud/mcp"],
      "env": {
        "ZENI_TOKEN": "zeni_pat_xxx",
        "ZENI_WORKSPACE": "your-workspace"
      }
    }
  }
}
```

Restart Claude Desktop. Bạn sẽ thấy 12 tools `zeni_*` trong AI session.

## Setup cho Cursor

`Settings → MCP → Add Server`:

```json
{
  "name": "zeni",
  "command": "npx @zenicloud/mcp",
  "env": {
    "ZENI_TOKEN": "zeni_pat_xxx",
    "ZENI_WORKSPACE": "your-workspace"
  }
}
```

## 12 Tools available

| Tool | Description |
|------|-------------|
| `zeni_deploy` | Deploy app (image URL or ZIP base64) |
| `zeni_list_projects` | List all projects |
| `zeni_get_project` | Project detail |
| `zeni_redeploy` | Re-deploy project |
| `zeni_ai_call` | Call ZeniRouter AI (Claude/GPT/Gemini smart routing) |
| `zeni_vector_create_collection` | Create pgvector collection |
| `zeni_vector_search` | Semantic search |
| `zeni_secret_set` | Store secret in encrypted vault |
| `zeni_billing_status` | Get subscription + quota |
| `zeni_trial_status` | 14-day trial status |
| `zeni_audit_log` | Recent workspace actions |
| `zeni_oauth_provider_setup` | Setup Zalo/Apple/Facebook/etc OAuth |

## Example AI conversation

> **You**: "Deploy my Next.js app to Zeni Cloud"
>
> **Claude**: I'll help you deploy. Let me first list your current projects.
> *(calls zeni_list_projects)*
> 
> Now I'll zip your code and deploy it.
> *(calls zeni_deploy with zip_base64 + framework=nextjs)*
> 
> ✅ Deployed! Your app is live at https://my-app-xxx.run.app

## Get your token

1. Login at https://zenicloud.io/app
2. Tab "API Tokens" → "+ Create Token"
3. Scope: `full` (or `deploy` for read-only AI)
4. Copy token starting with `zeni_pat_`

## Links

- [Zeni Cloud Docs](https://zenicloud.io/docs)
- [CLI alternative](https://github.com/zenicloud/cli)
- [API Reference](https://zenicloud.io/api/v1/openapi.json)
