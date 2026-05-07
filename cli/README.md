# Zeni Cloud CLI

> Deploy apps to [Zeni Cloud](https://zenicloud.io) from terminal — no GitHub, no Docker required.

## Install

```bash
npm install -g @zenicloud/cli
```

## Quick Start

```bash
# 1. Login (paste API token from dashboard)
zeni login --token zeni_pat_xxx --workspace your-workspace

# 2. Init project (auto-detect framework)
cd /path/to/my-app
zeni init

# 3. Deploy (zip + upload + build + live URL)
zeni deploy
# → ⏳ Zipping 247 files (2.4 MB)
# → ⏳ Uploading + queueing build...
# → ✓ DEPLOYED
# → URL: https://my-app-xxx.run.app

# 4. List + manage
zeni list
zeni open my-app
zeni logs my-app
```

## Commands

| Command | Description |
|---------|-------------|
| `zeni login` | Login (interactive email/pass or `--token`) |
| `zeni whoami` | Show login + verify backend |
| `zeni logout` | Clear local config |
| `zeni init` | Auto-detect framework → create `zeni.json` |
| `zeni deploy` | Deploy current dir |
| `zeni list` | List projects |
| `zeni logs <name>` | View logs |
| `zeni open [name]` | Open URL in browser |

## zeni.json

```json
{
  "name": "my-app",
  "framework": "nextjs",
  "port": 3000,
  "region": "asia-southeast1",
  "size": "s",
  "env_vars": { "NODE_ENV": "production" },
  "ignore": ["node_modules", ".git"]
}
```

## Frameworks supported

- `auto` — detect from files
- `nextjs`, `react`, `vue`, `static` — frontend
- `fastapi`, `express` — backend
- `custom` — uses your `Dockerfile`

## Env Variables

- `ZENI_TOKEN` — override token
- `ZENI_WORKSPACE` — override workspace
- `ZENI_API` — override API base
- `ZENI_DEBUG=1` — show stack traces

## Links

- [Docs](https://zenicloud.io/docs/cli)
- [AI Agent Guide](https://zenicloud.io/docs/ai-agent-deploy.html)
- [MCP Server](https://github.com/zenicloud/mcp) (for Claude Desktop/Cursor)
