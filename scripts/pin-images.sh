#!/usr/bin/env bash
# Move a service's image pin, and the BIJECT_IMAGE_SHA that goes with it.
#
# Both places must change together: the image tag decides what runs, and
# BIJECT_IMAGE_SHA is what that container reports on /healthz. If they disagree,
# the platform lies to you about what is deployed — which is worse than not
# reporting it at all.
#
#   ./scripts/pin-images.sh biject-proxy 9fa255babe470b43fa5ff0bfaa0dbc6037142bd9
#
# The SHA is the commit that the service repo's CI built and pushed. Find it in
# that repo's `ci` run summary, which prints the pin line ready to paste.
set -euo pipefail

COMPOSE=docker-compose.yml

usage() {
  echo "usage: $0 <service> <40-char-git-sha>"
  echo
  echo "services:"
  grep -oE '^  biject-[a-z]+:' "$COMPOSE" | tr -d ' :' | sed 's/^/  /'
  exit 2
}

[ $# -eq 2 ] || usage
service="$1"
sha="$2"

if ! [[ "$sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "error: '$sha' is not a full 40-character git SHA."
  echo "Short SHAs are ambiguous and abbreviations drift as history grows —"
  echo "this file pins the whole thing on purpose."
  exit 1
fi

if ! grep -q "image: ghcr.io/bijectai/${service}:" "$COMPOSE"; then
  echo "error: no service '$service' with a ghcr.io/bijectai image in $COMPOSE"
  usage
fi

python3 - "$COMPOSE" "$service" "$sha" <<'PY'
import re, sys
path, service, sha = sys.argv[1], sys.argv[2], sys.argv[3]
src = open(path).read()

image = re.sub(
    rf"(image: ghcr\.io/bijectai/{re.escape(service)}:)[0-9a-f]{{40}}",
    rf"\g<1>{sha}", src)

# BIJECT_IMAGE_SHA lives inside the same service block. Scope the substitution to
# that block so pinning one service cannot rewrite another's.
block = re.compile(
    rf"(^  {re.escape(service)}:\n(?:(?:    |\n).*\n)*?)(?=^  \S|\Z)",
    re.M)

def fix(match):
    return re.sub(r"(BIJECT_IMAGE_SHA: )[0-9a-f]{40}", rf"\g<1>{sha}", match.group(1))

open(path, "w").write(block.sub(fix, image))
PY

echo "pinned $service -> $sha"
grep -n -A1 "image: ghcr.io/bijectai/${service}:" "$COMPOSE" | head -4
echo
echo "Verify, then commit the diff — a pin change is a deploy:"
echo "  ./scripts/verify-pins.sh"
