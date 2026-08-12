#!/usr/bin/env bash
# Is the stack actually up, and is it running what docker-compose.yml pins?
#
# The second half is the part worth having. `docker compose ps` tells you a
# container is healthy; it does not tell you the container is the build this repo
# claims to deploy. Each service reports BIJECT_IMAGE_SHA on /healthz, and this
# script compares that against the pin — so a hand-retagged image or a stale
# container is caught rather than assumed away.
#
#   ./scripts/smoke.sh
set -uo pipefail

COMPOSE=docker-compose.yml
fail=0

probe() {
  local name="$1" url="$2"
  local body
  body=$(curl -fsS --max-time 5 "$url" 2>/dev/null) || {
    printf '  %-16s UNREACHABLE  %s\n' "$name" "$url"
    fail=1
    return
  }
  printf '  %-16s ok\n' "$name"

  # BIJECT_IMAGE_SHA is only set for the services this repo pins by SHA.
  local pinned running
  pinned=$(grep -A20 "^  ${name}:" "$COMPOSE" | grep -m1 -oE 'BIJECT_IMAGE_SHA: [0-9a-f]{40}' | awk '{print $2}')
  [ -n "$pinned" ] || return

  running=$(printf '%s' "$body" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("image_sha",""))' 2>/dev/null)
  if [ -z "$running" ]; then
    printf '  %-16s   (reports no image_sha — built from source?)\n' ""
  elif [ "$running" != "$pinned" ]; then
    printf '  %-16s   MISMATCH: running %s, pinned %s\n' "" "${running:0:12}" "${pinned:0:12}"
    fail=1
  else
    printf '  %-16s   image %s matches the pin\n' "" "${running:0:12}"
  fi
}

echo "Health:"
probe biject-api     http://localhost:8002/api/health
probe biject-proxy   http://localhost:8080/healthz
probe biject-trace   http://localhost:8010/healthz
probe biject-judge   http://localhost:8020/healthz
probe biject-console http://localhost:5173/healthz

echo
echo "Ledger:"
curl -fsS --max-time 5 http://localhost:8010/v1/verify 2>/dev/null \
  | python3 -m json.tool 2>/dev/null | sed 's/^/  /' \
  || echo "  biject-trace did not answer /v1/verify"

echo
if [ "$fail" -ne 0 ]; then
  echo "SMOKE FAILED"
  exit 1
fi
echo "Smoke passed."
