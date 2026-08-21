# repo-structure-standard — the meta repo has no image

## What the ticket assumed

> Per repo add: CLAUDE.md (context + ownership + the three §2B rules), AGENTS.md
> (Codex rules), CODEOWNERS, `.claude/deviations/`, Dockerfile, CI workflow.
>
> Standard CI per service repo: test, build image, push
> `ghcr.io/bijectai/<repo>:<sha>`, contract-drift check.

Read literally, "per repo" includes this one: a `Dockerfile`, a four-job CI, and a
`ghcr.io/bijectai/biject:<sha>` image.

## What was actually true

The meta repo contains a compose file, three shell scripts, and documentation. There is
no process to start, no port to expose, and no health check to answer. An image built
from it would contain a compose file that describes *other* images — and pinning the
meta repo's own image inside the meta repo's own compose is circular.

The contract-drift check has the same shape problem: this repo speaks no wire protocol
and consumes no schema, so there is nothing to drift.

## Decision

Adapt the standard rather than pad it out. Approved as part of the repo-structure
standardization work.

The governance files apply in full — `CLAUDE.md`, `AGENTS.md`, `.github/CODEOWNERS`, and
this directory are all present, and the three §2B rules are stated here byte-identically
with every other repo, including the "in this repo" notes that say how each one applies
to a topology file rather than to code.

The two build-shaped items do not: **no `Dockerfile`, and a two-job CI instead of four.**

## What changed as a result

- **CI is `compose` + `pins`.** `compose` runs `docker compose config -q` against a
  synthetic environment and greps for a literal secret assignment; `pins` runs
  `scripts/verify-pins.sh` and syntax-checks the shell scripts. Both are deterministic
  and cost seconds, which is what earns them a place in CI rather than in review.
- **The contract-drift check becomes a pin-immutability check.** It is the same idea
  pointed at this repo's actual invariant: in a service repo, CI asserts that the
  vendored contract matches upstream; here it asserts that every image reference is
  immutable and that no `build:` stanza can substitute something else under a pinned
  name. Both are "the thing you committed is the thing you get".
- **A secret-in-topology check was added.** Not in the original standard, but this is the
  one repo where a committed secret is a realistic accident: it is the only place that
  names `AUDIT_SIGNING_KEY` at all, and the difference between `${AUDIT_SIGNING_KEY:?}`
  and a pasted value is one careless edit. Mechanical, so it belongs in CI.

## Accepted residual risk

- **The pins currently name images that do not exist.** Each is a real commit SHA that
  the corresponding service repo's CI will build on merge to `main`, but until those
  merges happen `docker compose pull` will 404. This was chosen over leaving the file
  unpinned or pointing at `:latest`: a pin that is not yet resolvable fails loudly and
  obviously, whereas a floating tag fails silently by running the wrong thing.
- **`biject-api` has no publish workflow**, so nothing produces its image at any SHA.
  That repo was explicitly out of scope for this change. It is the single blocking item
  for a full-stack `docker compose up` and is recorded as a gate condition in
  `CLAUDE.md`.
- **`biject-console` reports no `image_sha`.** Its `/healthz` is served by nginx as a
  static string, so `scripts/smoke.sh` can confirm the console is up but cannot confirm
  it is the pinned build. Closing this needs `envsubst` at container start, which costs
  an entrypoint script; deferred until there is a second reason to want one.
