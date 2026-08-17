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
unverified=()

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
  # A service without it cannot be identity-checked at all — that is a real
  # hole, not a pass, so it is collected and reported rather than skipped
  # silently. Reaching `return` here used to look like success.
  local pinned running
  pinned=$(grep -A20 "^  ${name}:" "$COMPOSE" | grep -m1 -oE 'BIJECT_IMAGE_SHA: [0-9a-f]{40}' | awk '{print $2}')
  if [ -z "$pinned" ]; then
    printf '  %-16s   NOT VERIFIED: no BIJECT_IMAGE_SHA in %s\n' "" "$COMPOSE"
    unverified+=("$name")
    return
  fi

  running=$(printf '%s' "$body" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("image_sha",""))' 2>/dev/null)
  if [ -z "$running" ]; then
    # The pin says this service reports its identity, and it did not. That is
    # an image older than the field or a broken build — either way the running
    # container is unidentified, which is exactly what this script exists to
    # catch. Failing.
    printf '  %-16s   FAIL: pinned to %s but reports no image_sha\n' "" "${pinned:0:12}"
    fail=1
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
# Say out loud how much of the stack actually had its identity checked. The
# point of this script is the claim "this is the build the repo pins", and that
# claim does not hold for a service that never reported one — so the coverage
# is printed next to the verdict rather than left for the reader to infer from
# a silent pass.
if [ "${#unverified[@]}" -ne 0 ]; then
  echo "Image identity NOT verified for: ${unverified[*]}"
  echo "  Those services carry no BIJECT_IMAGE_SHA in $COMPOSE, so a stale or"
  echo "  hand-retagged image would not be caught. See CLAUDE.md § Gate conditions."
  echo
fi

if [ "$fail" -ne 0 ]; then
  echo "SMOKE FAILED"
  exit 1
fi

if [ "${#unverified[@]}" -ne 0 ]; then
  echo "Smoke passed (with ${#unverified[@]} service(s) unverified — see above)."
else
  echo "Smoke passed."
fi
