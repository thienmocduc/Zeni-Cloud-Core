"""
Cloud Run custom domain mapping service.

Workflow:
  1. Customer chooses domain (e.g., app.nexdesign.vn)
  2. POST /projects/{id}/domain maps it to their Cloud Run service
  3. Backend uses gcloud Cloud Run domain-mappings API
  4. Returns DNS records customer must add at their registrar
  5. Once DNS propagates, Google issues SSL cert auto

Note: Cloud Run domain mapping requires the domain to be VERIFIED in Search Console.
Verification can be auto if customer's domain DNS is delegated to Cloud DNS Zeni.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from functools import lru_cache

from google.api_core import exceptions as gcp_exceptions

from app.core.config import settings

log = logging.getLogger("zeni.domain_mapping")

DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.IGNORECASE)


def is_valid_domain(domain: str) -> bool:
    return bool(DOMAIN_RE.match(domain)) and len(domain) <= 253


@lru_cache(maxsize=1)
def _client():
    from google.cloud import run_v2
    return run_v2.ServicesClient()


def list_mapped_domains(service_name: str, region: str = "us-central1") -> list[dict[str, Any]]:
    """List all custom domains mapped to a Cloud Run service."""
    # Cloud Run v2 SDK uses DomainMappings via different client
    try:
        from googleapiclient import discovery
        run_v1 = discovery.build("run", "v1", cache_discovery=False)
        parent = f"namespaces/{settings.gcp_project_id}"
        result = run_v1.namespaces().domainmappings().list(parent=parent).execute()
        items = result.get("items", [])
        out = []
        for it in items:
            spec = it.get("spec", {})
            status = it.get("status", {})
            if spec.get("routeName") == service_name:
                out.append({
                    "domain": it.get("metadata", {}).get("name"),
                    "service": spec.get("routeName"),
                    "ready": any(c.get("type") == "Ready" and c.get("status") == "True"
                                 for c in status.get("conditions", [])),
                    "dns_records": status.get("resourceRecords", []),
                })
        return out
    except Exception as e:
        log.warning("list_mapped_domains failed: %s", e)
        return []


def create_domain_mapping(*, domain: str, service_name: str,
                           region: str = "asia-southeast1") -> dict[str, Any]:
    """
    Create domain mapping for Cloud Run service.

    Strategy (cascading):
      1. Try gcloud CLI subprocess (most stable — uses official gcloud auth)
      2. Fall back to googleapiclient REST API
      3. Fall back to manual DNS instructions (with Cloudflare proxy alternative)

    Returns dict with state, dns_records_to_add, instructions.
    """
    if not is_valid_domain(domain):
        raise ValueError(f"Domain '{domain}' không hợp lệ")

    # ─── STRATEGY 1: gcloud CLI subprocess (preferred) ────────────────
    try:
        import subprocess
        # gcloud run domain-mappings create supports both v1 (legacy) and v2 (managed)
        # Use beta to ensure latest features
        result = subprocess.run([
            "gcloud", "beta", "run", "domain-mappings", "create",
            f"--service={service_name}",
            f"--domain={domain}",
            f"--region={region}",
            "--format=json",
            "--quiet",
        ], capture_output=True, text=True, timeout=60)

        if result.returncode == 0:
            import json as _json
            try:
                data = _json.loads(result.stdout)
            except Exception:
                data = {}
            records = data.get("status", {}).get("resourceRecords") or [
                {"type": "CNAME", "name": domain, "rrdata": "ghs.googlehosted.com."},
            ]
            return {
                "domain": domain,
                "service": service_name,
                "state": "PENDING_DNS_PROPAGATION",
                "region": region,
                "ssl_cert": "Google-managed (auto provision sau DNS active 10-30 phút)",
                "dns_records_to_add": records,
                "method": "gcloud_cli",
                "instructions": (
                    f"✅ Domain mapping created. Tiếp theo:\n\n"
                    f"1. Add DNS record tại registrar (Namecheap/Cloudflare/GoDaddy):\n"
                    f"   CNAME    {domain}    ghs.googlehosted.com.\n"
                    f"   (Hoặc A record về 4 IPs Google nếu apex domain)\n\n"
                    f"2. Đợi DNS propagate 5-30 phút (check: https://dnschecker.org)\n\n"
                    f"3. SSL Let's Encrypt tự cấp 10-30 phút sau khi DNS active.\n\n"
                    f"4. Domain LIVE tại https://{domain}"
                ),
            }
        # gcloud failed — log + fall through
        log.warning("gcloud domain-mappings create failed: %s", result.stderr[:300])
    except subprocess.TimeoutExpired:
        log.warning("gcloud domain-mappings timed out (60s)")
    except FileNotFoundError:
        log.warning("gcloud CLI not available in container")
    except Exception as e:
        log.warning("gcloud CLI error: %s", e)

    # ─── STRATEGY 2: REST API fallback ────────────────
    try:
        from googleapiclient import discovery
        run_v1 = discovery.build(
            "run", "v1",
            discoveryServiceUrl=f"https://{region}-run.googleapis.com/$discovery/rest?version=v1",
            cache_discovery=False
        )
        parent = f"namespaces/{settings.gcp_project_id}"
        body = {
            "apiVersion": "domains.cloudrun.com/v1",
            "kind": "DomainMapping",
            "metadata": {
                "name": domain,
                "namespace": settings.gcp_project_id,
                "labels": {"zeni-managed": "true"},
            },
            "spec": {
                "routeName": service_name,
                "certificateMode": "AUTOMATIC",
            },
        }
        result = run_v1.namespaces().domainmappings().create(parent=parent, body=body).execute()
        return {
            "domain": domain,
            "service": service_name,
            "state": "PENDING_DNS_PROPAGATION",
            "region": region,
            "ssl_cert": "Google-managed",
            "dns_records_to_add": result.get("status", {}).get("resourceRecords") or [
                {"type": "CNAME", "name": domain, "rrdata": "ghs.googlehosted.com."},
            ],
            "method": "rest_api",
            "instructions": f"Add CNAME {domain} → ghs.googlehosted.com → đợi DNS + SSL.",
        }
    except Exception as e:
        log.warning("REST API fallback also failed: %s", e)

    # ─── STRATEGY 3: Manual instructions with Cloudflare alternative ────────
    return {
        "domain": domain,
        "service": service_name,
        "state": "MANUAL_SETUP_REQUIRED",
        "region": region,
        "ssl_cert": "Manual setup via Cloudflare (recommended) hoặc gcloud CLI",
        "dns_records_to_add": [
            {"type": "CNAME", "name": domain, "value": "ghs.googlehosted.com."},
        ],
        "method": "manual",
        "instructions": (
            f"⚠ Auto domain mapping tạm thời không khả dụng. 2 options:\n\n"
            f"OPTION A (Recommended) — Cloudflare free proxy (~10 phút):\n"
            f"  1. Đăng ký https://cloudflare.com (free)\n"
            f"  2. Add site {domain} → Cloudflare cấp 2 NS records\n"
            f"  3. Đổi nameservers tại registrar sang Cloudflare\n"
            f"  4. Trong Cloudflare DNS: add CNAME {domain} → {service_name}-xxx.asia-southeast1.run.app\n"
            f"  5. Bật proxy orange cloud → Universal SSL + WAF + CDN free\n\n"
            f"OPTION B — Manual via gcloud (cần GCP CLI):\n"
            f"  gcloud beta run domain-mappings create --service={service_name} \\\n"
            f"    --domain={domain} --region={region}\n\n"
            f"OPTION C — Email support@zenicloud.io: em setup trong 5 phút."
        ),
    }


def delete_domain_mapping(domain: str) -> None:
    """Remove a domain mapping."""
    try:
        from googleapiclient import discovery
        run_v1 = discovery.build("run", "v1", cache_discovery=False)
        name = f"namespaces/{settings.gcp_project_id}/domainmappings/{domain}"
        run_v1.namespaces().domainmappings().delete(name=name).execute()
    except Exception as e:
        log.warning("delete_domain_mapping failed: %s", e)
        raise
