#!/usr/bin/env bash
# Try shared team hosts end-to-end against a real server + real host daemon.
#
# Stands up a throwaway accounts-mode server on a scratch database, creates
# two real users (alice, bob), connects THIS machine as alice's host, then
# walks the share -> use -> revoke loop.
#
# Nothing here touches ~/.omnigent/chat.db: the server runs with an explicit
# --database-uri. The host daemon still uses this machine's real host id from
# ~/.omnigent/config.yaml, which is why a machine already registered to a
# different owner on the TARGET server is refused (HTTP 409) — that's the
# anti-hijack guard, not a bug.
#
# Usage:  ./scripts/try_host_sharing.sh [port]
set -euo pipefail

PORT="${1:-8913}"
BASE="http://127.0.0.1:${PORT}"
WORK="$(mktemp -d)"
PY="${PY:-.venv/bin/python}"
OMNI="${OMNI:-.venv/bin/omnigent}"

cleanup() {
  [[ -n "${HOST_PID:-}" ]] && kill "$HOST_PID" 2>/dev/null || true
  [[ -n "${SRV_PID:-}" ]] && kill "$SRV_PID" 2>/dev/null || true
}
trap cleanup EXIT

jqp() { "$PY" -c "import sys,json;$1"; }

echo "workdir: $WORK"

# ── server: accounts mode (multi-user), scratch DB ───────────────────
export OMNIGENT_AUTH_ENABLED=1
export OMNIGENT_ACCOUNTS_COOKIE_SECRET="$(openssl rand -hex 32)"  # MUST be hex
unset OMNIGENT_LOCAL_SINGLE_USER  # else a header-less caller becomes "local"

"$OMNI" server --port "$PORT" --host 127.0.0.1 \
  --database-uri "sqlite:///$WORK/clean.db" > "$WORK/server.log" 2>&1 &
SRV_PID=$!

for _ in $(seq 1 20); do
  curl -sf -m 2 "$BASE/health" >/dev/null 2>&1 && break
  sleep 3
done
curl -sf -m 2 "$BASE/health" >/dev/null || { echo "server failed:"; tail "$WORK/server.log"; exit 1; }
echo "server up on $BASE"

# ── two real users: first user claims admin, then invites ────────────
curl -s -X POST "$BASE/auth/setup" -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"alice-password-123"}' >/dev/null
ALICE=$(curl -s -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"alice-password-123"}' | jqp "print(json.load(sys.stdin)['token'])")
INVITE=$(curl -s -X POST "$BASE/auth/invite" -H "Authorization: Bearer $ALICE" \
  -H 'Content-Type: application/json' -d '{}' | jqp "print(json.load(sys.stdin)['token'])")
curl -s -X POST "$BASE/auth/register" -H 'Content-Type: application/json' \
  -d "{\"invite\":\"$INVITE\",\"username\":\"bob\",\"password\":\"bob-password-123\"}" >/dev/null
BOB=$(curl -s -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d '{"username":"bob","password":"bob-password-123"}' | jqp "print(json.load(sys.stdin)['token'])")
echo "alice: $(curl -s "$BASE/auth/me" -H "Authorization: Bearer $ALICE")"
echo "bob:   $(curl -s "$BASE/auth/me" -H "Authorization: Bearer $BOB")"

# ── connect THIS machine as alice's host ─────────────────────────────
# Equivalent to `omnigent login $BASE` (interactive) + `omnigent host $BASE`.
"$PY" - <<PYEOF
import time
from omnigent.cli_auth import store_token
store_token("$BASE", "$ALICE", "alice", time.time() + 28000)
PYEOF

"$OMNI" host "$BASE" > "$WORK/host.log" 2>&1 &
HOST_PID=$!
for _ in $(seq 1 20); do
  grep -q "Connected as" "$WORK/host.log" 2>/dev/null && break
  sleep 3
done
grep -q "Connected as" "$WORK/host.log" || { echo "host failed:"; tail "$WORK/host.log"; exit 1; }

HID=$(curl -s "$BASE/v1/hosts" -H "Authorization: Bearer $ALICE" \
  | jqp "print(json.load(sys.stdin)['hosts'][0]['host_id'])")
echo "host connected: $HID (owner=alice)"

# ── the actual feature ───────────────────────────────────────────────
echo
echo "1. bob's picker before share:"
curl -s "$BASE/v1/hosts" -H "Authorization: Bearer $BOB" | jqp "print('  ', json.load(sys.stdin)['hosts'])"

echo "2. bob reaches alice's host (expect 403):"
curl -s -o /dev/null -w "   HTTP %{http_code}\n" "$BASE/v1/hosts/$HID/filesystem?path=~" -H "Authorization: Bearer $BOB"

echo "3. alice shares with bob:"
curl -s -X POST "$BASE/v1/hosts/$HID/share" -H "Authorization: Bearer $ALICE" \
  -H 'Content-Type: application/json' -d '{"user_id":"bob"}'; echo

echo "4. bob's picker after share:"
curl -s "$BASE/v1/hosts" -H "Authorization: Bearer $BOB" \
  | jqp "h=json.load(sys.stdin)['hosts'];[print(f\"   {x['name']} (owner={x['owner']}, mine={x['is_owned_by_me']})\") for x in h]"

echo "5. bob reads alice's REAL filesystem over the tunnel:"
curl -s "$BASE/v1/hosts/$HID/filesystem?path=~" -H "Authorization: Bearer $BOB" \
  | jqp "d=json.load(sys.stdin);print('  ',[e.get('name') for e in d.get('data',[])][:6])"

echo "6. bob cannot re-share it to carol (expect 403):"
curl -s -o /dev/null -w "   HTTP %{http_code}\n" -X POST "$BASE/v1/hosts/$HID/share" \
  -H "Authorization: Bearer $BOB" -H 'Content-Type: application/json' -d '{"user_id":"carol"}'

echo "7. alice revokes:"
curl -s -X DELETE "$BASE/v1/hosts/$HID/share/bob" -H "Authorization: Bearer $ALICE"; echo

echo "8. bob is locked out again (expect 403):"
curl -s -o /dev/null -w "   HTTP %{http_code}\n" "$BASE/v1/hosts/$HID/filesystem?path=~" -H "Authorization: Bearer $BOB"

echo
echo "done. logs in $WORK"
