"""Smoke test v113: 7 new services (Payouts/Storage/Realtime/MobileCert/Pkg/Voice/Push)."""
import json
import os
import sys
import urllib.request
import urllib.error

API = os.environ.get("API_BASE", "https://zenicloud.io/api/v1")
TOKEN = os.environ.get("ZENI_TOKEN", "")
WS = os.environ.get("ZENI_WORKSPACE", "holdings")


def call(method: str, path: str, body=None):
    url = API + path
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode()[:500]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:500]
    except Exception as e:
        return 0, str(e)[:500]


PASS = "[PASS]"
FAIL = "[FAIL]"
results = []


def check(name, status, expected):
    ok = status in (expected if isinstance(expected, (list, tuple, set)) else [expected])
    results.append((name, ok))
    icon = PASS if ok else FAIL
    print(f"{icon} {name} -> HTTP {status} (expected {expected})")


# 1. Payouts
status, _ = call("GET", f"/payouts/?ws={WS}")
check("Payouts list (auth)", status, [200, 401])
status, _ = call("POST", f"/payouts/?ws={WS}", {"method": "zeni_token", "amount_zeni": 1, "recipient_wallet_address": "0x0"})
check("Payouts create (auth)", status, [202, 401, 422])

# 2. Storage
status, _ = call("GET", f"/storage/buckets?ws={WS}")
check("Storage buckets list (auth)", status, [200, 401])

# 3. Realtime
status, _ = call("GET", f"/realtime/channels?ws={WS}")
check("Realtime channels list (auth)", status, [200, 401])

# 4. Mobile Certs
status, _ = call("GET", f"/identity/mobile-certs/?ws={WS}")
check("Mobile certs list (auth)", status, [200, 401])
status, _ = call("GET", f"/identity/mobile-certs/expiring?ws={WS}&days=30")
check("Mobile certs expiring", status, [200, 401])

# 5. Package Registry
status, _ = call("GET", "/packages/registry-info")
check("Package registry info (public)", status, 200)
status, _ = call("GET", f"/packages/?ws={WS}")
check("Package list (auth)", status, [200, 401])
status, _ = call("GET", "/npm/-/whoami")
check("npm whoami (auth=401 without token)", status, 401)
status, _ = call("GET", "/pypi/simple/")
check("pypi simple index (auth=401)", status, 401)

# 6. Voice (existing scaffold + worker wired)
status, _ = call("GET", "/voice-ai/voices")
check("Voice catalog (public)", status, 200)

# 7. Push (existing scaffold + worker wired)
status, _ = call("GET", f"/push/devices?ws={WS}")
check("Push devices (auth)", status, [200, 401])

# Summary
total = len(results)
passed = sum(1 for _, ok in results if ok)
print(f"\n{'='*60}\n{passed}/{total} passed")
sys.exit(0 if passed == total else 1)
