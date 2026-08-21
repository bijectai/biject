#!/usr/bin/env bash
# Every image in docker-compose.yml must be pinned to something immutable.
#
# This is the rule the whole meta repo exists to hold: a biject service image is
# pinned to a full 40-character git SHA, and a third-party image is pinned to a
# sha256 digest. A floating tag would make "what is deployed" a question about
# when someone last pulled, which is exactly what pinning is for.
#
# Known limit, deliberately not papered over: a 40-hex git SHA is still a *tag*,
# and an OCI tag is mutable no matter what it looks like. Anyone who can push to
# the GHCR package can retarget it, and this check would still say "ok" — it
# verifies the shape of the reference, not the bytes behind it. Only an
# @sha256: digest is immutable at the registry. Closing that gap means pinning
# first-party images as `:<git-sha>@sha256:<digest>`; see CLAUDE.md § Gate
# conditions for why it has not happened yet.
#
# Runs in CI on every PR and is safe to run locally:
#   ./scripts/verify-pins.sh
set -euo pipefail

COMPOSE="${1:-docker-compose.yml}"
FIRST_PARTY='^ghcr\.io/bijectai/[a-z0-9-]+:[0-9a-f]{40}$'
THIRD_PARTY='@sha256:[0-9a-f]{64}$'

fail=0

# Only `image:` lines — a `cache_from:` or a comment mentioning an image is not
# a deployment pin.
images=$(grep -oE '^[[:space:]]+image:[[:space:]]*\S+' "$COMPOSE" | awk '{print $2}')

if [ -z "$images" ]; then
  echo "::error title=No images::$COMPOSE declares no images. That is almost certainly a mistake."
  exit 1
fi

while IFS= read -r image; do
  case "$image" in
    ghcr.io/bijectai/*)
      if [[ "$image" =~ $FIRST_PARTY ]]; then
        echo "  ok        $image"
      else
        echo "  NOT PINNED $image"
        echo "             a biject image must be tagged with a full 40-character git SHA"
        fail=1
      fi
      ;;
    *)
      if [[ "$image" =~ $THIRD_PARTY ]]; then
        echo "  ok        $image"
      else
        echo "  NOT PINNED $image"
        echo "             a third-party image must carry an @sha256: digest"
        fail=1
      fi
      ;;
  esac
done <<< "$images"

# A `build:` beside an `image:` means someone can build something other than what
# is pinned and run it under the pinned name. That silently defeats the pin.
if grep -qE '^[[:space:]]+build:' "$COMPOSE"; then
  echo
  echo "::error title=Build stanza::$COMPOSE contains a 'build:' key."
  echo "This file deploys published, pinned images. Building here means the name"
  echo "in the file no longer identifies what is running."
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  echo
  echo "::error title=Unpinned image::every image must be immutable — see the header of $COMPOSE"
  echo "Move a pin with: ./scripts/pin-images.sh <service> <sha>"
  exit 1
fi

echo
echo "All images pinned."
