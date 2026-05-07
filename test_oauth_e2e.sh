#!/bin/bash
# ═══════════════════════════════════════════════════════════
# E2E test: OAuth providers endpoint + signup page render
# Run after v41 deploy
# ═══════════════════════════════════════════════════════════

BASE="https://zenicloud.io"
PASS=0
FAIL=0

check() {
  local name="$1"; local cond="$2"
  if [ "$cond" = "1" ]; then
    echo "PASS  $name"
    PASS=$((PASS+1))
  else
    echo "FAIL  $name"
    FAIL=$((FAIL+1))
  fi
}

echo "=== 1. /providers endpoint ==="
RESP=$(curl -s "$BASE/api/v1/auth/oauth/providers")
echo "$RESP" | python -m json.tool 2>/dev/null || echo "$RESP"

# Should return 3 providers
COUNT=$(echo "$RESP" | python -c "import sys,json;d=json.load(sys.stdin);print(len(d.get('providers',[])))")
check "/providers returns 3 entries (count=$COUNT)" "$([ "$COUNT" = "3" ] && echo 1 || echo 0)"

echo ""
echo "=== 2. /signup page renders ==="
HTML=$(curl -s "$BASE/signup")
check "signup contains 'Continue with Google'" "$(echo "$HTML" | grep -q 'Continue with Google' && echo 1 || echo 0)"
check "signup contains 'Continue with GitHub'" "$(echo "$HTML" | grep -q 'Continue with GitHub' && echo 1 || echo 0)"
check "signup contains 'Zeni Digital'" "$(echo "$HTML" | grep -q 'Zeni Digital' && echo 1 || echo 0)"

echo ""
echo "=== 3. /authorize endpoint returns 503 (not configured yet) ==="
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/v1/auth/oauth/google/authorize")
check "/google/authorize returns 503 (got $HTTP_CODE)" "$([ "$HTTP_CODE" = "503" ] && echo 1 || echo 0)"

HTTP_CODE2=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/v1/auth/oauth/github/authorize")
check "/github/authorize returns 503 (got $HTTP_CODE2)" "$([ "$HTTP_CODE2" = "503" ] && echo 1 || echo 0)"

echo ""
echo "=== 4. /authorize unknown provider returns 404 ==="
HTTP_CODE3=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/v1/auth/oauth/facebook/authorize")
check "/facebook/authorize returns 404 (got $HTTP_CODE3)" "$([ "$HTTP_CODE3" = "404" ] && echo 1 || echo 0)"

echo ""
echo "=== 5. Health check still working ==="
HEALTH=$(curl -s "$BASE/health")
check "health returns 'ok'" "$(echo "$HEALTH" | grep -q '"status":"ok"' && echo 1 || echo 0)"

echo ""
echo "=== 6. Existing endpoints still work ==="
HTTP_CODE4=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/v1/email/quota?ws=holdings" -H "Authorization: Bearer $ZENI_TOKEN")
check "/email/quota responds (got $HTTP_CODE4)" "$([ "$HTTP_CODE4" = "200" ] || [ "$HTTP_CODE4" = "401" ] && echo 1 || echo 0)"

echo ""
echo "════════════════════════════════"
echo "  PASS: $PASS  /  FAIL: $FAIL"
echo "════════════════════════════════"
exit $FAIL
