"""
Zeni Cloud SDK · Python.

    pip install zenicloud

Usage:
    from zenicloud import ZeniCloud

    zeni = ZeniCloud(token=os.environ["ZENI_TOKEN"], workspace="nexbuild")

    result = zeni.agents.architecture.run(
        brief="Biet thu 3 tang 200m2 Tropical Modern",
        generate_renders=True, n_renders=2,
    )
    print(result["concept"][:500])
"""
from __future__ import annotations

import json
import os
from typing import Any, Iterator

import httpx

__version__ = "1.0.0"
__all__ = ["ZeniCloud", "ZeniError"]


class ZeniError(Exception):
    def __init__(self, status: int, detail: Any):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class ZeniCloud:
    """Main client. Subclients: auth, projects, data, ai, agents, automation,
    identity, web3, billing, tokens."""

    def __init__(
        self,
        token: str | None = None,
        workspace: str = "",
        base_url: str = "https://zenicloud.io/api/v1",
        timeout: float = 120.0,
    ):
        self.token = token or os.environ.get("ZENI_TOKEN")
        if not self.token:
            raise ValueError("Zeni token required (pass token= or set ZENI_TOKEN env)")
        self.workspace = workspace
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": f"zenicloud-python/{__version__}",
            },
        )
        self._async_client = httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout,
            headers={"Authorization": f"Bearer {self.token}",
                     "User-Agent": f"zenicloud-python/{__version__}"},
        )
        # Resources
        self.auth = _Auth(self)
        self.projects = _Projects(self)
        self.data = _Data(self)
        self.ai = _AI(self)
        self.agents = _Agents(self)
        self.automation = _Automation(self)
        self.identity = _Identity(self)
        self.web3 = _Web3(self)
        self.billing = _Billing(self)
        self.tokens = _Tokens(self)
        self.email = _Email(self)

    def set_workspace(self, ws: str) -> None:
        self.workspace = ws

    def close(self):
        self._client.close()

    def _request(self, method: str, path: str, *, body: Any = None,
                 ws: bool | str = False, params: dict | None = None) -> Any:
        q = dict(params or {})
        ws_value = self.workspace if ws is True else (ws if isinstance(ws, str) else None)
        if ws_value:
            q["ws"] = ws_value
        r = self._client.request(method, path, json=body, params=q)
        try:
            data = r.json()
        except Exception:
            data = r.text
        if r.status_code >= 400:
            raise ZeniError(r.status_code, data.get("detail", data) if isinstance(data, dict) else data)
        return data


# ─── Sub-resources ─────────────────────────────────────────
class _Auth:
    def __init__(self, z: ZeniCloud): self._z = z
    def me(self): return self._z._request("GET", "/auth/me")
    def refresh(self, refresh_token: str): return self._z._request("POST", "/auth/refresh", body={"refresh_token": refresh_token})


class _Projects:
    def __init__(self, z: ZeniCloud): self._z = z
    def list(self): return self._z._request("GET", "/projects", ws=True)
    def get(self, id: str): return self._z._request("GET", f"/projects/{id}", ws=True)
    def deploy(self, name: str, image: str, *, type: str = "web", size: str = "s",
               region: str = "us-central1", port: int = 8080,
               allow_unauthenticated: bool = True,
               env_vars: dict | None = None, secrets: dict | None = None,
               git_ref: str = "main", runtime: str = "container") -> dict:
        return self._z._request("POST", "/projects", ws=True, body={
            "name": name, "type": type, "runtime": runtime,
            "size": size, "region": region, "image": image,
            "port": port, "allow_unauthenticated": allow_unauthenticated,
            "env_vars": env_vars, "secrets": secrets, "git_ref": git_ref,
        })
    def delete(self, id: str): return self._z._request("DELETE", f"/projects/{id}", ws=True)
    def add_domain(self, project_id: str, domain: str):
        return self._z._request("POST", f"/projects/{project_id}/domain", ws=True, body={"domain": domain})
    def list_domains(self, project_id: str):
        return self._z._request("GET", f"/projects/{project_id}/domains", ws=True)


class _Data:
    def __init__(self, z: ZeniCloud): self._z = z
    def list_databases(self): return self._z._request("GET", "/data/databases", ws=True)
    def list_tables(self): return self._z._request("GET", "/data/tables", ws=True)
    def query(self, sql: str, target: str = "_", qtype: str = "sql"):
        return self._z._request("POST", "/data/query", ws=True,
                                 body={"qtype": qtype, "target": target, "query": sql})
    def sql(self, sql: str, target: str = "_"):
        return self.query(sql, target=target, qtype="sql")


class _AI:
    def __init__(self, z: ZeniCloud): self._z = z
    def models(self): return self._z._request("GET", "/ai/models")
    def complete(self, prompt: str, *, model: str = "gemini-2.5-flash",
                 system: str | None = None, temperature: float = 0.7, max_tokens: int = 2048):
        return self._z._request("POST", "/ai/complete", ws=True, body={
            "prompt": prompt, "model": model, "system": system,
            "temperature": temperature, "max_tokens": max_tokens,
        })
    def generate_image(self, prompt: str, *, aspect_ratio: str = "16:9", n: int = 1,
                       negative_prompt: str | None = None, seed: int | None = None):
        return self._z._request("POST", "/ai/generate-image", ws=True, body={
            "prompt": prompt, "aspect_ratio": aspect_ratio, "n": n,
            "negative_prompt": negative_prompt, "seed": seed,
        })
    def analyze_image(self, prompt: str, *, image_url: str | None = None,
                      image_data_uri: str | None = None,
                      model: str = "gemini-2.5-flash", max_tokens: int = 2048):
        return self._z._request("POST", "/ai/analyze-image", ws=True, body={
            "prompt": prompt, "image_url": image_url,
            "image_data_uri": image_data_uri, "model": model, "max_tokens": max_tokens,
        })
    def embed(self, texts: list[str], *, model: str = "text-embedding-004",
              task_type: str = "RETRIEVAL_DOCUMENT"):
        return self._z._request("POST", "/ai/embed", ws=True, body={
            "texts": texts, "model": model, "task_type": task_type,
        })
    def stream(self, prompt: str, *, model: str = "gemini-2.5-flash",
               system: str | None = None, temperature: float = 0.7,
               max_tokens: int = 2048) -> Iterator[str]:
        """Streaming completion. Yields text chunks."""
        url = f"{self._z.base_url}/ai/complete-stream?ws={self._z.workspace}"
        with httpx.stream("POST", url, headers={
            "Authorization": f"Bearer {self._z.token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }, json={"prompt": prompt, "model": model, "system": system,
                 "temperature": temperature, "max_tokens": max_tokens},
        timeout=300.0) as r:
            for line in r.iter_lines():
                if line.startswith("data: "):
                    try:
                        event = json.loads(line[6:])
                        if "chunk" in event: yield event["chunk"]
                        if event.get("done"): return
                    except Exception:
                        pass


class _Agents:
    """5 specialized design agents."""
    def __init__(self, z: ZeniCloud):
        self._z = z
        self.architecture = _AgentKind(z, "architecture")
        self.interior = _AgentKind(z, "interior")
        self.product = _AgentKind(z, "product")
        self.fashion = _AgentKind(z, "fashion")
        self.structural = _AgentKind(z, "structural")
    def kinds(self): return self._z._request("GET", "/agents/kinds")


class _AgentKind:
    def __init__(self, z: ZeniCloud, kind: str):
        self._z = z; self.kind = kind
    def run(self, brief: str, *, generate_renders: bool = True, n_renders: int = 2,
            aspect_ratio: str = "16:9", reference_image_url: str | None = None,
            constraints: dict | None = None) -> dict:
        return self._z._request("POST", f"/agents/{self.kind}/run", ws=True, body={
            "brief": brief, "generate_renders": generate_renders,
            "n_renders": n_renders, "aspect_ratio": aspect_ratio,
            "reference_image_url": reference_image_url,
            "constraints": constraints or {},
        })
    def run_structured(self, brief: dict, *, n_renders_per_room: int = 2,
                        aspect_ratio: str = "16:9", enable_verify: bool = True):
        return self._z._request("POST", f"/agents/{self.kind}/run-structured", ws=True,
                                 body=brief, params={
                                     "n_renders_per_room": n_renders_per_room,
                                     "aspect_ratio": aspect_ratio,
                                     "enable_verify": str(enable_verify).lower(),
                                 })
    def schema(self): return self._z._request("GET", f"/agents/{self.kind}/structured-schema")
    def refine(self, previous_concept: str, feedback: str, *,
               keep_concept: bool = False, n_renders: int = 2):
        return self._z._request("POST", f"/agents/{self.kind}/refine", ws=True, body={
            "previous_concept": previous_concept, "feedback": feedback,
            "keep_concept": keep_concept, "n_renders": n_renders,
        })


class _Automation:
    def __init__(self, z: ZeniCloud): self._z = z
    def list_connectors(self): return self._z._request("GET", "/automation/connectors", ws=True)
    def add_connector(self, type: str, config: dict):
        return self._z._request("POST", "/automation/connectors", ws=True,
                                 body={"type": type, "config": config})
    def fire_event(self, source: str, action: str, payload: dict | None = None,
                   connector_id: str | None = None):
        return self._z._request("POST", "/automation/events/fire", ws=True, body={
            "source": source, "action": action, "payload": payload or {},
            "connector_id": connector_id,
        })
    def list_crons(self): return self._z._request("GET", "/automation/crons", ws=True)
    def create_cron(self, name: str, schedule: str, target_url: str, *,
                    method: str = "POST", headers: dict | None = None,
                    body: str | None = None, timezone: str = "Asia/Ho_Chi_Minh"):
        return self._z._request("POST", "/automation/crons", ws=True, body={
            "name": name, "schedule": schedule, "target_url": target_url,
            "method": method, "headers": headers or {}, "body": body, "timezone": timezone,
        })
    def list_webhook_attempts(self, *, status: str | None = None, limit: int = 50):
        params = {"limit": limit}
        if status: params["status"] = status
        return self._z._request("GET", "/automation/webhook-attempts", ws=True, params=params)


class _Identity:
    def __init__(self, z: ZeniCloud): self._z = z
    def list_secrets(self): return self._z._request("GET", "/identity/secrets", ws=True)
    def create_secret(self, name: str, value: str, env: str = "prod"):
        return self._z._request("POST", "/identity/secrets", ws=True, body={
            "name": name, "value": value, "env": env,
        })
    def setup_mfa(self): return self._z._request("POST", "/auth/mfa/setup", body={})
    def verify_mfa(self, code: str): return self._z._request("POST", "/auth/mfa/verify", body={"code": code})


class _Web3:
    def __init__(self, z: ZeniCloud): self._z = z
    def chains(self): return self._z._request("GET", "/web3/chains")
    def zeni_stack(self): return self._z._request("GET", "/web3/zeni-stack")
    def read(self, chain: str, address: str, *, kind: str = "erc20", owner: str | None = None):
        return self._z._request("POST", "/web3/read", body={
            "chain": chain, "address": address, "kind": kind, "owner": owner,
        })


class _Billing:
    def __init__(self, z: ZeniCloud): self._z = z
    def wallet(self): return self._z._request("GET", "/billing/wallet", ws=True)
    def subscription(self): return self._z._request("GET", "/billing/subscription", ws=True)
    def transactions(self, limit: int = 20):
        return self._z._request("GET", "/billing/transactions", ws=True, params={"limit": limit})
    def dashboard_summary(self, days: int = 30):
        return self._z._request("GET", "/billing/dashboard/summary", ws=True, params={"days": days})


class _Tokens:
    def __init__(self, z: ZeniCloud): self._z = z
    def list(self): return self._z._request("GET", "/api-tokens", ws=True)
    def create(self, name: str, scopes: str = "ai", expires_in_days: int = 365):
        return self._z._request("POST", "/api-tokens", ws=True, body={
            "name": name, "scopes": scopes, "expires_in_days": expires_in_days,
        })
    def revoke(self, token_id: str):
        return self._z._request("DELETE", f"/api-tokens/{token_id}", ws=True)


class _Email:
    def __init__(self, z: ZeniCloud): self._z = z
    def send(self, *, to, subject: str, body_html: str,
             body_text: str | None = None, reply_to: str | None = None,
             tag: str | None = None) -> dict:
        return self._z._request("POST", "/email/send", ws=True, body={
            "to": to, "subject": subject, "body_html": body_html,
            "body_text": body_text, "reply_to": reply_to, "tag": tag,
        })
    def quota(self) -> dict:
        return self._z._request("GET", "/email/quota", ws=True)
