# biject

Meta repo for the **biject** platform — a formal-verification guardrail layer for
AI-agent tool calls. An agent's proposed action is turned into a Lean 4 conjecture,
checked by a pre-warmed Lean kernel pool against compiled policies, and the binary
PROVED / REFUTED verdict is written to a cryptographically signed, hash-chained audit
ledger.

**This repo owns the deployment topology and nothing else.** There is no application
code here, and no image is published from here.

| Repo | Role | Port |
| --- | --- | --- |
| [`biject-api`](https://github.com/bijectai/biject-api) | Verification core — FastAPI, Lean kernel pool, policy registry, signed ledger | 8002 |
| [`biject-proxy`](https://github.com/bijectai/biject-proxy) | Inline enforcement hop. Forwards only on a positive verdict | 8080 |
| [`biject-trace`](https://github.com/bijectai/biject-trace) | Read-only ledger query + chain verification | 8010 |
| [`biject-judge`](https://github.com/bijectai/biject-judge) | Advisory scoring. Never authorizes | 8020 |
| [`biject-console`](https://github.com/bijectai/biject-console) | Operator status board | 5173 |
| [`biject-contracts`](https://github.com/bijectai/biject-contracts) | The wire contracts. No runtime | — |

## Before first boot

```bash
cp .env.example .env
python3 -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"
# paste into AUDIT_SIGNING_KEY
```

`biject-api` raises at **import** time without `AUDIT_SIGNING_KEY` — there is no
ephemeral-key fallback — so the stack will not start until this is set. Rotating it
later invalidates every prior signature and breaks chain continuity for existing ledger
entries, so a rotation needs a migration plan rather than a re-roll.

## Running it

```bash
docker compose up -d
./scripts/smoke.sh
```

`smoke.sh` probes each service **and** compares the `image_sha` it reports against the
pin in `docker-compose.yml`. `docker compose ps` tells you a container is healthy; only
this tells you it is the build this repo claims to deploy.

## Pinning

Every image is pinned to something immutable: biject services to a full 40-character
git SHA, third-party images to a `sha256` digest. Each service repo's CI publishes
exactly one tag per merge to `main` — `ghcr.io/bijectai/<repo>:<git-sha>` — and no
`:latest`, so there is no floating tag to deploy by accident.

```bash
./scripts/pin-images.sh biject-proxy 398a456b8bf87472b89c12022fffc5e322953d27
./scripts/verify-pins.sh
```

`pin-images.sh` moves the image tag and the matching `BIJECT_IMAGE_SHA` together — a
container that reports a different SHA than the one deployed is worse than one that
reports nothing. `verify-pins.sh` runs in CI and rejects floating tags, missing digests,
and any `build:` stanza that would let something other than the pinned image run under
the pinned name.

**A pin change is a deploy.** The diff is the deploy record.

## Two things that look like omissions and are not

**`biject-proxy` has no `depends_on: biject-api`, and its healthcheck probes `/healthz`
rather than `/readyz`.** The proxy is fail-closed: with the verifier unreachable it
comes up and denies every call, which is the specified behaviour. An ordering dependency
would stop it starting exactly when you most want it up and denying, and a readiness
healthcheck would get it restarted for doing its job.

**`biject-console` has no `depends_on` either.** nginx resolves upstreams per request, so
the console comes up and *reports* an outage instead of joining it.

## Current state

The pins name real commits, but **the images they name have not been published yet** —
each service repo's CI publishes on merge to `main`, and those merges have not happened.
`biject-api` additionally has no publish workflow at all yet, so nothing produces
`ghcr.io/bijectai/biject-api:<sha>`. See `CLAUDE.md` § Gate conditions.

See [`CLAUDE.md`](CLAUDE.md) for the rules governing this file and the deploy procedure.
