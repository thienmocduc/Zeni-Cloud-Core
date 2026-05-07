"""Smoke test for v111: PWA templates + Build Farm + Edge Runtime endpoints."""
import json
import os
import sys
import urllib.request
import urllib.error

API = os.environ.get("API_BASE", "https://zenicloud.io/api/v1")
TOKEN = os.environ.get("ZENI_TOKEN", "")
WS = os.environ.get("ZENI_WORKSPACE", "holdings")


def call(method: str, path: str, body=None, expect_status=None):
    url = API + path
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")
    except Exception as e:
        return 0, {"error": str(e)}


PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
results = []


def check(name, status, expected_status, body=None):
    ok = status == expected_status
    results.append((name, ok, status, body))
    icon = PASS if ok else FAIL
    print(f"{icon} {name} → HTTP {status} (expected {expected_status})")
    if not ok and body:
        print(f"  └─ {body}")


# Test 1: PWA Frameworks live
status, body = call("GET", "/github/frameworks")
check("PWA frameworks list returns 8 (was 6)", status, 200)
if status == 200:
    fw_names = [f["framework"] for f in body]
    assert "nextjs-pwa" in fw_names, f"Missing nextjs-pwa, got: {fw_names}"
    assert "vite-pwa" in fw_names, f"Missing vite-pwa, got: {fw_names}"
    print(f"  └─ Frameworks: {sorted(fw_names)}")

# Test 2: Build Farm toolchains list
status, body = call("GET", "/build-farm/toolchains")
check("Build Farm toolchains endpoint", status, 200)
if status == 200:
    print(f"  └─ {len(body)} toolchains: {[t['id'] for t in body]}")

# Test 3: Build Farm quotas (auth required)
status, body = call("GET", f"/build-farm/quotas?ws={WS}")
check("Build Farm quotas endpoint", status, 200 if TOKEN else 401)

# Test 4: Edge Runtime list
status, body = call("GET", "/edge/runtimes")
check("Edge Runtime list endpoint", status, 200)
if status == 200:
    print(f"  └─ {len(body)} runtimes: {[r['id'] for r in body]}")

# Test 5: Edge Runtime quotas (auth required)
status, body = call("GET", f"/edge/quotas?ws={WS}")
check("Edge Runtime quotas endpoint", status, 200 if TOKEN else 401)

# Summary
total = len(results)
passed = sum(1 for _, ok, *_ in results if ok)
print(f"\n{'='*50}\n{passed}/{total} passed")
sys.exit(0 if passed == total else 1)
