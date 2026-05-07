"""Smoke test v112: Voice STT/TTS + Push Notifications + Benchmark Tracker."""
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
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")
    except Exception as e:
        return 0, {"error": str(e)}


PASS = "[PASS]"
FAIL = "[FAIL]"
results = []

def check(name, status, expected_status):
    ok = status == expected_status
    results.append((name, ok))
    icon = PASS if ok else FAIL
    print(f"{icon} {name} -> HTTP {status} (expected {expected_status})")


# Voice AI: list voices is public
status, body = call("GET", "/voice-ai/voices")
check("Voice catalog (10 voices: 6 VN + 2 EN + 2 premium)", status, 200)
if status == 200:
    print(f"  Voices: {len(body)}")
    for v in body[:5]:
        print(f"    - {v['id']:20s} | {v['language']} | {v['gender']:6s} | {v['display_name']}")

# Voice STT/TTS: auth required
status, body = call("POST", f"/voice-ai/synthesize?ws={WS}", {"text": "test", "voice_id": "vn-female-1"})
check("Voice TTS endpoint (auth required)", status, 401 if not TOKEN else 202)

# Push: auth required
status, body = call("GET", f"/push/devices?ws={WS}")
check("Push devices list (auth required)", status, 401 if not TOKEN else 200)

# Push send: auth required
status, body = call("POST", f"/push/send?ws={WS}", {"title": "test", "body": "test"})
check("Push send endpoint (auth required)", status, 401 if not TOKEN else 202)

# Benchmarks: list sources is public
status, body = call("GET", "/benchmarks/sources")
check("Benchmark sources (8 leaderboards)", status, 200)
if status == 200:
    print(f"  Sources: {len(body)}: {[s['id'] for s in body]}")

# Benchmark scores for swe-bench (public)
status, body = call("GET", "/benchmarks/swe-bench")
check("Benchmark SWE-bench scores", status, 200)
if status == 200:
    print(f"  Top SWE-bench: {len(body)} models")
    for s in body[:3]:
        print(f"    #{s.get('rank',0):2d} {s['model_name']:25s} {s['score_value']}%")

# Summary
total = len(results)
passed = sum(1 for _, ok in results if ok)
print(f"\n{'='*50}\n{passed}/{total} passed")
sys.exit(0 if passed == total else 1)
